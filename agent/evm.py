"""EVM chains (BSC, Base, Robinhood Chain): read a token's transfer history from a public RPC, price it
with 1-minute candles, and find wallets that bought early and made the minimum multiple.

The approach is launchpad-agnostic: every buy and sell, whether on a bonding curve (four.meme, Pons, ...)
or a Uniswap / PancakeSwap pool, moves the token between a "market" contract and a wallet. Markets are
detected as the addresses that trade with many different counterparties.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass

from .common import Http, log

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = {ZERO, "0x000000000000000000000000000000000000dead"}


def topic_addr(t: str) -> str:
    return "0x" + t[-40:].lower()


class EvmRPC:
    def __init__(self, url: str, rps: float = 8, chunk: int = 50000):
        self.url = url
        self.http = Http(rps=rps, timeout=40)
        self.chunk = chunk
        self.calls = 0
        self._block_ts: dict[int, int] = {}

    def call(self, method: str, params: list):
        self.calls += 1
        res = self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not res:
            return None
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error']}")
        return res.get("result")

    def head(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_ts(self, n: int) -> int:
        if n not in self._block_ts:
            b = self.call("eth_getBlockByNumber", [hex(n), False])
            self._block_ts[n] = int(b["timestamp"], 16)
        return self._block_ts[n]

    def ts_of(self, n: int) -> int:
        """Block timestamp, interpolated between fetched anchor blocks (block times are near-constant)."""
        if n in self._block_ts:
            return self._block_ts[n]
        known = sorted(self._block_ts)
        i = bisect.bisect_left(known, n)
        if 0 < i < len(known):
            a, b = known[i - 1], known[i]
        elif len(known) >= 2:
            a, b = (known[0], known[1]) if i == 0 else (known[-2], known[-1])
        else:
            return self.block_ts(n)
        ta, tb = self._block_ts[a], self._block_ts[b]
        return int(ta + (tb - ta) * (n - a) / (b - a))

    def block_at(self, ts: int, head: int | None = None) -> int:
        head = head or self.head()
        back = min(200000, head - 1)
        th, tb = self.block_ts(head), self.block_ts(head - back)
        bt = (th - tb) / back
        return max(0, head - int((th - ts) / bt))

    def logs(self, address: str, topics: list, start: int, end: int, max_logs: int = 80000) -> list[dict] | None:
        """All logs in [start, end], chunked; halves the chunk when the node refuses. None if too many."""
        out, b, size = [], start, self.chunk
        while b <= end:
            e = min(end, b + size - 1)
            try:
                got = self.call("eth_getLogs", [{"address": address, "topics": topics,
                                                 "fromBlock": hex(b), "toBlock": hex(e)}])
            except RuntimeError as err:
                if size <= 500:
                    log.warning("getLogs keeps failing at %d: %s", b, err)
                    return None
                size //= 2
                continue
            if got is None:
                return None
            out.extend(got)
            if len(out) > max_logs:
                return None
            self.block_ts(e)  # anchor for timestamp interpolation
            b = e + 1
        return out

    def eth_call(self, to: str, data: str) -> str | None:
        try:
            return self.call("eth_call", [{"to": to, "data": data}, "latest"])
        except RuntimeError:
            return None

    def erc20(self, token: str) -> tuple[float, int] | None:
        """(total supply in whole tokens, decimals)."""
        dec, sup = self.eth_call(token, "0x313ce567"), self.eth_call(token, "0x18160ddd")
        if not dec or not sup or dec == "0x" or sup == "0x":
            return None
        d = int(dec, 16)
        return int(sup, 16) / 10 ** d, d

    def tx(self, h: str) -> dict | None:
        return self.call("eth_getTransactionByHash", [h])

    def receipt(self, h: str) -> dict | None:
        return self.call("eth_getTransactionReceipt", [h])


# ---------------------------------------------------------------- creation & launchpad

def find_creation(rpc: EvmRPC, token: str, created_ts: int, head: int) -> dict | None:
    """The token's mint (Transfer from 0x0) near its first pool's creation time: tx, block, minter."""
    guess = rpc.block_at(created_ts, head)
    span = int(3 * 3600 / max(0.05, (rpc.block_ts(head) - rpc.block_ts(max(1, head - 200000))) / 200000))
    for lo, hi in ((guess - span, guess + span // 6), (guess - 8 * span, guess - span)):
        found = rpc.logs(token, [TRANSFER, "0x" + "0" * 64], max(0, lo), min(head, hi), max_logs=50)
        if found:
            first = min(found, key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16)))
            return {"tx": first["transactionHash"], "block": int(first["blockNumber"], 16),
                    "minted_to": topic_addr(first["topics"][2])}
    return None


def detect_launchpad(token: str, chain: dict, gt_dexes: set[str], creation: dict | None, rpc: EvmRPC) -> tuple[str, str | None]:
    """(launchpad name or 'other', factory address). Uses GeckoTerminal dex labels, then known launchpad
    contracts touched by the creation tx, then vanity address endings."""
    lp = chain["launchpads"]
    for dex in gt_dexes:
        if dex in (lp.get("gt_dex") or {}):
            return lp["gt_dex"][dex], None
    contracts = {k.lower(): v for k, v in (lp.get("contracts") or {}).items()}
    factory = None
    if creation:
        tx = rpc.tx(creation["tx"]) or {}
        factory = (tx.get("to") or "").lower() or None
        touched = {factory, creation.get("minted_to")}
        rc = rpc.receipt(creation["tx"]) or {}
        touched |= {(lg.get("address") or "").lower() for lg in rc.get("logs") or []}
        for addr in touched:
            if addr in contracts:
                return contracts[addr], factory
    for suffix, name in (lp.get("suffixes") or {}).items():
        if token.lower().endswith(suffix.lower()):
            return name, factory
    return "other", factory


# ---------------------------------------------------------------- prices

class PriceSeries:
    """USD price of the token per minute, merged across its pools (highest-volume pool wins each minute)."""

    def __init__(self, candles_by_pool: list[list[list[float]]]):
        best: dict[int, tuple[float, float, float]] = {}
        for candles in candles_by_pool:
            for ts, o, _h, _l, c, v in candles:
                ts = int(ts)
                if c and (ts not in best or v > best[ts][2]):
                    best[ts] = (o or c, c, v)
        self.ts = sorted(best)
        self.op = [best[t][0] for t in self.ts]
        self.px = [best[t][1] for t in self.ts]

    def __bool__(self):
        return bool(self.ts)

    def at(self, t: int) -> float | None:
        """Price at time t: interpolated between the open and close of the minute containing t (so a buy
        early in a fast-rising minute isn't charged the minute's close). Outside known minutes, the
        nearest earlier close (or the first open)."""
        if not self.ts:
            return None
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return self.op[0]
        frac = (t - self.ts[i]) / 60
        if frac > 1:
            return self.px[i]
        return self.op[i] + (self.px[i] - self.op[i]) * frac

    def cross_time(self, threshold_price: float, k: int = 5) -> int | None:
        """End of the minute where the median of k consecutive closes first reached the threshold."""
        for i in range(len(self.px) - k + 1):
            window = sorted(self.px[i:i + k])
            if window[k // 2] >= threshold_price:
                return self.ts[i + k // 2] + 60
        return None


def fetch_prices(gt, network: str, token: str, pools: list[str], since: int, until: int) -> PriceSeries:
    series = []
    for pool in pools[:3]:
        candles, before = [], until + 60
        for _ in range(6):  # up to 6000 minutes (~4 days)
            got = gt.ohlcv_minutes(network, pool, token, before)
            if not got:
                break
            candles += got
            before = int(min(c[0] for c in got))
            if before <= since:
                break
        series.append(candles)
    return PriceSeries(series)


# ---------------------------------------------------------------- trades

@dataclass
class EvmTrade:
    tx: str
    block: int
    time: int
    wallet: str
    tokens: float      # + received, - sent
    kind: str          # buy | sell | in | out

    @property
    def is_buy(self):
        return self.kind == "buy"

    @property
    def is_sell(self):
        return self.kind == "sell"


def parse_transfers(logs: list[dict], decimals: int) -> list[tuple]:
    out = []
    for lg in logs:
        if len(lg.get("topics") or []) < 3:
            continue
        out.append((int(lg["blockNumber"], 16), int(lg["logIndex"], 16), lg["transactionHash"],
                    topic_addr(lg["topics"][1]), topic_addr(lg["topics"][2]),
                    int(lg["data"], 16) / 10 ** decimals if lg.get("data") not in (None, "0x") else 0.0))
    out.sort()
    return out


def detect_markets(transfers: list[tuple], known: set[str] = frozenset(), min_counterparties: int = 5) -> set[str]:
    """Contracts that trade with many different addresses: bonding curves, pools, the v4 PoolManager.
    `known` market contracts (e.g. the chain's Uniswap v4 PoolManager) always count, even on quiet coins."""
    peers: dict[str, set] = defaultdict(set)
    for _b, _i, _tx, frm, to, _v in transfers:
        if frm not in DEAD and to not in DEAD:
            peers[frm].add(to)
            peers[to].add(frm)
    n_tx = len({t[2] for t in transfers})
    need = max(min_counterparties, int(0.03 * n_tx))
    return {a for a, p in peers.items() if len(p) >= need} | {k.lower() for k in known}


def extract_trades(transfers: list[tuple], markets: set[str], ts_of) -> list[EvmTrade]:
    """Net token flow per transaction: wallets gaining tokens while a market loses them are buyers,
    and vice versa. Routers/intermediaries net to zero and drop out."""
    by_tx: dict[str, list] = defaultdict(list)
    for t in transfers:
        by_tx[t[2]].append(t)
    trades = []
    for txh, items in by_tx.items():
        delta: dict[str, float] = defaultdict(float)
        for _b, _i, _tx, frm, to, v in items:
            delta[frm] -= v
            delta[to] += v
        block = items[0][0]
        market_flow = sum(v for a, v in delta.items() if a in markets)
        wallets = {a: v for a, v in delta.items() if a not in markets and a not in DEAD and abs(v) > 1e-12}
        # Only the biggest receiver of a buy (sender of a sell) is the trader; smaller flows in the same tx
        # are fee/tax wallets and count as plain transfers.
        top_in = max((a for a, v in wallets.items() if v > 0), key=lambda a: wallets[a], default=None)
        top_out = min((a for a, v in wallets.items() if v < 0), key=lambda a: wallets[a], default=None)
        for addr, v in wallets.items():
            if v > 0:
                kind = "buy" if market_flow < 0 and addr == top_in else "in"
            else:
                kind = "sell" if market_flow > 0 and addr == top_out else "out"
            trades.append(EvmTrade(txh, block, ts_of(block), addr, v, kind))
    trades.sort(key=lambda t: (t.block, t.tx))
    return trades


# ---------------------------------------------------------------- analysis

class EvmAnalyzer:
    def __init__(self, rpc: EvmRPC, cfg: dict, known_markets: list[str] = ()):
        self.rpc = rpc
        self.known_markets = [m.lower() for m in known_markets]
        self.w = cfg["wallets"]
        self.e = cfg["evm"]

    def analyze(self, token: str, creation: dict, supply: float, decimals: int, prices: PriceSeries,
                price_now: float, head: int, target_hint: int | None = None) -> dict:
        if not prices:
            return {"status": "skipped", "reason": "no_price_history"}
        start = max(0, creation["block"] - 5)
        logs = self.rpc.logs(token, [TRANSFER], start, head, self.e["max_transfer_logs"])
        if logs is None:
            return {"status": "skipped", "reason": "history_too_long_or_unavailable"}
        transfers = parse_transfers(logs, decimals)
        if not transfers:
            return {"status": "skipped", "reason": "no_transfers"}
        markets = detect_markets(transfers, set(self.known_markets))
        trades = extract_trades(transfers, markets, self.rpc.ts_of)
        creator = None
        try:
            creator = ((self.rpc.tx(creation["tx"]) or {}).get("from") or "").lower() or None
        except RuntimeError:
            pass
        buys = [t for t in trades if t.is_buy]
        if not buys:
            return {"status": "skipped", "reason": "no_buys_detected", "markets": sorted(markets)}
        launch_block = buys[0].block

        target_px = self.w["target_mcap_usd"] / supply
        cross = prices.cross_time(target_px)
        t_target = min(x for x in (cross, target_hint) if x) if (cross or target_hint) else None
        if not t_target:
            return {"status": "skipped", "reason": "target_mcap_time_not_found"}

        cands: dict[str, EvmTrade] = {}
        for t in buys:
            if t.time > t_target:
                break
            if self.w["exclude_creator"] and t.wallet == creator:
                continue
            if self.w["exclude_same_slot_as_launch"] and t.block == launch_block:
                continue
            px = prices.at(t.time)
            if not px or t.tokens * px < self.e["min_buy_usd"]:
                continue
            if px * supply < self.w["max_entry_mcap_usd"] and t.wallet not in cands:
                cands[t.wallet] = t
        cand_list = sorted(cands.values(), key=lambda t: t.block)[:self.w["max_candidates_per_token"]]

        by_wallet: dict[str, list[EvmTrade]] = defaultdict(list)
        wanted = {t.wallet for t in cand_list}
        for t in trades:
            if t.wallet in wanted:
                by_wallet[t.wallet].append(t)
        results = [self._wallet_result(t, by_wallet[t.wallet], t_target, prices, price_now, supply)
                   for t in cand_list]
        results = [r for r in results if r]
        qualified = [r for r in results if r["qualified"]]
        log.info("%s: %d markets, %d early buyers, %d qualified", token[:10], len(markets), len(cand_list), len(qualified))
        return {"status": "analyzed", "creator": creator, "launch_time": buys[0].time, "target_time": t_target,
                "early_buyers_checked": len(cand_list), "markets": sorted(markets)[:6], "wallets": qualified,
                "near_misses": sorted([r for r in results if not r["qualified"]], key=lambda r: -r["multiple"])[:5]}

    def _wallet_result(self, first: EvmTrade, trades: list[EvmTrade], t_target: int, prices: PriceSeries,
                       price_now: float, supply: float) -> dict | None:
        cost = sum(t.tokens * (prices.at(t.time) or 0) for t in trades if t.is_buy)
        if cost <= 0:
            return None
        proceeds = sum(-t.tokens * (prices.at(t.time) or 0) for t in trades if t.is_sell)
        bought_pre = sum(t.tokens for t in trades if t.is_buy and t.time <= t_target)
        cost_pre = sum(t.tokens * (prices.at(t.time) or 0) for t in trades if t.is_buy and t.time <= t_target)
        bal = peak = 0.0
        for t in trades:
            if t.time > t_target:
                break
            if t.kind != "in":
                bal += t.tokens
                peak = max(peak, bal)
        hold_frac = bal / peak if peak > 0 else 0.0
        # Tokens received for free don't count; tokens sent to another wallet count as given away.
        remaining = max(0.0, sum(t.tokens for t in trades if t.kind != "in"))
        unrealised = remaining * price_now
        multiple = (proceeds + (unrealised if self.w["pnl_mode"] == "total" else 0)) / cost
        avg_entry = cost_pre / bought_pre * supply if bought_pre > 0 else None
        qualified = (avg_entry is not None and avg_entry < self.w["max_entry_mcap_usd"]
                     and hold_frac >= self.w["min_hold_fraction_at_target"] and multiple >= self.w["min_multiple"])
        return {
            "wallet": first.wallet, "qualified": qualified,
            "first_buy_time": first.time, "first_buy_sig": first.tx,
            "first_entry_mcap": round((prices.at(first.time) or 0) * supply), "avg_entry_mcap": round(avg_entry or 0),
            "hold_fraction_at_target": round(hold_frac, 3),
            "usd_in": round(cost, 2), "usd_out": round(proceeds, 2), "unrealised_usd": round(unrealised, 2),
            "pnl_usd": round(proceeds + unrealised - cost, 2),
            "multiple": round(multiple, 2), "realised_multiple": round(proceeds / cost, 2),
            "tier": ("10x+" if multiple >= self.w["highlight_multiple"]
                     else f"{self.w['min_multiple']:g}x+" if multiple >= self.w["min_multiple"] else None),
            "still_holding_pct": round(100 * remaining / max(peak, remaining, 1e-12), 1),
            "transferred_out": any(t.kind == "out" for t in trades), "n_trades": len(trades),
        }
