"""Offline tests for the EVM engine: a simulated launch on a bonding curve, run through the real code."""
import pytest

from agent.common import load_config
from agent.evm import (TRANSFER, EvmAnalyzer, PriceSeries, detect_launchpad, detect_markets, extract_trades,
                       parse_transfers)
from agent.evm_safety import goplus_checks, honeypot_is_checks

T0 = 1_800_000_000  # launch time (block 100); 1 block per second
SUPPLY, DEC = 1_000_000_000, 18
BOND, ROUTER, FEE = "0x" + "b0" * 20, "0x" + "70" * 20, "0x" + "fe" * 20


def addr(name):
    return "0x" + name.encode().hex().ljust(40, "0")[:40]


def mcap_at(t):  # $5K at launch, linear to $200K after 20 minutes, then $150K
    dt = t - T0
    return 5000 + 195000 * dt / 1200 if dt < 1200 else 150000


def price_at(t):
    return mcap_at(t) / SUPPLY


def ts_of(block):
    return T0 + (block - 100)


class Chain:
    def __init__(self):
        self.logs, self.txs, self.n = [], {}, 0

    def tx(self, sender, *moves, block):
        """moves: (from, to, tokens). Returns tx hash."""
        h = "0x%064x" % self.n
        self.n += 1
        self.txs[h] = {"from": sender}
        for i, (frm, to, amt) in enumerate(moves):
            self.logs.append({"blockNumber": hex(block), "logIndex": hex(i), "transactionHash": h,
                              "topics": [TRANSFER, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]],
                              "data": hex(int(amt * 10 ** DEC))})
        return h

    def buy(self, who, usd, t, via_router=False, fee=0.0):
        tokens = usd / price_at(t)
        b = 100 + (t - T0)
        if via_router:
            return self.tx(who, (BOND, ROUTER, tokens), (ROUTER, who, tokens), block=b)
        moves = [(BOND, who, tokens)] + ([(BOND, FEE, tokens * fee)] if fee else [])
        return self.tx(who, *moves, block=b)

    def sell(self, who, tokens, t):
        return self.tx(who, (who, BOND, tokens), block=100 + (t - T0))


def launch():
    c = Chain()
    c.tx(addr("DEV"), ("0x" + "0" * 40, BOND, SUPPLY), block=99)            # mint to the bonding curve
    c.buy(addr("DEV"), 500, T0)                                            # dev buy in the launch block
    c.buy(addr("SNIPER"), 800, T0)                                         # same block: excluded
    alpha_tokens = 200 / price_at(T0 + 60)
    c.buy(addr("ALPHA"), 200, T0 + 60, fee=0.01)                           # 1% to a fee wallet
    c.buy(addr("DELTA"), 150, T0 + 90, via_router=True)                    # through a router
    gamma_tokens = 100 / price_at(T0 + 120)
    c.buy(addr("GAMMA"), 100, T0 + 120)
    c.buy(addr("BETA"), 300, T0 + 300)                                     # entry above $30K
    for i in range(12):                                                    # the crowd
        c.buy(addr(f"F{i}"), 50, T0 + 400 + i * 10)
    c.sell(addr("GAMMA"), gamma_tokens, T0 + 240)                          # sold too early: 1.8x
    c.sell(addr("ALPHA"), alpha_tokens / 2, T0 + 1500)                     # half at $150K, holds half
    return c


class FakeRPC:
    def __init__(self, chain):
        self.c = chain
        self.calls = 0

    def logs(self, address, topics, start, end, max_logs=10 ** 9):
        return [lg for lg in self.c.logs if start <= int(lg["blockNumber"], 16) <= end]

    def tx(self, h):
        return self.c.txs.get(h)

    def receipt(self, h):
        return {"logs": []}

    def ts_of(self, b):
        return ts_of(b)


def prices():
    candles = [[t, price_at(t), 0, 0, price_at(t + 60), 1000] for t in range(T0 - 60, T0 + 3600, 60)]
    return PriceSeries([candles])


@pytest.fixture
def cfg():
    return load_config()


def test_market_detection_and_trade_extraction():
    c = launch()
    transfers = parse_transfers(c.logs, DEC)
    markets = detect_markets(transfers)
    assert detect_markets(transfers[:3], {"0xPM"}) >= {"0xpm"}  # known markets always count
    assert BOND in markets and ROUTER not in markets and FEE not in markets
    trades = extract_trades(transfers, markets, ts_of)
    kinds = {(t.wallet, t.kind) for t in trades}
    assert (addr("DELTA"), "buy") in kinds          # routed buy credited to the wallet
    assert not any(t.wallet == ROUTER for t in trades)
    assert (FEE, "in") in kinds and (FEE, "buy") not in kinds  # fee wallet is not a buyer
    assert (addr("GAMMA"), "sell") in kinds


def test_evm_wallet_analysis(cfg):
    c = launch()
    res = EvmAnalyzer(FakeRPC(c), cfg).analyze(
        "0xtoken", {"tx": "0x" + "%064x" % 0, "block": 99}, SUPPLY, DEC, prices(), price_at(T0 + 3000), head=10 ** 6)
    assert res["status"] == "analyzed", res
    assert res["creator"] == addr("DEV")
    q = {w["wallet"]: w for w in res["wallets"]}
    assert set(q) == {addr("ALPHA"), addr("DELTA")}
    assert q[addr("ALPHA")]["tier"] == "10x+" and q[addr("ALPHA")]["multiple"] == pytest.approx(10.2, rel=0.05)
    assert q[addr("DELTA")]["tier"] == "5x+" and q[addr("DELTA")]["avg_entry_mcap"] == pytest.approx(19700, rel=0.05)
    near = {w["wallet"]: w for w in res["near_misses"]}
    assert near[addr("GAMMA")]["multiple"] == pytest.approx(1.8, rel=0.05)
    checked = {w["wallet"] for w in res["wallets"] + res["near_misses"]}
    assert addr("SNIPER") not in checked and addr("BETA") not in checked and FEE not in checked


def test_price_cross_ignores_single_spike():
    candles = [[T0 + 60 * i, 0, 0, 0, 1e-5, 1] for i in range(30)]
    candles[3][4] = 1e-3  # one absurd minute
    assert PriceSeries([candles]).cross_time(1e-4) is None
    for i in range(10, 30):
        candles[i][4] = 2e-4
    assert PriceSeries([candles]).cross_time(1e-4) == T0 + 60 * 10 + 60  # first minute of the sustained move


def test_launchpad_detection(cfg):
    bsc, base, rh = (cfg["evm"]["chains"][k] for k in ("bsc", "base", "robinhood"))

    class R:
        def __init__(self, to, emitters=()):
            self.to, self.emitters = to, emitters

        def tx(self, h):
            return {"to": self.to}

        def receipt(self, h):
            return {"logs": [{"address": a} for a in self.emitters]}

    cr = {"tx": "0x1", "block": 1, "minted_to": "0x" + "11" * 20}
    assert detect_launchpad("0xabc", bsc, {"four-meme"}, None, R(None))[0] == "four.meme"
    assert detect_launchpad("0xabc", rh, {"uniswap-pools-trade"}, None, R(None))[0] == "pools.trade"
    clanker = "0xE85A59c628F7d27878ACeB4bf3b35733630083a9"
    assert detect_launchpad("0xabc", base, {"uniswap-v4-base"}, cr, R(clanker)) == ("clanker", clanker.lower())
    # a trading bot creates the coin, but the Flap portal emits events in the same tx
    flap = "0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0"
    assert detect_launchpad("0xabc", bsc, set(), cr, R("0x" + "99" * 20, [flap]))[0] == "flap"
    assert detect_launchpad("0xdeadbeef7777", bsc, set(), cr, R("0x" + "99" * 20))[0] == "flap"
    assert detect_launchpad("0xabc", base, set(), cr, R("0x" + "99" * 20)) == ("other", "0x" + "99" * 20)


def test_evm_safety_checks(cfg):
    assert goplus_checks({"is_honeypot": "1", "sell_tax": "0.25", "buy_tax": "0.01"}, cfg) == ["honeypot", "sell_tax_25pct"]
    assert goplus_checks({"is_mintable": "1", "owner_address": "0x0000000000000000000000000000000000000000"}, cfg) == []
    assert goplus_checks({"is_mintable": "1", "owner_address": "0xabc"}, cfg) == ["mintable_by_owner"]
    holders = [{"address": f"0x{i}", "percent": "0.05", "is_contract": "0"} for i in range(10)]
    holders.insert(0, {"address": "0xpool", "percent": "0.6", "is_contract": "1"})
    assert goplus_checks({"holders": holders}, cfg) == ["concentrated_holders_top10_50pct"]
    assert honeypot_is_checks({"honeypotResult": {"isHoneypot": True}, "simulationResult": {"sellTax": 30}}, cfg) == [
        "honeypot_simulation_failed_sell", "simulated_sellTax_30pct"]
