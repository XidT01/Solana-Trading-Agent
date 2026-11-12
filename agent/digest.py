"""Nightly Telegram digest: one message covering every chain, built from that day's report files.

Run after the per-chain reports:  python -m agent.digest [--date YYYY-MM-DD] [--dry-run]
Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (otherwise it only prints the message).
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from datetime import date, timedelta

import requests

from .common import load_config, log, read_json, setup_logging, utcnow
from .report import data_dir

CHAINS = {
    "solana":    {"name": "Solana", "icon": "🟣", "unit": "SOL", "gmgn": "sol", "explorer": None, "dex": "solana"},
    "bsc":       {"name": "BSC", "icon": "🟡", "unit": "USD", "gmgn": "bsc", "explorer": None, "dex": "bsc"},
    "base":      {"name": "Base", "icon": "🔵", "unit": "USD", "gmgn": "base", "explorer": None, "dex": "base"},
    "robinhood": {"name": "Robinhood", "icon": "🟢", "unit": "USD", "gmgn": None,
                  "explorer": "https://robinhoodchain.blockscout.com", "dex": "robinhood"},
}
MAX_LEN = 3900          # Telegram's limit is 4096 characters per message
TOP_WALLETS = 5         # new wallets listed per chain
BIDI = re.compile("[‎‏‪-‮⁦-⁩]")


def esc(s) -> str:
    return html.escape(BIDI.sub("", str(s or "")), quote=True)


def short(a: str) -> str:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else a or ""


def money(n: float) -> str:
    a = abs(n)
    if a >= 1e6:
        return f"${n / 1e6:.2f}M".replace(".00M", "M")
    if a >= 1e3:
        return f"${n / 1e3:.1f}K".replace(".0K", "K")
    return f"${n:,.0f}"


def pnl_text(h: dict, unit: str) -> str:
    v = h.get("pnl_sol", h.get("pnl_usd"))
    if v is None:
        return ""
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):,.1f} SOL" if unit == "SOL" else f"{sign}{money(abs(v))}"


def wallet_link(chain: str, a: str) -> str:
    c = CHAINS[chain]
    url = f"https://gmgn.ai/{c['gmgn']}/address/{a}" if c["gmgn"] else f"{c['explorer']}/address/{a}"
    return f'<a href="{esc(url)}">{esc(short(a))}</a>'


def coin_link(chain: str, mint: str, symbol: str) -> str:
    c = CHAINS[chain]
    url = f"https://gmgn.ai/{c['gmgn']}/token/{mint}" if c["gmgn"] else f"https://dexscreener.com/{c['dex']}/{mint}"
    return f'<a href="{esc(url)}">{esc(symbol or short(mint))}</a>'


def plain_note(md: str, limit: int = 320) -> str:
    """First part of the AI summary as plain text (markdown stripped), cut at a sentence end."""
    text = re.sub(r"\*\*|`|^#+\s*|^\s*[-*]\s+", "", md, flags=re.M)
    text = re.sub(r"\n+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > limit // 2 else cut.rstrip() + "…")


def chain_section(chain: str, r: dict | None, day: str) -> str:
    c = CHAINS[chain]
    head = f"{c['icon']} <b>{c['name']}</b>"
    if not r or not r["stats"].get("runs"):
        return f"{head}\n<i>No scans today.</i>"
    s = r["stats"]
    lines = [f"{head}  ·  {s['runs']} scans · {s['analyzed']} coins analysed · {s['rejected']} unsafe removed"]
    new = r.get("new_wallets") or []
    if new:
        tens = sum(1 for h in new if h["multiple"] >= 10)
        lines.append(f"🏆 <b>{len(new)} new wallet{'s' if len(new) != 1 else ''}</b>"
                     + (f" ({tens} at 10x+)" if tens else ""))
        for i, h in enumerate(new[:TOP_WALLETS], 1):
            star = "🔥 " if h["multiple"] >= 10 else ""
            lines.append(f"{i}. {star}{wallet_link(chain, h['wallet'])} <b>{h['multiple']:.1f}x</b> on "
                         f"{coin_link(chain, h['mint'], h['symbol'])} · entry {money(h['avg_entry_mcap'])} · "
                         f"{pnl_text(h, c['unit'])}")
        if len(new) > TOP_WALLETS:
            lines.append(f"<i>+{len(new) - TOP_WALLETS} more on the dashboard</i>")
    else:
        lines.append("No new wallets today.")
    repeat = [w for w in r.get("leaderboard") or [] if w["hits"] >= 2 and w["last_hit"].startswith(day)]
    for w in repeat[:3]:
        coins = ", ".join(esc(t) for t in w["tokens"][-4:])
        lines.append(f"🔁 Repeat winner {wallet_link(chain, w['wallet'])}: <b>{w['hits']} coins</b> ({coins}), "
                     f"median {w['median_multiple']:.1f}x")
    best = max(r.get("passed_tokens") or [], key=lambda t: t.get("wallets_found", 0), default=None)
    if best and best.get("wallets_found", 0) >= 2:
        lines.append(f"🎯 Most winners: {coin_link(chain, best['mint'], best['symbol'])} "
                     f"({best['wallets_found']} wallets)")
    if r.get("ai_summary"):
        lines.append(f"🧠 <i>{esc(plain_note(r['ai_summary']))}</i>")
    return "\n".join(lines)


def build_messages(day: str, reports: dict[str, dict | None], cfg: dict) -> list[str]:
    w = cfg["wallets"]
    nice = date.fromisoformat(day).strftime("%a %d %b")
    total = sum(len((r or {}).get("new_wallets") or []) for r in reports.values())
    tens = sum(1 for r in reports.values() for h in (r or {}).get("new_wallets") or [] if h["multiple"] >= 10)
    header = (f"📊 <b>Smart Wallet Daily — {nice}</b>\n"
              f"<b>{total}</b> new wallet{'s' if total != 1 else ''} today"
              + (f", <b>{tens}</b> at 10x+" if tens else "") + "\n"
              f"<i>Coins that peaked at {money(w['target_mcap_usd'])}+ · entry under {money(w['max_entry_mcap_usd'])}"
              f" · {w['min_multiple']:g}x+</i>")
    sections = [chain_section(k, reports.get(k), day) for k in CHAINS]
    footer = "<i>Automated on-chain analysis. Not financial advice.</i>"
    messages, current = [], header
    for sec in sections:  # split between chains if the message gets too long
        if len(current) + len(sec) + 2 > MAX_LEN:
            messages.append(current)
            current = sec
        else:
            current += "\n\n" + sec
    current += "\n\n" + footer
    messages.append(current)
    return messages


def send(messages: list[str], dashboard: str | None) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.info("Telegram not configured; not sending")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for i, text in enumerate(messages):
        body = {"chat_id": chat, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if dashboard and i == len(messages) - 1:
            body["reply_markup"] = {"inline_keyboard": [[{"text": "📈 Open dashboard", "url": dashboard}]]}
        try:
            r = requests.post(url, json=body, timeout=20)
            if not r.ok:  # e.g. a formatting problem: fall back to plain text so the report still arrives
                log.warning("Telegram HTML send failed (%s): %s", r.status_code, r.text[:200])
                plain = re.sub(r"<[^>]+>", "", text)
                r = requests.post(url, json={"chat_id": chat, "text": html.unescape(plain),
                                             "link_preview_options": {"is_disabled": True}}, timeout=20)
            ok &= r.ok
        except requests.RequestException as e:
            log.warning("Telegram send failed: %s", e)
            ok = False
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC date, default: the day the nightly report covers")
    ap.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(args.config)
    day = args.date or (utcnow() - timedelta(hours=6)).date().isoformat()
    reports = {k: read_json(data_dir(k) / "reports" / f"{day}.json", None) for k in CHAINS}
    if not any(r and r["stats"].get("runs") for r in reports.values()):
        log.info("No scans on %s; no digest", day)
        return 0
    messages = build_messages(day, reports, cfg)
    if args.dry_run:
        print("\n\n----- next message -----\n\n".join(messages))
        return 0
    send(messages, cfg["report"].get("dashboard_url"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
