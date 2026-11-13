"""Wallet credibility: win rate across ALL coins a tracked Solana wallet traded recently, not just the coin
that got it tracked.

For each tracked wallet (re-profiled at most once a day), read its recent transactions and work out, per coin:
SOL spent buying, SOL received selling, and the value of anything still held at today's price.
A coin is a win when sold + still-held value > spent.

Run:  SOLANA_RPC_URL=... python -m agent.profile
Writes data/wallet_profiles.json.
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import timedelta

from .common import DATA, WSOL, load_config, log, read_json, setup_logging, utcnow, write_json
from .sources import DexScreener, SolanaRPC

STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
MIN_TRADE_SOL = 0.01  # ignore dust (fees and rent dominate)


def _keys(tx: dict) -> list[str]:
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def wallet_flows(tx: dict | None, wallet: str, sol_usd: float) -> tuple[float, dict[str, float]] | None:
    """(SOL change, {mint: token change}) for `wallet` in one transaction; network fee excluded,
    wSOL and USD stablecoins folded into the SOL change."""
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    meta, keys = tx["meta"], _keys(tx)
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        if i == 0:
            sol += meta.get("fee", 0) / 1e9
    deltas: dict[str, float] = defaultdict(float)
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") == wallet:
                ui = b.get("uiTokenAmount") or {}
                deltas[b["mint"]] += sign * float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
    sol += deltas.pop(WSOL, 0.0)
    for s in STABLES:
        if s in deltas:
            sol += deltas.pop(s) / sol_usd
    return sol, {m: d for m, d in deltas.items() if abs(d) > 1e-9}


def profile_from_txs(wallet: str, txs: list[dict], price_sol: dict[str, float], sol_usd: float) -> dict:
    """Per-coin PnL from a wallet's transactions; price_sol = current price (SOL per token) per mint."""
    coins: dict[str, dict] = {}
    first_ts = last_ts = None
    for tx in txs:
        f = wallet_flows(tx, wallet, sol_usd)
        if not f:
            continue
        sol, deltas = f
        if len(deltas) != 1 or abs(sol) < MIN_TRADE_SOL:
            continue  # transfers, multi-token swaps, dust
        (mint, tok), = deltas.items()
        ts = tx.get("blockTime") or 0
        first_ts = min(first_ts or ts, ts)
        last_ts = max(last_ts or ts, ts)
        c = coins.setdefault(mint, {"sol_in": 0.0, "sol_out": 0.0, "tokens": 0.0, "first": ts})
        if tok > 0 and sol < 0:
            c["sol_in"] += -sol
            c["tokens"] += tok
        elif tok < 0 and sol > 0:
            c["sol_out"] += sol
            c["tokens"] += tok
        c["first"] = min(c["first"], ts)
    rois, pnl, realised, wins, big = [], 0.0, 0.0, 0, 0
    traded = {m: c for m, c in coins.items() if c["sol_in"] >= MIN_TRADE_SOL}  # coins it actually bought
    for m, c in traded.items():
        value = c["sol_out"] + max(0.0, c["tokens"]) * price_sol.get(m, 0.0)
        roi = value / c["sol_in"]
        rois.append(roi)
        pnl += value - c["sol_in"]
        realised += c["sol_out"] - c["sol_in"]
        wins += roi > 1
        big += roi >= 2
    n = len(traded)
    return {
        "coins_traded": n, "wins": wins, "losses": n - wins,
        "win_rate": round(wins / n, 3) if n else None, "big_wins": big,
        "median_roi": round(statistics.median(rois), 2) if rois else None,
        "pnl_sol": round(pnl, 2), "realised_pnl_sol": round(realised, 2),
        "days_covered": round(((last_ts or 0) - (first_ts or 0)) / 86400, 1) if n else 0,
        "txs_read": len(txs),
    }


def current_prices(ds: DexScreener, mints: list[str], sol_usd: float) -> dict[str, float]:
    out = {}
    pairs = ds.pairs_for(mints)
    for m, ps in pairs.items():
        if not ps:
            continue
        best = max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        quote = (best.get("quoteToken") or {}).get("symbol", "").upper()
        if quote in ("SOL", "WSOL") and best.get("priceNative"):
            out[m] = float(best["priceNative"])
        elif best.get("priceUsd"):
            out[m] = float(best["priceUsd"]) / sol_usd
    return out


def profile_wallet(rpc: SolanaRPC, ds: DexScreener, wallet: str, sol_usd: float, cfg: dict) -> dict:
    p = cfg["profiles"]
    cutoff = time.time() - p["lookback_days"] * 86400
    sigs, before = [], None
    while len(sigs) < p["max_txs"]:
        opts = {"limit": min(1000, p["max_txs"] - len(sigs))}
        if before:
            opts["before"] = before
        page = rpc.call("getSignaturesForAddress", [wallet, opts]) or []
        page = [s for s in page if (s.get("blockTime") or 0) >= cutoff]
        sigs += [s for s in page if s.get("err") is None]
        if len(page) < opts["limit"]:
            break
        before = page[-1]["signature"]
    txs = [t for t in rpc.get_transactions([s["signature"] for s in sigs]).values() if t]
    mints = {m for t in txs if (f := wallet_flows(t, wallet, sol_usd)) for m in f[1]}
    prof = profile_from_txs(wallet, txs, current_prices(ds, sorted(mints), sol_usd), sol_usd)
    return {**prof, "profiled_at": utcnow().isoformat()}


def refresh_profiles(rpc: SolanaRPC, ds: DexScreener, cfg: dict, data=DATA, sol_usd: float | None = None) -> int:
    """Profile the tracked wallets whose profile is missing or stale (a few per call)."""
    p = cfg["profiles"]
    wallets = read_json(data / "wallets.json", {})
    profiles = read_json(data / "wallet_profiles.json", {})
    stale = (utcnow() - timedelta(hours=p["refresh_hours"])).isoformat()
    todo = sorted((w for w in wallets if (profiles.get(w) or {}).get("profiled_at", "") < stale),
                  key=lambda w: (profiles.get(w) or {}).get("profiled_at", ""))[:p["max_wallets_per_run"]]
    if not todo:
        log.info("All %d wallet profiles are fresh", len(wallets))
        return 0
    sol_usd = sol_usd or ds.sol_usd() or 0
    if not sol_usd:
        log.error("Could not fetch SOL/USD price")
        return 1
    for w in todo:
        try:
            profiles[w] = profile_wallet(rpc, ds, w, sol_usd, cfg)
            log.info("profiled %s: %s", w[:8], profiles[w])
        except Exception:
            log.exception("profiling %s failed", w[:8])
    profiles = {w: v for w, v in profiles.items() if w in wallets}  # drop wallets no longer tracked
    write_json(data / "wallet_profiles.json", profiles)
    log.info("Profiled %d wallets", len(todo))
    return 0


def main(argv=None) -> int:
    setup_logging()
    cfg = load_config()
    if not os.environ.get("SOLANA_RPC_URL"):
        log.error("SOLANA_RPC_URL is not set")
        return 2
    rpc = SolanaRPC(os.environ["SOLANA_RPC_URL"], cfg["rpc"]["requests_per_second"])
    return refresh_profiles(rpc, DexScreener(), cfg)


if __name__ == "__main__":
    sys.exit(main())
