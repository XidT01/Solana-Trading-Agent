"""Find wallets that bought under $30K mcap on coins that peaked at $100K+, and made >= 5x (10x+ tagged).

Everything is derived from raw Solana transactions (balance deltas), so it works the
same for pump.fun bonding curves, PumpSwap, Raydium, Meteora or any other venue.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import time

from .common import WSOL, log
from .integrity import bundled_launch, wallet_passes
from .sources import SolanaRPC

DUST = 1e-9
# Trades smaller than this are ignored for pricing: network fees and account rent (~0.002 SOL)
# dominate them, e.g. a 25-token "buy" costing 0.002 SOL would imply a $5M market cap.
MIN_PRICED_SOL = 0.02
WINDOW = 5  # trades per median when deciding when a coin reached the target


@dataclass
class Trade:
    sig: str
    slot: int
    time: int
    wallet: str
    token_delta: float      # + bought / received, - sold / sent
    sol_delta: float        # SOL (incl. wSOL) change for the wallet, network fee excluded
    token_account: str | None = None

    @property
    def is_buy(self) -> bool:
        return self.token_delta > 0 and self.sol_delta < 0

    @property
    def is_sell(self) -> bool:
        return self.token_delta < 0 and self.sol_delta > 0

    def price_sol(self) -> float | None:
        if abs(self.token_delta) < DUST or abs(self.sol_delta) < MIN_PRICED_SOL:
            return None
        return abs(self.sol_delta) / abs(self.token_delta)


def _keys(tx: dict) -> list[str]:
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def _owner_token_amount(balances: list[dict], owner: str, mint: str) -> tuple[float, int | None]:
    total, idx = 0.0, None
    for b in balances or []:
        if b.get("owner") == owner and b.get("mint") == mint:
            ui = b.get("uiTokenAmount") or {}
            total += float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
            idx = b.get("accountIndex")
    return total, idx


def signer_trade(sig: str, tx: dict | None, mint: str, quotes: dict[str, float] | None = None) -> Trade | None:
    """The fee payer's token/SOL change in this transaction, or None if it didn't touch `mint`.

    `quotes` maps other quote-asset mints (USDC, tokenized stocks on stonk.fun, ...) to their value in SOL
    per unit, so trades paid in those assets get a SOL-equivalent `sol_delta`."""
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    meta = tx["meta"]
    keys = _keys(tx)
    signer = keys[0]
    pre_t, _ = _owner_token_amount(meta.get("preTokenBalances"), signer, mint)
    post_t, idx = _owner_token_amount(meta.get("postTokenBalances"), signer, mint)
    if idx is None:
        _, idx = _owner_token_amount(meta.get("preTokenBalances"), signer, mint)
    tok = post_t - pre_t
    if abs(tok) < DUST:
        return None
    pre_w, _ = _owner_token_amount(meta.get("preTokenBalances"), signer, WSOL)
    post_w, _ = _owner_token_amount(meta.get("postTokenBalances"), signer, WSOL)
    lamports = meta["postBalances"][0] - meta["preBalances"][0] + meta.get("fee", 0)
    other = 0.0
    for q, sol_per_unit in (quotes or {}).items():
        pre_q, _ = _owner_token_amount(meta.get("preTokenBalances"), signer, q)
        post_q, _ = _owner_token_amount(meta.get("postTokenBalances"), signer, q)
        other += (post_q - pre_q) * sol_per_unit
    return Trade(sig=sig, slot=tx.get("slot", 0), time=tx.get("blockTime") or 0, wallet=signer,
                 token_delta=tok, sol_delta=lamports / 1e9 + (post_w - pre_w) + other,
                 token_account=keys[idx] if idx is not None and idx < len(keys) else None)


class TokenAnalyzer:
    def __init__(self, rpc: SolanaRPC, cfg: dict):
        self.rpc = rpc
        self.cfg = cfg
        self.w = cfg["wallets"]

    def mcap(self, price_sol: float | None) -> float | None:
        return price_sol * self.sol_usd * self.supply if price_sol else None

    def _robust_cross(self, trades: list[Trade]) -> int | None:
        """Time the market cap first reached the target, judged by the median of 5 consecutive priced
        trades. Single trades can't be trusted: launch transactions (setup fees + a tiny dev buy) and
        bundles (one wallet paying for several) imply absurd prices."""
        priced = [(t.time, m) for t in trades if (t.is_buy or t.is_sell) and (m := self.mcap(t.price_sol()))]
        k = WINDOW
        for i in range(len(priced) - k + 1):
            window = sorted(m for _, m in priced[i:i + k])
            if window[k // 2] >= self.w["target_mcap_usd"]:
                return priced[i + k // 2][0]
        return None

    def _mcap_at_index(self, sigs: list[dict], i: int) -> float | None:
        """Median market cap of the first few priced trades at or after signature index i."""
        mcaps = []
        for j in range(i, min(i + 12, len(sigs))):
            t = signer_trade(sigs[j]["signature"], self.rpc.get_transaction(sigs[j]["signature"]), self.mint,
                             self.quotes)
            if t and (t.is_buy or t.is_sell) and (m := self.mcap(t.price_sol())):
                mcaps.append(m)
                if len(mcaps) == WINDOW:
                    break
        return sorted(mcaps)[len(mcaps) // 2] if mcaps else None

    def _find_target_time(self, sigs: list[dict], early: list[Trade]) -> int | None:
        target = self.w["target_mcap_usd"]
        cross = self._robust_cross(early)
        if cross is not None:
            return cross
        start = min(self.early_count, len(sigs))
        if start >= len(sigs):
            return None
        # Coarse sampling over the rest of history, then binary search inside the bracket.
        step = max(1, (len(sigs) - start) // 40)
        lo, hi = start, None
        for i in range(start, len(sigs), step):
            m = self._mcap_at_index(sigs, i)
            if m and m >= target:
                hi = i
                break
            lo = i
        if hi is None:
            return None
        while hi - lo > 1:
            mid = (lo + hi) // 2
            m = self._mcap_at_index(sigs, mid)
            if m and m >= target:
                hi = mid
            else:
                lo = mid
        return sigs[hi].get("blockTime")

    def analyze(self, mint: str, supply: float, sol_usd: float, price_sol_now: float,
                quotes: dict[str, float] | None = None, target_time_hint: int | None = None) -> dict:
        """`target_time_hint`: when the coin first reached the target market cap, from price candles.
        Without it, the crossing is searched for on-chain."""
        self.mint, self.supply, self.sol_usd, self.quotes = mint, supply, sol_usd, quotes or {}
        sigs, complete = self.rpc.signatures_asc(mint, self.w["max_signature_pages"])
        if not complete or not sigs:
            return {"status": "skipped", "reason": "history_too_long_or_unavailable"}
        age_h = (time.time() - (sigs[0].get("blockTime") or time.time())) / 3600
        if age_h > self.cfg["discovery"]["max_token_age_hours"]:
            # e.g. VEYL: launched a week earlier, revived with a new pool that looked young
            return {"status": "skipped", "reason": f"launched_{age_h / 24:.0f}d_ago"}

        # Read the first transactions in batches until the coin is clearly past the entry market cap.
        early_sigs, txs, early = [], {}, []
        stop_mcap = self.w["max_entry_mcap_usd"] * 1.5
        limit = min(len(sigs), self.w["max_early_txs"])
        for i in range(0, limit, 100):
            chunk = [s["signature"] for s in sigs[i:min(i + 100, limit)]]
            got = self.rpc.get_transactions(chunk)
            txs.update(got)
            early_sigs += chunk
            early += [t for s in chunk if (t := signer_trade(s, got[s], mint, self.quotes))]
            recent = [m for t in early[-10:] if (t.is_buy or t.is_sell) and (m := self.mcap(t.price_sol()))]
            if len(recent) >= 5 and min(recent) > stop_mcap:
                break
        self.early_count = len(early_sigs)
        first_tx = txs.get(early_sigs[0])
        creator = _keys(first_tx)[0] if first_tx else None
        launch_slot = sigs[0].get("slot")

        if target_time_hint:
            early_cross = self._robust_cross(early)
            t_target = min(target_time_hint, early_cross) if early_cross else target_time_hint
            if sigs[0].get("blockTime") and t_target < sigs[0]["blockTime"]:
                return {"status": "skipped", "reason": "target_time_before_launch", "creator": creator}
        else:
            t_target = self._find_target_time(sigs, early)
        if t_target is None:
            return {"status": "skipped", "reason": "target_mcap_time_not_found", "creator": creator}

        # Candidates: early buyers below the entry cap, before the token hit the target.
        cands: dict[str, Trade] = {}
        for t in early:
            if not t.is_buy or t.time > t_target or -t.sol_delta < self.w["min_buy_sol"]:
                continue
            if self.w["exclude_creator"] and t.wallet == creator:
                continue
            if self.w["exclude_same_slot_as_launch"] and t.slot == launch_slot:
                continue
            m = self.mcap(t.price_sol())
            if m and m < self.w["max_entry_mcap_usd"] and t.wallet not in cands:
                cands[t.wallet] = t
        cand_list = sorted(cands.values(), key=lambda t: t.time)[:self.w["max_candidates_per_token"]]

        results = [r for t in cand_list if (r := self._wallet_result(t, t_target, price_sol_now))]
        launch_time = sigs[0].get("blockTime")
        n = bundled_launch(results, launch_time, self.cfg)
        if n:
            return {"status": "rejected", "creator": creator,
                    "reason": f"bundled_launch ({n} winners within {self.cfg['integrity']['bundle_window_seconds']}s of launch)"}
        qualified = [r for r in results if r["qualified"]]
        log.info("%s: %d early buyers <$%dK, %d qualified", mint[:8], len(cand_list),
                 self.w["max_entry_mcap_usd"] // 1000, len(qualified))
        return {"status": "analyzed", "creator": creator, "launch_time": sigs[0].get("blockTime"),
                "target_time": t_target, "early_buyers_checked": len(cand_list),
                "wallets": qualified, "near_misses": sorted(
                    [r for r in results if not r["qualified"]], key=lambda r: -r["multiple"])[:5]}

    def _wallet_trades(self, first: Trade) -> list[Trade]:
        trades = {first.sig: first}
        if first.token_account:
            page = self.rpc.call("getSignaturesForAddress", [first.token_account, {"limit": 1000}]) or []
            sigs = [s["signature"] for s in reversed(page) if s.get("err") is None][:self.w["max_txs_per_wallet"]]
            for s, tx in self.rpc.get_transactions(sigs).items():
                t = signer_trade(s, tx, self.mint, self.quotes)
                if t and t.wallet == first.wallet:
                    trades[s] = t
                elif tx and t is None:
                    # Someone else signed and tokens left this wallet (e.g. a delegated transfer).
                    # Incoming free tokens are ignored so they can't inflate the multiple.
                    delta = _owner_delta(tx, first.wallet, self.mint)
                    if delta < -DUST:
                        trades[s] = Trade(s, tx.get("slot", 0), tx.get("blockTime") or 0,
                                          first.wallet, delta, 0.0)
        return sorted(trades.values(), key=lambda t: (t.slot, t.time))

    def _wallet_result(self, first: Trade, t_target: int, price_sol_now: float) -> dict | None:
        trades = self._wallet_trades(first)
        sol_in = sum(-t.sol_delta for t in trades if t.is_buy)
        if sol_in <= 0:
            return None
        sol_out = sum(t.sol_delta for t in trades if t.is_sell)
        bought_pre = sum(t.token_delta for t in trades if t.is_buy and t.time <= t_target)
        sol_pre = sum(-t.sol_delta for t in trades if t.is_buy and t.time <= t_target)
        transferred_out = sum(-t.token_delta for t in trades if t.token_delta < 0 and not t.is_sell)

        bal = peak = 0.0
        for t in trades:
            if t.time > t_target:
                break
            bal += t.token_delta
            peak = max(peak, bal)
        hold_frac = bal / peak if peak > 0 else 0.0
        remaining = max(0.0, sum(t.token_delta for t in trades))
        unrealised = remaining * price_sol_now
        multiple = (sol_out + (unrealised if self.w["pnl_mode"] == "total" else 0)) / sol_in
        avg_entry = self.mcap(sol_pre / bought_pre) if bought_pre > 0 else None
        first_entry = self.mcap(first.price_sol())

        result = {"transferred_out": transferred_out > DUST}
        qualified = (avg_entry is not None and avg_entry < self.w["max_entry_mcap_usd"]
                     and hold_frac >= self.w["min_hold_fraction_at_target"]
                     and multiple >= self.w["min_multiple"] and wallet_passes(result, self.cfg))
        return {
            "wallet": first.wallet, "qualified": qualified,
            "first_buy_time": first.time, "first_buy_sig": first.sig,
            "first_entry_mcap": round(first_entry or 0), "avg_entry_mcap": round(avg_entry or 0),
            "hold_fraction_at_target": round(hold_frac, 3),
            "sol_in": round(sol_in, 4), "sol_out": round(sol_out, 4),
            "unrealised_sol": round(unrealised, 4), "pnl_sol": round(sol_out + unrealised - sol_in, 4),
            "multiple": round(multiple, 2), "realised_multiple": round(sol_out / sol_in, 2),
            "total_multiple": round((sol_out + unrealised) / sol_in, 2),
            "tier": ("10x+" if multiple >= self.w["highlight_multiple"]
                     else f"{self.w['min_multiple']:g}x+" if multiple >= self.w["min_multiple"] else None),
            "still_holding_pct": round(100 * remaining / max(peak, remaining, DUST), 1),
            "transferred_out": transferred_out > DUST, "n_trades": len(trades),
        }


def _owner_delta(tx: dict, owner: str, mint: str) -> float:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return 0.0
    pre, _ = _owner_token_amount(meta.get("preTokenBalances"), owner, mint)
    post, _ = _owner_token_amount(meta.get("postTokenBalances"), owner, mint)
    return post - pre


__all__ = ["Trade", "TokenAnalyzer", "signer_trade", "asdict"]
