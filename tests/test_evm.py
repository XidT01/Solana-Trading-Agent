"""Offline tests for the EVM engine: a simulated launch on a bonding curve, run through the real code."""
import re
import pytest

from agent.common import load_config
from agent.evm import (TRANSFER, EvmAnalyzer, PriceSeries, detect_launchpad, detect_markets, extract_trades,
                       parse_transfers)
from agent.evm_safety import goplus_checks, honeypot_is_checks

T0 = 1_800_000_000  # launch time (block 100); 1 block per second
SUPPLY, DEC = 1_000_000_000, 18
NATIVE_USD = 600.0  # BNB price in the simulation
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
            h = self.tx(who, (BOND, ROUTER, tokens), (ROUTER, who, tokens), block=b)
        else:
            moves = [(BOND, who, tokens)] + ([(BOND, FEE, tokens * fee)] if fee else [])
            h = self.tx(who, *moves, block=b)
        self.txs[h]["value"] = hex(int(usd / NATIVE_USD * 1e18))  # paid in BNB
        return h

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
    c = load_config()
    c["wallets"]["pnl_mode"] = "total"  # the mechanics tests below value held tokens too
    return c


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


def test_coin_launched_above_entry_cap_is_skipped(cfg):
    """Regression (GLDSTN on Base): a Zora coin launched at ~$1M, so no wallet could enter below $30K."""
    flat = PriceSeries([[[T0 + 60 * i, 1e-3, 0, 0, 1e-3, 1] for i in range(30)]])
    res = EvmAnalyzer(FakeRPC(launch()), cfg).analyze("0xtoken", {"tx": "0x0", "block": 99}, SUPPLY, DEC, flat,
                                                      1e-3, head=10 ** 6)
    assert res["status"] == "skipped" and res["reason"].startswith("launched_above_entry_cap")


def test_launch_contract_is_not_a_holder(cfg):
    """Regression (GM on BSC): GoPlus listed the Flap curve, holding 100% of supply, as a wallet."""
    gp = {"holders": [{"address": "0x9E7Ce8434D1633aa3c23fdb18f03ceca1934375a", "percent": "1.0", "is_contract": "0"}]}
    assert goplus_checks(gp, cfg) == ["concentrated_holders_top10_100pct"]
    assert goplus_checks(gp, cfg, {"0x9e7ce8434d1633aa3c23fdb18f03ceca1934375a"}) == []


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
    # o1 Launchpad (PENNY on Base): its factory is the creation tx's target
    o1 = "0x53c59d8d335681d58f564cf62073fa3c57cf81ee"
    assert detect_launchpad("0xeb3841d05d2ad5d941abd0defafd41a8dda19801", base, {"uniswap-v4-base"}, cr, R(o1))[0] == "o1"
    assert detect_launchpad("0xabc", rh, {"o1-launchpad-robinhood"}, None, R(None))[0] == "o1"


def test_evm_safety_checks(cfg):
    assert goplus_checks({"is_honeypot": "1", "sell_tax": "0.25", "buy_tax": "0.01"}, cfg) == ["honeypot", "sell_tax_25pct"]
    assert goplus_checks({"is_mintable": "1", "owner_address": "0x0000000000000000000000000000000000000000"}, cfg) == []
    assert goplus_checks({"is_mintable": "1", "owner_address": "0xabc"}, cfg) == ["mintable_by_owner"]
    holders = [{"address": f"0x{i}", "percent": "0.05", "is_contract": "0"} for i in range(10)]
    holders.insert(0, {"address": "0xpool", "percent": "0.6", "is_contract": "1"})
    assert goplus_checks({"holders": holders}, cfg) == ["concentrated_holders_top10_50pct"]
    assert honeypot_is_checks({"honeypotResult": {"isHoneypot": True}, "simulationResult": {"sellTax": 30}}, cfg) == [
        "honeypot_simulation_failed_sell", "simulated_sellTax_30pct"]


def _report(chain, new, leaderboard=(), runs=3):
    return {"chain": chain, "stats": {"runs": runs, "analyzed": 2, "rejected": 1, "wallets_found": len(new)},
            "new_wallets": list(new), "leaderboard": list(leaderboard), "passed_tokens": [], "ai_summary": None}


def test_telegram_digest(cfg, monkeypatch):
    from html.parser import HTMLParser
    from agent import digest
    sol_hit = {"wallet": "2Tp9scbrD3mrzFCQexmw56QY19bZ7VtRydAqkXUB8D65", "mint": "GHtnAtTCPFz8MRTMQiEyuEob283EUgqd1d1Fysnhpump",
               "symbol": "VE<YL>‮", "multiple": 36.12, "avg_entry_mcap": 4946, "pnl_sol": 49.81}
    bsc_hit = {"wallet": "0x" + "ab" * 20, "mint": "0x" + "12" * 18 + "4444", "symbol": "DOG", "multiple": 6.2,
               "avg_entry_mcap": 21000, "pnl_usd": 1234.5}
    lb = [{"wallet": "0x" + "ab" * 20, "hits": 2, "tokens": ["DOG", "CAT"], "median_multiple": 7.0,
           "last_hit": "2026-11-12T10:00:00+00:00"}]
    reports = {"solana": _report("solana", [sol_hit]), "bsc": _report("bsc", [bsc_hit], lb), "base": None,
               "robinhood": _report("robinhood", [], runs=0)}
    msgs = digest.build_messages("2026-11-12", reports, cfg,
                                 {sol_hit["wallet"]: {"coins_traded": 29, "win_rate": 0.241, "wins": 7}})
    text = "\n".join(msgs)
    assert "2 new wallets today, 1 at 10x+" in re.sub("<[^>]+>", "", text)
    assert "https://gmgn.ai/sol/token/GHtnAtTCPFz8MRTMQiEyuEob283EUgqd1d1Fysnhpump" in text
    assert "https://gmgn.ai/bsc/address/0x" + "ab" * 20 in text
    assert "VE&lt;YL&gt;" in text and "‮" not in text      # escaped, bidi stripped
    assert "+49.8 SOL · WR 24% (29)" in text and "+$1.2K" in text
    assert "Repeat winner" in text and "No scans today" in text

    class Check(HTMLParser):  # Telegram only accepts a few tags, and they must be balanced
        stack, bad = [], []
        def handle_starttag(self, tag, attrs):
            (self.stack.append(tag) if tag in ("b", "i", "a") else self.bad.append(tag))
        def handle_endtag(self, tag):
            assert self.stack.pop() == tag
    for m in msgs:
        p = Check(); p.feed(m)
        assert not p.stack and not p.bad and len(m) <= 4096

    sent = []

    class Resp:
        def __init__(self, ok): self.ok, self.status_code, self.text = ok, 200 if ok else 400, "can't parse entities"
    def fake_post(url, json, timeout):
        sent.append(json)
        return Resp(len(sent) > 1)  # first (HTML) attempt fails -> plain-text fallback
    monkeypatch.setattr(digest.requests, "post", fake_post)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert digest.send(["<b>hi</b> &amp; bye"], "https://example.org/")
    assert sent[0]["parse_mode"] == "HTML" and sent[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://example.org/"
    assert "parse_mode" not in sent[1] and sent[1]["text"] == "hi & bye"


def test_evm_realised_mode_and_transfer_out(cfg):
    """Default rules: only sold profits count, and wallets passing tokens on are excluded."""
    cfg["wallets"]["pnl_mode"] = "realised"
    c = launch()
    alpha_tokens = 200 / price_at(T0 + 60)
    c.sell(addr("ALPHA"), alpha_tokens / 2, T0 + 1600)            # ALPHA now sold everything at $150K
    omega_tokens = 120 / price_at(T0 + 150)
    c.buy(addr("OMEGA"), 120, T0 + 150)
    c.tx(addr("OMEGA"), (addr("OMEGA"), addr("SIDE"), omega_tokens / 2), block=100 + 400)  # passes half on
    c.sell(addr("OMEGA"), omega_tokens / 2, T0 + 1700)
    res = EvmAnalyzer(FakeRPC(c), cfg).analyze(
        "0xtoken", {"tx": "0x" + "%064x" % 0, "block": 99}, SUPPLY, DEC, prices(), price_at(T0 + 3000), head=10 ** 6)
    q = {w["wallet"]: w for w in res["wallets"]}
    assert set(q) == {addr("ALPHA")}                               # DELTA holds (paper), OMEGA transferred out
    assert q[addr("ALPHA")]["multiple"] == q[addr("ALPHA")]["realised_multiple"]
    near = {w["wallet"]: w for w in res["near_misses"]}
    assert near[addr("OMEGA")]["transferred_out"] and near[addr("DELTA")]["realised_multiple"] == 0


def test_curve_phase_priced_from_native_paid(cfg):
    """Regression (Flap on BSC): GeckoTerminal doesn't track the bonding curve, so candles only start at
    graduation (~$34-50K). Early buys must be priced from the BNB the buyer paid, not the first candle."""
    late = [[t, price_at(t), 0, 0, price_at(t + 60), 1000] for t in range(T0 + 600, T0 + 3600, 60)]
    c = launch()
    blind = EvmAnalyzer(FakeRPC(c), cfg).analyze(
        "0xtoken", {"tx": "0x" + "%064x" % 0, "block": 99}, SUPPLY, DEC, PriceSeries([late]), price_at(T0 + 3000),
        head=10 ** 6)
    assert blind["status"] == "skipped" and blind["reason"].startswith("launched_above_entry_cap")
    res = EvmAnalyzer(FakeRPC(c), cfg, native_usd=NATIVE_USD).analyze(
        "0xtoken", {"tx": "0x" + "%064x" % 0, "block": 99}, SUPPLY, DEC, PriceSeries([late]), price_at(T0 + 3000),
        head=10 ** 6)
    q = {w["wallet"]: w for w in res["wallets"]}
    assert set(q) == {addr("ALPHA"), addr("DELTA")}
    assert q[addr("ALPHA")]["avg_entry_mcap"] == pytest.approx(mcap_at(T0 + 60), rel=0.05)
    assert q[addr("DELTA")]["avg_entry_mcap"] == pytest.approx(mcap_at(T0 + 90), rel=0.05)


def test_estimated_peak_from_price_changes():
    from agent.safety import estimated_peak
    # $40K now, up 100% in 1h, down 75% over 6h (so $160K 6h ago), no 24h data
    pair = {"marketCap": 40000, "priceChange": {"h1": 100, "h6": -75}}
    assert estimated_peak(pair) == pytest.approx(160000)
    assert estimated_peak({"marketCap": 40000, "priceChange": {"h24": -100}}) == 40000  # ignore -100%


def test_incomplete_history_is_not_analysed(cfg):
    """Regression (Jacket on BSC): the public node returned no logs for the first hours, so the agent analysed
    only the coin's later life (first 'buy' 15h after launch, at $328K)."""
    c = launch()

    class Pruned(FakeRPC):
        def logs(self, address, topics, start, end, max_logs=10 ** 9):
            return [lg for lg in super().logs(address, topics, start, end) if int(lg["blockNumber"], 16) > 400]
    res = EvmAnalyzer(Pruned(c), cfg).analyze("0xtoken", {"tx": "0x" + "%064x" % 0, "block": 99}, SUPPLY, DEC,
                                                prices(), price_at(T0 + 3000), head=10 ** 6)
    assert res == {"status": "skipped", "reason": "incomplete_history_from_rpc"}


def test_empty_log_chunks_are_retried():
    from agent.evm import EvmRPC
    rpc = EvmRPC("http://fake", rps=0, chunk=100, empty_retries=3)
    answers = iter([[], [], [{"blockNumber": "0x5"}]])
    rpc.call = lambda m, p: next(answers) if m == "eth_getLogs" else None
    rpc.block_ts = lambda n: 0
    assert rpc.logs("0xt", [], 0, 50) == [{"blockNumber": "0x5"}]
