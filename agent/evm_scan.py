"""One scan cycle on the EVM chains (BSC, Base, Robinhood Chain). Same rules as the Solana scan:
coins that peaked at $100K+, launched on an allowed launchpad, passed the safety checks; then wallets that
bought below the entry market cap and made the minimum multiple.

Run:  python -m agent.evm_scan [--chain bsc --chain base ...] [--force]
Data per chain goes to data/chains/<chain>/.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .common import ROOT, load_config, log, read_json, setup_logging, utcnow, write_json
from .evm import EvmAnalyzer, EvmRPC, detect_launchpad, fetch_prices, find_creation
from .evm_safety import evm_safety
from .safety import market_checks, market_snapshot
from .scan import in_campaign_window, peak_from_candles
from .sources import DexScreener, GeckoTerminal


def chain_dir(key: str) -> Path:
    return Path(os.environ.get("AGENT_EVM_DATA_DIR", ROOT / "data" / "chains")) / key


def scan_chain(key: str, chain: dict, cfg: dict, now, ds, gt, rpc=None) -> dict:
    d, e, w = cfg["discovery"], cfg["evm"], cfg["wallets"]
    target = w["target_mcap_usd"]
    out = chain_dir(key)
    tokens = read_json(out / "tokens.json", {})
    wallets = read_json(out / "wallets.json", {})
    seen = read_json(out / "seen.json", {})
    watch = read_json(out / "watchlist.json", {})
    cutoff = (now.date() - timedelta(days=3)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    today = now.date().isoformat()
    done_today = sum(1 for r in tokens.values()
                     if r.get("verdict") == "analyzed" and r.get("checked_at", "").startswith(today))
    paced = math.ceil(e["max_tokens_per_day"] * (now.hour + 1) / 24)
    budget = max(0, min(e["max_tokens_per_run"], paced - done_today))
    own_rpc = os.environ.get(f"EVM_RPC_{key.upper()}")
    rpc = rpc or EvmRPC(own_rpc or chain["rpc"], 8, chain["log_chunk_blocks"],
                        0 if own_rpc else chain.get("empty_log_retries", 0))
    net = chain["geckoterminal"]

    found = [t.lower() for t, _ in gt.new_pools(d["geckoterminal_new_pool_pages"], net)]
    if d["use_dexscreener_profiles"]:
        found += [t.lower() for t in ds.latest_solana_mints(chain["dexscreener"])]
    for t in found:
        if t not in tokens and t not in seen:
            watch.setdefault(t, now.isoformat())
    candidates = [t for t in watch if t not in tokens and t not in seen]
    pairs = ds.pairs_for(candidates, chain["dexscreener"])
    stats = {"discovered": len(set(found)), "watchlist": len(candidates), "launchpad_rejected": 0, "too_old": 0,
             "below_target_peak": 0, "market_rejected": 0, "safety_rejected": 0, "integrity_rejected": 0, "analyzed": 0, "skipped": 0,
             "wallets_found": 0}
    unknown_factories: Counter = Counter()

    snaps = {}
    for t in candidates:
        ps = pairs.get(t) or []
        watched_h = (now - datetime.fromisoformat(watch[t])).total_seconds() / 3600
        if not ps:
            if watched_h > d["max_token_age_hours"]:
                watch.pop(t, None)
            continue
        m = market_snapshot(ps)
        age_h = (now.timestamp() * 1000 - m["first_pair_ms"]) / 3.6e6 if m["first_pair_ms"] else watched_h
        if age_h > d["max_token_age_hours"]:
            stats["too_old"] += 1
            seen[t] = today
            watch.pop(t, None)
            continue
        never_got_going = (age_h > d["watch_drop_no_progress_hours"]
                           and max(m["mcap_usd"], m["est_peak_mcap"]) < d["peak_check_min_mcap_usd"])
        if never_got_going or (age_h > d["watch_drop_after_hours"] and m["mcap_usd"] < d["watch_drop_below_mcap_usd"]):
            seen[t] = today
            watch.pop(t, None)
            continue
        snaps[t] = (m, age_h)

    head = None
    # Peak checks are rate-limited, so rotate them: coins never checked first, then the longest-unchecked.
    # (Ordering by current market cap starved coins that had peaked and fallen back, e.g. PENNY.)
    # Each coin's last measured peak is remembered between scans.
    peaks = read_json(out / "peaks.json", {})
    peak_checks = 0
    shortlist = []
    # ...and among those, the likeliest runners (highest estimated peak) first.
    order = sorted(snaps.items(), key=lambda kv: ((peaks.get(kv[0]) or {}).get("at", ""), -kv[1][0]["est_peak_mcap"]))
    for t, (m, age_h) in order:
        if max(m["mcap_usd"], m["est_peak_mcap"]) < d["peak_check_min_mcap_usd"]:
            stats["below_target_peak"] += 1
            continue
        supply_est = m["mcap_usd"] / m["price_usd"] if m["price_usd"] else 0
        peak, cross_t = None, None
        if peak_checks < d["max_peak_checks_per_run"] and m["pair"]:
            peak_checks += 1
            peak, cross_t = peak_from_candles(gt.ohlcv(m["pair"], d["max_token_age_hours"] + 2, net), supply_est, target)
            peaks[t] = {"at": now.isoformat(), "peak": round(peak or 0), "cross": cross_t}
        elif peaks.get(t):
            peak, cross_t = peaks[t].get("peak"), peaks[t].get("cross")
        peak = max(peak or 0, m["mcap_usd"])
        if peak < target:
            stats["below_target_peak"] += 1
            continue
        pools = gt.token_pools(net, t)
        created = min([p["created_at"] for p in pools if p["created_at"]] or [int(now.timestamp() - age_h * 3600)])
        head = head or rpc.head()
        try:
            creation = find_creation(rpc, t, created, head)
            origin, factory = detect_launchpad(t, chain, {p["dex"] for p in pools if p["dex"]}, creation, rpc)
        except RuntimeError as err:
            log.warning("%s %s: creation lookup failed: %s", key, t, err)
            continue
        if origin not in chain["launchpads"]["allowed"]:
            stats["launchpad_rejected"] += 1
            if origin == "other" and factory:
                unknown_factories[factory] += 1
            seen[t] = today
            watch.pop(t, None)
            continue
        rec = {"mint": t, "chain": key, "origin": origin, "factory": factory, "age_hours": round(age_h, 1),
               "checked_at": now.isoformat(), "peak_mcap_usd": round(peak), "target_time_hint": cross_t,
               "creation": creation, "pools": [p["address"] for p in pools[:3]], **m}
        reasons = market_checks(m, cfg)
        if reasons:
            stats["market_rejected"] += 1
            tokens[t] = {**rec, "verdict": "rejected", "stage": "market", "reasons": reasons}
            watch.pop(t, None)
            continue
        shortlist.append(rec)

    shortlist.sort(key=lambda r: -r["volume_24h"])
    np_ = chain.get("native_price") or {}
    native_usd = ds.price_usd(np_["address"], np_["chain"]) if np_ else None
    stables = {a.lower() for a in chain.get("stablecoins") or []}
    quote_cache: dict[str, tuple[float | None, int]] = {}

    def token_usd(addr: str) -> tuple[float | None, int]:
        """USD price and decimals of a token a buyer paid with (USDT, tokenized stocks, WBNB / WETH...)."""
        if addr not in quote_cache:
            dec = 18
            try:
                d_hex = rpc.eth_call(addr, "0x313ce567")
                dec = int(d_hex, 16) if d_hex and d_hex != "0x" else 18
            except ValueError:
                pass
            quote_cache[addr] = (1.0 if addr in stables else ds.price_usd(addr, chain["dexscreener"]), dec)
        return quote_cache[addr]

    known = [*(chain.get("markets") or []), *(chain["launchpads"].get("contracts") or {})]
    analyzer = EvmAnalyzer(rpc, cfg, known, native_usd, token_usd)
    for rec in shortlist:
        if budget <= 0:
            break
        t = rec["mint"]
        watch.pop(t, None)
        not_holders = {(rec["creation"] or {}).get("minted_to"), rec["factory"], t,
                       *(chain["launchpads"].get("contracts") or {}), *(chain.get("markets") or [])}
        reasons, coverage = evm_safety(chain, t, cfg, not_holders)
        rec["security_scan"] = coverage
        if reasons:
            stats["safety_rejected"] += 1
            tokens[t] = {**rec, "verdict": "rejected", "stage": "safety", "reasons": reasons}
            continue
        try:
            erc = rpc.erc20(t)
            if not erc or not rec["creation"]:
                raise RuntimeError("token metadata or creation tx unavailable")
            supply, decimals = erc
            created_ts = rpc.ts_of(rec["creation"]["block"])
            prices = fetch_prices(gt, net, t, rec["pools"], created_ts, int(now.timestamp()))
            res = analyzer.analyze(t, rec["creation"], supply, decimals, prices, rec["price_usd"], head,
                                   rec["target_time_hint"])
            if not (res.get("reason") or "").startswith("launched_above_entry_cap"):
                budget -= 1  # only full wallet scans count against the daily budget
        except Exception as err:  # one bad token must not kill the run
            log.exception("%s analysis failed for %s", key, t)
            res = {"status": "skipped", "reason": f"error: {err}"}
            budget -= 1
        for entry in wallets.values():  # the latest verdict on a coin replaces its earlier wallet hits
            entry["hits"] = [h for h in entry["hits"] if h["mint"] != t]
        if res["status"] != "analyzed":
            rejected = res["status"] == "rejected"
            stats["integrity_rejected" if rejected else "skipped"] += 1
            tokens[t] = {**rec, "verdict": "rejected" if rejected else "skipped",
                            "stage": "integrity" if rejected else "wallets", "reasons": [res["reason"]]}
            continue
        stats["analyzed"] += 1
        stats["wallets_found"] += len(res["wallets"])
        tokens[t] = {**rec, "verdict": "analyzed", "stage": "wallets", "reasons": [], "supply": supply, **res}
        for hit in res["wallets"]:
            entry = wallets.setdefault(hit["wallet"], {"hits": []})
            entry["hits"] = [h for h in entry["hits"] if h["mint"] != t] + [
                {**hit, "mint": t, "symbol": rec["symbol"], "origin": rec["origin"], "found_at": now.isoformat()}]

    # Hits only stand for coins whose latest verdict is "analyzed" (a coin rejected later, e.g. by a new
    # safety rule on re-check, takes its wallets' hits with it).
    not_ok = {m for m, r in tokens.items() if r.get("verdict") != "analyzed"}
    for entry in wallets.values():
        entry["hits"] = [h for h in entry["hits"] if h["mint"] not in not_ok]
    wallets = {a: w for a, w in wallets.items() if w["hits"]}
    if len(watch) > d["watchlist_max"]:
        watch = dict(sorted(watch.items(), key=lambda kv: kv[1])[-d["watchlist_max"]:])
    write_json(out / "tokens.json", tokens)
    write_json(out / "wallets.json", wallets)
    write_json(out / "seen.json", seen)
    write_json(out / "watchlist.json", watch)
    write_json(out / "peaks.json", {k: v for k, v in peaks.items() if k in watch})
    runs = read_json(out / "runs.json", [])
    runs.append({"at": now.isoformat(), "rpc_calls": rpc.calls, **stats,
                 "unknown_factories": dict(unknown_factories.most_common(5))})
    write_json(out / "runs.json", runs[-3000:])
    log.info("%s run done: %s (rpc calls: %d)", key, stats, rpc.calls)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", action="append", help="bsc, base, robinhood (default: all)")
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
    ds, gt = DexScreener(), GeckoTerminal()
    failures = 0
    for key in args.chain or list(cfg["evm"]["chains"]):
        try:
            scan_chain(key, cfg["evm"]["chains"][key], cfg, now, ds, gt)
        except Exception:
            failures += 1
            log.exception("%s scan failed", key)
    return 1 if failures == len(args.chain or cfg["evm"]["chains"]) else 0


if __name__ == "__main__":
    sys.exit(main())
