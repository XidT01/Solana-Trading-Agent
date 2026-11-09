"""One scan cycle: discover fresh launches -> filter -> analyse early wallets -> store.

Run:  SOLANA_RPC_URL=... python -m agent.scan [--force]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

from .common import DATA, WSOL, load_config, log, read_json, setup_logging, utcnow, write_json
from .safety import (allowed_launch, launch_origin, launchlab_platform, market_checks, market_snapshot,
                     onchain_checks, rugcheck_checks)
from .sources import DexScreener, GeckoTerminal, RugCheck, SolanaRPC
from .wallets import TokenAnalyzer


def in_campaign_window(cfg: dict, now) -> tuple[bool, str]:
    c = cfg["campaign"]
    today = now.date()
    if today < date.fromisoformat(c["start_date"]) or today > date.fromisoformat(c["end_date"]):
        return False, f"outside campaign dates {c['start_date']}..{c['end_date']}"
    start, end = c["peak_hours_utc"]
    if not (start <= now.hour < end):
        return False, f"outside peak hours {start:02d}:00-{end:02d}:00 UTC"
    return True, "ok"


STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def quote_values(pairs: list[dict], ds, sol_usd: float, cache: dict) -> dict[str, float]:
    """Non-SOL quote assets of a token's pools -> value in SOL per unit."""
    out = {}
    for p in pairs:
        q = (p.get("quoteToken") or {}).get("address")
        if not q or q == WSOL:
            continue
        if q not in cache:
            cache[q] = 1.0 if q in STABLES else ds.price_usd(q)
        if cache[q]:
            out[q] = cache[q] / sol_usd
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore campaign dates / peak hours")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(args.config)
    now = utcnow()

    ok, why = in_campaign_window(cfg, now)
    if not ok and not args.force:
        log.info("Not scanning: %s", why)
        return 0
    rpc_url = os.environ.get("SOLANA_RPC_URL")
    if not rpc_url:
        log.error("SOLANA_RPC_URL is not set (e.g. https://mainnet.helius-rpc.com/?api-key=...)")
        return 2

    d, s, w = cfg["discovery"], cfg["safety"], cfg["wallets"]
    tokens = read_json(DATA / "tokens.json", {})
    wallets = read_json(DATA / "wallets.json", {})
    seen = read_json(DATA / "seen.json", {})
    cutoff = (now.date() - timedelta(days=3)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    today = now.date().isoformat()
    done_today = sum(1 for r in tokens.values()
                     if r.get("verdict") == "analyzed" and r.get("checked_at", "").startswith(today))
    budget = max(0, min(d["max_tokens_per_run"], d["max_tokens_per_day"] - done_today))

    ds, gt, rc = DexScreener(), GeckoTerminal(), RugCheck()
    rpc = SolanaRPC(rpc_url, cfg["rpc"]["requests_per_second"])

    # ---- discovery
    found = [m for m, _ in gt.new_pools(d["geckoterminal_new_pool_pages"])]
    if d["use_dexscreener_profiles"]:
        found += ds.latest_solana_mints()
    fresh = list(dict.fromkeys(m for m in found if m != WSOL and m not in tokens and m not in seen))
    log.info("Discovered %d mints, %d new", len(set(found)), len(fresh))
    sol_usd = ds.sol_usd()
    if not sol_usd:
        log.error("Could not fetch SOL/USD price")
        return 1
    pairs = ds.pairs_for(fresh)

    stats = {"discovered": len(fresh), "launchpad_rejected": 0, "too_old": 0, "below_target_mcap": 0,
             "market_rejected": 0, "safety_rejected": 0, "analyzed": 0, "skipped": 0, "wallets_found": 0}
    shortlist = []
    for mint in fresh:
        ps = pairs.get(mint) or []
        if not ps:
            continue  # not indexed yet — try again next run
        m = market_snapshot(ps)
        age_h = (now.timestamp() * 1000 - m["first_pair_ms"]) / 3.6e6 if m["first_pair_ms"] else 0
        if age_h > d["max_token_age_hours"]:
            stats["too_old"] += 1
            seen[mint] = today
            continue
        if m["mcap_usd"] < w["target_mcap_usd"]:
            stats["below_target_mcap"] += 1  # not recorded: may still run past $50K later
            continue
        # Launch platform from the creation tx (stonk.fun has no vanity suffix, so this needs the chain).
        creation_tx = None
        if not mint.endswith("pump"):
            sigs, _ = rpc.signatures_asc(mint, w["max_signature_pages"])
            creation_tx = rpc.get_transaction(sigs[0]["signature"]) if sigs else None
        origin = launch_origin(mint, ps, cfg, creation_tx)
        if not allowed_launch(origin, cfg):
            stats["launchpad_rejected"] += 1
            seen[mint] = today
            continue
        rec = {"mint": mint, "origin": origin, "launchlab_platform": launchlab_platform(creation_tx, cfg),
               "age_hours": round(age_h, 1), "checked_at": now.isoformat(), **m}
        reasons = market_checks(m, cfg)
        if reasons:
            stats["market_rejected"] += 1
            tokens[mint] = {**rec, "verdict": "rejected", "stage": "market", "reasons": reasons}
            continue
        shortlist.append(rec)

    shortlist.sort(key=lambda r: -r["volume_24h"])
    price_cache: dict[str, float | None] = {}
    analyzer = TokenAnalyzer(rpc, cfg)
    for rec in shortlist:
        if budget <= 0:
            break  # unrecorded -> picked up next run
        mint = rec["mint"]
        info = rpc.mint_info(mint)
        reasons = onchain_checks(info, cfg)
        if not reasons:
            reasons = rugcheck_checks(rc.report(mint), rec["pair"], cfg)
        if reasons:
            stats["safety_rejected"] += 1
            tokens[mint] = {**rec, "verdict": "rejected", "stage": "safety", "reasons": reasons}
            continue
        budget -= 1
        price_sol = rec["price_sol"] or rec["price_usd"] / sol_usd
        try:
            quotes = quote_values(pairs[mint], ds, sol_usd, price_cache)
            res = analyzer.analyze(mint, info["supply"], sol_usd, price_sol, quotes)
        except Exception as e:  # one bad token must not kill the run
            log.exception("analysis failed for %s", mint)
            res = {"status": "skipped", "reason": f"error: {e}"}
        if res["status"] != "analyzed":
            stats["skipped"] += 1
            tokens[mint] = {**rec, "verdict": "skipped", "stage": "wallets", "reasons": [res["reason"]]}
            continue
        stats["analyzed"] += 1
        stats["wallets_found"] += len(res["wallets"])
        tokens[mint] = {**rec, "verdict": "analyzed", "stage": "wallets", "reasons": [],
                        "sol_usd": sol_usd, "supply": info["supply"], **res}
        for hit in res["wallets"]:
            entry = wallets.setdefault(hit["wallet"], {"hits": []})
            entry["hits"] = [h for h in entry["hits"] if h["mint"] != mint] + [
                {**hit, "mint": mint, "symbol": rec["symbol"], "origin": rec["origin"], "found_at": now.isoformat()}]

    write_json(DATA / "tokens.json", tokens)
    write_json(DATA / "wallets.json", wallets)
    write_json(DATA / "seen.json", seen)
    runs = read_json(DATA / "runs.json", [])
    runs.append({"at": now.isoformat(), "rpc_calls": rpc.calls, "sol_usd": sol_usd, **stats})
    write_json(DATA / "runs.json", runs[-3000:])
    log.info("Run done: %s (rpc calls: %d)", stats, rpc.calls)
    return 0



if __name__ == "__main__":
    sys.exit(main())
