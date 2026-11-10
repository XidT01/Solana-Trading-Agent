"""Daily report: stats for the day + cumulative wallet leaderboard + AI analyst summary.

Run:  python -m agent.report [--date YYYY-MM-DD]
Optional env: ANTHROPIC_API_KEY (AI summary), TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (push).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter

import requests

from .common import DATA, load_config, log, read_json, setup_logging, utcnow, write_json

SYSTEM_PROMPT = """You are the on-chain analyst behind a Solana meme-coin wallet tracker.
Each day you receive JSON: the day's scan statistics, the coins that peaked above the target market cap
and passed every safety filter, the wallets that qualified today (average entry below the entry market cap
and at least the minimum multiple, tiered as 5x+ or 10x+), and the cumulative wallet leaderboard.

Write the daily briefing in Markdown, 200-350 words, with these sections:
**Today in numbers**, **Standout wallets**, **Red flags**, **Watch tomorrow**.

Judge wallets the way an experienced on-chain analyst would: repeat hits across different tokens are
the strongest signal, a single hit is often luck; 10x+ wallets with repeat hits matter most. Flag wallets that look like snipers, insiders or
bundlers (first buy within seconds of launch, near-identical timing across tokens, tokens transferred out
instead of sold, huge multiples on tiny size). Refer to wallets by their first 4 and last 4 characters.
Stay factual and neutral, never hype, and do not give buy or sell instructions. If the data is thin,
say so plainly."""


def reason_key(r: str) -> str:
    r = r.split(" (")[0]
    return re.sub(r"_\d+(pct)?$", "", r)


def leaderboard(wallets: dict, until_iso: str, size: int) -> list[dict]:
    rows = []
    for addr, w in wallets.items():
        hits = [h for h in w["hits"] if h["found_at"] <= until_iso]
        if not hits:
            continue
        mults = [h["multiple"] for h in hits]
        rows.append({
            "wallet": addr, "hits": len(hits),
            "tokens": [h["symbol"] for h in sorted(hits, key=lambda h: h["found_at"])],
            "median_multiple": round(statistics.median(mults), 2), "best_multiple": max(mults),
            "tenx_hits": sum(m >= 10 for m in mults),
            "avg_entry_mcap": round(statistics.mean(h["avg_entry_mcap"] for h in hits)),
            "total_pnl_sol": round(sum(h["pnl_sol"] for h in hits), 2),
            "last_hit": max(h["found_at"] for h in hits),
        })
    rows.sort(key=lambda r: (-r["hits"], -r["median_multiple"]))
    return rows[:size]


def ai_summary(payload: dict, cfg: dict) -> str | None:
    if not cfg["report"].get("ai_summary") or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=cfg["report"]["model"],
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
    except Exception as e:  # report must still ship without the AI section
        log.warning("AI summary failed: %s", e)
        return None
    if resp.stop_reason == "refusal":
        log.warning("AI summary declined")
        return None
    return "".join(b.text for b in resp.content if b.type == "text").strip() or None


def to_markdown(r: dict) -> str:
    s = r["stats"]
    lines = [f"# Solana smart-wallet report — {r['date']}", ""]
    if r.get("ai_summary"):
        lines += [r["ai_summary"], ""]
    lines += ["## Scan stats", "",
              f"- Scans run: {s['runs']} · tokens discovered: {s['discovered']}",
              f"- Passed launchpad/age/mcap and checked: {s['tokens_checked']}",
              f"- Rejected (liquidity / honeypot / scam): {s['rejected']}",
              f"- Fully analysed: {s['analyzed']} · qualifying wallets: {s['wallets_found']}"
              f" (10x+: {s.get('wallets_10x', 0)})", ""]
    if r.get("analyzed_by_launchpad"):
        lines += ["Analysed by launchpad: " + ", ".join(f"{k} ({v})" for k, v in r["analyzed_by_launchpad"]), ""]
    if r["rejection_reasons"]:
        lines += ["Top rejection reasons: " + ", ".join(f"{k} ({v})" for k, v in r["rejection_reasons"][:8]), ""]
    lines += ["## New qualifying wallets today", ""]
    if r["new_wallets"]:
        lines += ["| Tier | Wallet | Token | Avg entry mcap | Multiple | PnL (SOL) | Still holding |",
                  "|---|---|---|---|---|---|---|"]
        for h in r["new_wallets"]:
            lines.append(f"| {h.get('tier', '')} | `{h['wallet']}` | {h['symbol']} | ${h['avg_entry_mcap']:,} | {h['multiple']}x | "
                         f"{h['pnl_sol']} | {h['still_holding_pct']}% |")
    else:
        lines.append("_None today._")
    lines += ["", "## Leaderboard (campaign to date)", "",
              "| # | Wallet | Hits | 10x+ hits | Median x | Best x | Avg entry | PnL (SOL) |",
              "|---|---|---|---|---|---|---|---|"]
    for i, w in enumerate(r["leaderboard"], 1):
        lines.append(f"| {i} | `{w['wallet']}` | {w['hits']} | {w.get('tenx_hits', 0)} | {w['median_multiple']} | {w['best_multiple']} | "
                     f"${w['avg_entry_mcap']:,} | {w['total_pnl_sol']} |")
    lines += ["", "_Automated on-chain analysis. Not financial advice._", ""]
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
    except requests.RequestException as e:
        log.warning("Telegram send failed: %s", e)


def build(day: str, cfg: dict) -> dict:
    tokens = read_json(DATA / "tokens.json", {})
    wallets = read_json(DATA / "wallets.json", {})
    runs = [r for r in read_json(DATA / "runs.json", []) if r["at"].startswith(day)]
    todays = [t for t in tokens.values() if t.get("checked_at", "").startswith(day)]
    rejected = [t for t in todays if t["verdict"] == "rejected"]
    analyzed = [t for t in todays if t["verdict"] == "analyzed"]
    new_wallets = sorted(
        ({**h, "symbol": t["symbol"], "mint": t["mint"]} for t in analyzed for h in t.get("wallets", [])),
        key=lambda h: -h["multiple"])
    reasons = Counter(reason_key(r) for t in rejected for r in t["reasons"])
    origins = Counter(t.get("origin", "unknown") for t in analyzed)
    report = {
        "date": day, "generated_at": utcnow().isoformat(),
        "filters": {k: cfg["wallets"][k]
                    for k in ("max_entry_mcap_usd", "target_mcap_usd", "min_multiple", "highlight_multiple")},
        "stats": {"runs": len(runs), "discovered": sum(r["discovered"] for r in runs),
                  "tokens_checked": len(todays), "rejected": len(rejected), "analyzed": len(analyzed),
                  "skipped": sum(t["verdict"] == "skipped" for t in todays), "wallets_found": len(new_wallets),
                  "wallets_10x": sum(h["multiple"] >= cfg["wallets"]["highlight_multiple"] for h in new_wallets)},
        "rejection_reasons": reasons.most_common(),
        "analyzed_by_launchpad": origins.most_common(),
        "passed_tokens": [{k: t.get(k) for k in ("mint", "symbol", "origin", "mcap_usd", "liquidity_usd",
                                                  "early_buyers_checked", "url")} for t in analyzed],
        "new_wallets": new_wallets,
        "leaderboard": leaderboard(wallets, day + "T23:59:59.999999+00:00", cfg["report"]["leaderboard_size"]),
    }
    report["ai_summary"] = ai_summary({k: v for k, v in report.items() if k != "generated_at"}, cfg)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC date, default today")
    ap.add_argument("--config")
    ap.add_argument("--force", action="store_true", help="report even outside the campaign dates")
    args = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(args.config)
    day = args.date or utcnow().date().isoformat()
    c = cfg["campaign"]
    if not args.force and not (c["start_date"] <= day <= c["end_date"]):
        log.info("No report: %s is outside the campaign %s..%s", day, c["start_date"], c["end_date"])
        return 0
    report = build(day, cfg)
    md = to_markdown(report)
    out = DATA / "reports"
    write_json(out / f"{day}.json", report)
    (out / f"{day}.md").write_text(md)
    index = read_json(out / "index.json", [])
    index = [e for e in index if e["date"] != day] + [
        {"date": day, "wallets_found": report["stats"]["wallets_found"], "analyzed": report["stats"]["analyzed"]}]
    write_json(out / "index.json", sorted(index, key=lambda e: e["date"], reverse=True))
    send_telegram(md)
    log.info("Report written for %s: %s", day, report["stats"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
