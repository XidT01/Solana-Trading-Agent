"""One scan cycle: discover launches -> keep coins that peaked at $100K+ -> filter -> analyse early wallets.

Run:  SOLANA_RPC_URL=... python -m agent.scan [--force]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, datetime, timedelta

from .common import DATA, WSOL, load_config, log, read_json, setup_logging, utcnow, write_json
from .safety import (allowed_launch, launch_origin, launchlab_platform, market_checks, market_snapshot,
                     onchain_checks, rugcheck_checks)
from .sources import DexScreener, GeckoTerminal, RugCheck, SolanaRPC
from .wallets import TokenAnalyzer

STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def in_campaign_window(cfg: dict, now) -> tuple[bool, str]:
    c = cfg["campaign"]
    today = now.date()
    if today < date.fromisoformat(c["start_date"]) or today > date.fromisoformat(c["end_date"]):
        return False, f"outside campaign dates {c['start_date']}..{c['end_date']}"
    start, end = c["scan_hours_utc"]
    if not (start <= now.hour < end):
        return False, f"outside scan hours {start:02d}:00-{end:02d}:00 UTC"
    return True, "ok"


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


CANDLE_SECONDS = 15 * 60


def peak_from_candles(candles: list[list[float]], supply: float, target: float) -> tuple[float | None, int | None]:
    """(peak market cap, latest time by which the coin had reached `target`).

    A single odd trade can spike one candle's high, so the coin only counts as having reached the target
    once a candle CLOSED above it, or the high reached it in at least two candles. Candles are stamped with
    their START time, so the crossing happened before the END of that candle."""
    if not candles or not supply:
        return None, None
    hits = [c for c in candles if c[2] * supply >= target]
    closed = [c for c in candles if c[4] * supply >= target]
    if len(hits) >= 2:
        peak = max(c[2] for c in candles) * supply
    else:
        peak = max(c[4] for c in candles) * supply
    firsts = [int(c[0]) for c in closed[:1]] + [int(c[0]) for c in hits[1:2]]
    cross = min(firsts) + CANDLE_SECONDS if firsts else None
    return peak, cross


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore campaign dates / scan hours")
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

    d, w = cfg["discovery"], cfg["wallets"]
    target = w["target_mcap_usd"]
    tokens = read_json(DATA / "tokens.json", {})
    wallets = read_json(DATA / "wallets.json", {})
    seen = read_json(DATA / "seen.json", {})
    watch = read_json(DATA / "watchlist.json", {})
    cutoff = (now.date() - timedelta(days=3)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    today = now.date().isoformat()
    done_today = sum(1 for r in tokens.values()
                     if r.get("verdict") == "analyzed" and r.get("checked_at", "").startswith(today))
    # Spread the daily analysis budget over the day instead of spending it all in the first hours.
    paced = math.ceil(d["max_tokens_per_day"] * (now.hour + 1) / 24)
    budget = max(0, min(d["max_tokens_per_run"], paced - done_today))

    ds, gt, rc = DexScreener(), GeckoTerminal(), RugCheck()
    rpc = SolanaRPC(rpc_url, cfg["rpc"]["requests_per_second"])

    # ---- discovery: new launches + the watchlist of young coins that may still pump to the target
    found = [m for m, _ in gt.new_pools(d["geckoterminal_new_pool_pages"])]
    if d["use_dexscreener_profiles"]:
        found += ds.latest_solana_mints()
    for m in found:
        if m != WSOL and m not in tokens and m not in seen:
            watch.setdefault(m, now.isoformat())
    candidates = [m for m in watch if m not in tokens and m not in seen]
    log.info("Discovered %d mints; checking %d (watchlist)", len(set(found)), len(candidates))
    sol_usd = ds.sol_usd()
    if not sol_usd:
        log.error("Could not fetch SOL/USD price")
        return 1
    pairs = ds.pairs_for(candidates)

    stats = {"discovered": len(set(found)), "watchlist": len(candidates), "launchpad_rejected": 0, "too_old": 0,
             "below_target_peak": 0, "market_rejected": 0, "safety_rejected": 0, "integrity_rejected": 0, "analyzed": 0, "skipped": 0,
             "wallets_found": 0}
    snaps = {}
    for mint in candidates:
        ps = pairs.get(mint) or []
        watched_h = (now - datetime.fromisoformat(watch[mint])).total_seconds() / 3600
        if not ps:
            if watched_h > d["max_token_age_hours"]:
                watch.pop(mint, None)  # never got indexed
            continue
        m = market_snapshot(ps)
        age_h = (now.timestamp() * 1000 - m["first_pair_ms"]) / 3.6e6 if m["first_pair_ms"] else watched_h
        if age_h > d["max_token_age_hours"]:
            stats["too_old"] += 1
            seen[mint] = today
            watch.pop(mint, None)
            continue
        never_got_going = (age_h > d["watch_drop_no_progress_hours"]
                           and max(m["mcap_usd"], m["est_peak_mcap"]) < d["peak_check_min_mcap_usd"])
        if never_got_going or (age_h > d["watch_drop_after_hours"] and m["mcap_usd"] < d["watch_drop_below_mcap_usd"]):
            seen[mint] = today  # never took off: stop watching
            watch.pop(mint, None)
            continue
        snaps[mint] = (m, age_h)

    # ---- peak market cap: coins that reached the target, even if they have dropped since
    # Peak checks are rate-limited, so rotate them: coins never checked first, then the longest-unchecked.
    # (Ordering by current market cap starved coins that had peaked and fallen back, e.g. PENNY.)
    # Each coin's last measured peak is remembered between scans.
    peaks = read_json(DATA / "peaks.json", {})
    peak_checks = 0
    shortlist = []
    # ...and among those, the likeliest runners (highest estimated peak) first.
    order = sorted(snaps.items(), key=lambda kv: ((peaks.get(kv[0]) or {}).get("at", ""), -kv[1][0]["est_peak_mcap"]))
    for mint, (m, age_h) in order:
        if max(m["mcap_usd"], m["est_peak_mcap"]) < d["peak_check_min_mcap_usd"]:
            stats["below_target_peak"] += 1
            continue
        supply_est = m["mcap_usd"] / m["price_usd"] if m["price_usd"] else 0
        peak, cross_t = None, None
        if peak_checks < d["max_peak_checks_per_run"] and m["pair"]:
            peak_checks += 1
            peak, cross_t = peak_from_candles(gt.ohlcv(m["pair"], d["max_token_age_hours"] + 2), supply_est, target)
            peaks[mint] = {"at": now.isoformat(), "peak": round(peak or 0), "cross": cross_t}
        elif peaks.get(mint):
            peak, cross_t = peaks[mint].get("peak"), peaks[mint].get("cross")
        peak = max(peak or 0, m["mcap_usd"])
        if peak < target:
            stats["below_target_peak"] += 1  # stays on the watchlist
            continue
        ps = pairs[mint]
        # Launch platform from the creation tx (stonk.fun has no vanity suffix, so this needs the chain).
        creation_tx = None
        if not mint.endswith("pump"):
            sigs, _ = rpc.signatures_asc(mint, w["max_signature_pages"])
            creation_tx = rpc.get_transaction(sigs[0]["signature"]) if sigs else None
        origin = launch_origin(mint, ps, cfg, creation_tx)
        if not allowed_launch(origin, cfg):
            stats["launchpad_rejected"] += 1
            seen[mint] = today
            watch.pop(mint, None)
            continue
        rec = {"mint": mint, "origin": origin, "launchlab_platform": launchlab_platform(creation_tx, cfg),
               "age_hours": round(age_h, 1), "checked_at": now.isoformat(), "peak_mcap_usd": round(peak),
               "target_time_hint": cross_t, **m}
        reasons = market_checks(m, cfg)
        if reasons:
            stats["market_rejected"] += 1
            tokens[mint] = {**rec, "verdict": "rejected", "stage": "market", "reasons": reasons}
            watch.pop(mint, None)
            continue
        shortlist.append(rec)

    shortlist.sort(key=lambda r: -r["volume_24h"])
    price_cache: dict[str, float | None] = {}
    analyzer = TokenAnalyzer(rpc, cfg)
    for rec in shortlist:
        if budget <= 0:
            break  # stays on the watchlist -> picked up next run
        mint = rec["mint"]
        watch.pop(mint, None)
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
            res = analyzer.analyze(mint, info["supply"], sol_usd, price_sol, quotes, rec["target_time_hint"])
        except Exception as e:  # one bad token must not kill the run
            log.exception("analysis failed for %s", mint)
            res = {"status": "skipped", "reason": f"error: {e}"}
        for entry in wallets.values():  # the latest verdict on a coin replaces its earlier wallet hits
            entry["hits"] = [h for h in entry["hits"] if h["mint"] != mint]
        if res["status"] != "analyzed":
            rejected = res["status"] == "rejected"
            stats["integrity_rejected" if rejected else "skipped"] += 1
            tokens[mint] = {**rec, "verdict": "rejected" if rejected else "skipped",
                            "stage": "integrity" if rejected else "wallets", "reasons": [res["reason"]]}
            continue
        stats["analyzed"] += 1
        stats["wallets_found"] += len(res["wallets"])
        tokens[mint] = {**rec, "verdict": "analyzed", "stage": "wallets", "reasons": [],
                        "sol_usd": sol_usd, "supply": info["supply"], **res}
        for hit in res["wallets"]:
            entry = wallets.setdefault(hit["wallet"], {"hits": []})
            entry["hits"] = [h for h in entry["hits"] if h["mint"] != mint] + [
                {**hit, "mint": mint, "symbol": rec["symbol"], "origin": rec["origin"], "found_at": now.isoformat()}]

    # Hits only stand for coins whose latest verdict is "analyzed" (a coin rejected later, e.g. by a new
    # safety rule on re-check, takes its wallets' hits with it).
    not_ok = {m for m, r in tokens.items() if r.get("verdict") != "analyzed"}
    for entry in wallets.values():
        entry["hits"] = [h for h in entry["hits"] if h["mint"] not in not_ok]
    wallets = {a: w for a, w in wallets.items() if w["hits"]}
    if len(watch) > d["watchlist_max"]:
        watch = dict(sorted(watch.items(), key=lambda kv: kv[1])[-d["watchlist_max"]:])
    write_json(DATA / "tokens.json", tokens)
    write_json(DATA / "wallets.json", wallets)
    write_json(DATA / "seen.json", seen)
    write_json(DATA / "watchlist.json", watch)
    write_json(DATA / "peaks.json", {k: v for k, v in peaks.items() if k in watch})
    runs = read_json(DATA / "runs.json", [])
    runs.append({"at": now.isoformat(), "rpc_calls": rpc.calls, "sol_usd": sol_usd, **stats})
    write_json(DATA / "runs.json", runs[-3000:])
    log.info("Run done: %s (rpc calls: %d)", stats, rpc.calls)
    try:  # wallet credibility: refresh a few tracked wallets' win rates each cycle
        from .profile import refresh_profiles
        refresh_profiles(rpc, ds, cfg, DATA, sol_usd)
    except Exception:
        log.exception("wallet profiling failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
