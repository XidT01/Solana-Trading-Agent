"""Offline tests: a simulated pump.fun-style launch run through the real analysis code."""
import copy
from datetime import datetime, timezone

import pytest

from agent import report as report_mod
from agent.common import load_config
from agent.safety import launch_origin, market_checks, onchain_checks, rugcheck_checks
from agent.scan import in_campaign_window
from agent.sources import SolanaRPC
from agent.wallets import TokenAnalyzer, signer_trade

MINT = "MeMe111111111111111111111111111111111111pump"
SUPPLY = 1_000_000_000
SOL_USD = 200.0


def price_sol_at(mcap_usd):
    return mcap_usd / (SOL_USD * SUPPLY)


class Chain:
    """Builds jsonParsed-shaped transactions and serves them through a fake RPC."""

    def __init__(self):
        self.txs, self.order, self.balances, self.accounts = {}, [], {}, {}

    def trade(self, wallet, slot, t, sol, mcap):
        """sol > 0 = buy with that much SOL; sol < 0 = sell tokens worth |sol| at this mcap."""
        acct = self.accounts.setdefault(wallet, f"ata_{wallet}")
        tokens = abs(sol) / price_sol_at(mcap)
        pre = self.balances.get(wallet, 0.0)
        post = pre + tokens if sol > 0 else pre - tokens
        self.balances[wallet] = post
        lamport_delta = int(-sol * 1e9) - 5000
        sig = f"sig{len(self.order)}"
        bal = lambda amt: [{"accountIndex": 1, "mint": MINT, "owner": wallet,
                            "uiTokenAmount": {"uiAmountString": repr(amt)}}]
        self.txs[sig] = {
            "slot": slot, "blockTime": t,
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": acct}, {"pubkey": MINT}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [100_000_000_000, 0, 0],
                     "postBalances": [100_000_000_000 + lamport_delta, 0, 0],
                     "preTokenBalances": bal(pre), "postTokenBalances": bal(post)},
        }
        self.order.append((sig, wallet))
        return sig

    def rpc(self):
        chain = self

        class FakeRPC(SolanaRPC):
            def call(self, method, params):
                self.calls += 1
                if method == "getSignaturesForAddress":
                    addr = params[0]
                    sigs = [s for s, w in chain.order if addr == MINT or chain.accounts.get(w) == addr]
                    rows = [{"signature": s, "slot": chain.txs[s]["slot"], "blockTime": chain.txs[s]["blockTime"],
                             "err": None} for s in sigs]
                    return list(reversed(rows))  # RPC returns newest first
                if method == "getTransaction":
                    return copy.deepcopy(chain.txs[params[0]])
                raise AssertionError(method)

        return FakeRPC("http://fake", rps=0)


def launch():
    c = Chain()
    c.trade("DEV", 100, 1000, 2.0, 4000)        # creator
    c.trade("SNIPER", 100, 1000, 3.0, 4500)     # same slot as launch
    c.trade("ALPHA", 101, 1010, 1.0, 6000)      # good: holds, exits at 60K -> 10x
    c.trade("PAPER", 102, 1020, 1.0, 7000)      # sells before $50K
    c.trade("HALF", 103, 1030, 1.0, 9000)       # holds past $50K but only exits at 40K -> 4.4x
    c.trade("LATE", 104, 1040, 1.0, 15000)      # entry above $10K
    c.trade("DUST", 105, 1050, 0.01, 8000)      # dust buy
    c.trade("PAPER", 106, 1100, -2.857, 20000)  # PAPER sells everything at 20K
    for i, m in enumerate([25000, 30000, 38000, 45000, 49000]):
        c.trade(f"F{i}", 110 + i, 1200 + i * 60, 0.5, m)
    c.trade("CROSS", 120, 1600, 0.5, 52000)     # first trade at >= $50K
    for i, m in enumerate([55000, 58000, 62000, 65000]):  # the run continues above the target
        c.trade(f"G{i}", 121 + i, 1610 + i * 10, 0.5, m)
    c.trade("ALPHA", 130, 2000, -10.0, 60000)   # ALPHA exits: 10 SOL back
    c.trade("HALF", 131, 2100, -4.444, 40000)   # HALF exits at 40K
    return c


@pytest.fixture
def cfg():
    c = load_config()
    c["wallets"]["target_mcap_usd"] = 50000  # the simulated launch below is drawn on a $50K scale
    c["wallets"]["max_entry_mcap_usd"] = 10000  # ...with a $10K entry cap
    c["discovery"]["max_token_age_hours"] = 10 ** 9  # simulated block times are from 1970
    return c


@pytest.mark.parametrize("max_early", [300, 5])  # 5 forces the sampled/binary search path
def test_wallet_analysis(cfg, max_early):
    cfg["wallets"]["max_early_txs"] = max_early
    c = launch()
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["status"] == "analyzed"
    assert res["creator"] == "DEV"
    if max_early == 300:
        assert res["target_time"] == 1600
    else:  # found by sampling + binary search over medians: may land a trade or two early
        assert 1300 <= res["target_time"] <= 1600
    qualified = {w["wallet"]: w for w in res["wallets"]}
    if max_early == 5:
        # only the first 5 txs are scanned for early buyers: DEV, SNIPER, ALPHA, PAPER, HALF
        assert res["early_buyers_checked"] == 3
    assert set(qualified) == {"ALPHA"}
    a = qualified["ALPHA"]
    assert a["avg_entry_mcap"] == pytest.approx(6000, rel=0.01)
    assert a["multiple"] == pytest.approx(10.0, rel=0.01)
    assert a["hold_fraction_at_target"] == 1.0
    near = {w["wallet"]: w for w in res["near_misses"]}
    assert near["HALF"]["multiple"] == pytest.approx(4.44, rel=0.01)
    assert near["HALF"]["tier"] is None and qualified["ALPHA"]["tier"] == "10x+"
    assert near["PAPER"]["hold_fraction_at_target"] == pytest.approx(0, abs=0.01)


def test_unrealised_counts_in_total_mode(cfg):
    cfg["wallets"]["pnl_mode"] = "total"
    c = launch()
    sell = [s for s, w in c.order if w == "ALPHA"][-1]  # ALPHA never sells
    c.order = [o for o in c.order if o[0] != sell]
    del c.txs[sell]
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    alpha = next(w for w in res["wallets"] if w["wallet"] == "ALPHA")
    assert alpha["multiple"] == pytest.approx(55000 / 6000, rel=0.01)
    assert alpha["realised_multiple"] == 0
    cfg["wallets"]["pnl_mode"] = "realised"
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["wallets"] == []


def test_signer_trade_ignores_failed_and_unrelated():
    c = launch()
    tx = c.txs["sig2"]
    t = signer_trade("sig2", tx, MINT)
    assert t.is_buy and t.wallet == "ALPHA" and t.sol_delta == pytest.approx(-1.0)
    failed = copy.deepcopy(tx)
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert signer_trade("x", failed, MINT) is None
    assert signer_trade("sig2", tx, "OtherMint") is None


def test_safety_filters(cfg):
    assert launch_origin("abcpump", [], cfg) == "pumpfun"
    assert launch_origin("abc", [{"dexId": "meteora", "pairCreatedAt": 1}, {"dexId": "raydium", "pairCreatedAt": 5}], cfg) == "meteora"
    m = {"liquidity_usd": 5000, "mcap_usd": 80000, "buys_24h": 300, "sells_24h": 0}
    r = market_checks(m, cfg)
    assert any("low_liquidity" in x for x in r) and "no_sells_possible_honeypot" in r
    info = {"program": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "mint_authority": None,
            "freeze_authority": "X", "extensions": [
                {"extension": "transferHook", "state": {"programId": "Hook111", "authority": None}},
                {"extension": "metadataPointer", "state": {}}]}
    r = onchain_checks(info, cfg)
    assert "freeze_authority_enabled_honeypot" in r and "token2022_risky_extensions:transferHook" in r
    rep = {"score_normalised": 80, "risks": [{"name": "Mutable metadata", "level": "warn"}],
           "topHolders": [{"owner": "POOL", "pct": 60}] + [{"owner": f"h{i}", "pct": 4} for i in range(10)],
           "knownAccounts": {"POOL": {"type": "AMM"}},
           "markets": [{"pubkey": "M1", "liquidityA": "VAULT", "liquidityAAccount": {"owner": "AUTH"}}]}
    r = rugcheck_checks(rep, "pair", cfg)
    assert r == ["rugcheck_score_80", "concentrated_holders_top10_40pct"]
    assert rugcheck_checks(None, None, cfg) == []


def test_campaign_window(cfg):
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    assert in_campaign_window(cfg, at("2026-11-15T15:00"))[0]
    assert in_campaign_window(cfg, at("2026-11-15T05:00"))[0]  # around the clock
    cfg["campaign"]["scan_hours_utc"] = [13, 23]
    assert not in_campaign_window(cfg, at("2026-11-15T05:00"))[0]
    assert not in_campaign_window(cfg, at("2026-12-11T15:00"))[0]


def test_daily_report(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "DATA", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    hit = {"wallet": "ALPHA", "multiple": 10.0, "avg_entry_mcap": 6000, "pnl_sol": 9.0,
           "still_holding_pct": 0.0, "found_at": "2026-11-10T15:00:00+00:00"}
    from agent.common import write_json
    write_json(tmp_path / "tokens.json", {
        "M1": {"mint": "M1", "symbol": "AAA", "verdict": "analyzed", "checked_at": "2026-11-10T15:00:00",
               "reasons": [], "wallets": [hit]},
        "M2": {"mint": "M2", "symbol": "BBB", "verdict": "rejected", "checked_at": "2026-11-10T16:00:00",
               "reasons": ["low_liquidity ($3,000)", "rugcheck_score_77"]}})
    write_json(tmp_path / "wallets.json", {"ALPHA": {"hits": [{**hit, "mint": "M1", "symbol": "AAA"}]}})
    write_json(tmp_path / "runs.json", [{"at": "2026-11-10T15:00:00", "discovered": 40}])
    assert report_mod.main(["--date", "2026-11-10", "--force"]) == 0
    md = (tmp_path / "reports" / "2026-11-10.md").read_text()
    assert "`ALPHA` | AAA | $6,000 | 10.0x" in md
    rep = report_mod.read_json(tmp_path / "reports" / "2026-11-10.json", {})
    assert dict(rep["rejection_reasons"]) == {"low_liquidity": 1, "rugcheck_score": 1}
    assert rep["leaderboard"][0]["hits"] == 1


def test_scan_end_to_end(cfg, tmp_path, monkeypatch):
    """Full scan cycle with fake market APIs + the simulated chain."""
    import yaml
    from agent import scan as scan_mod
    chain = launch()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000

    def pair(mint, dex, liq, mcap, sells=50):
        return {"baseToken": {"address": mint, "symbol": mint[:4]}, "quoteToken": {"symbol": "SOL"},
                "dexId": dex, "pairAddress": "P" + mint, "priceUsd": str(mcap / SUPPLY),
                "priceNative": str(price_sol_at(mcap)), "marketCap": mcap, "liquidity": {"usd": liq},
                "pairCreatedAt": now_ms - 3.6e6, "txns": {"h24": {"buys": 500, "sells": sells}},
                "volume": {"h24": 1e5}}

    # MINT is at $40K now but peaked at $60K (above the $50K target) -> must still be analysed.
    # DUD peaked at $30K -> not analysed, stays on the watchlist.
    candles = {"P" + MINT: [[1200, 0, 30000 / SUPPLY, 0, 28000 / SUPPLY, 0], [1500, 0, 60000 / SUPPLY, 0, 55000 / SUPPLY, 0],
                            [1800, 0, 45000 / SUPPLY, 0, 40000 / SUPPLY, 0]],
               "PDUDpump": [[1200, 0, 30000 / SUPPLY, 0, 25000 / SUPPLY, 0]]}
    pairs = {MINT: [pair(MINT, "pumpswap", 30000, 40000)],
             "DUDpump": [pair("DUDpump", "pumpswap", 30000, 25000)],
             "RUG": [pair("RUG", "raydium", 30000, 90000)],
             "HONEY": [pair("HONEY", "meteora", 40000, 70000, sells=0)],
             "OTHER": [pair("OTHER", "orca", 50000, 90000)]}

    class FakeDS:
        def latest_solana_mints(self): return list(pairs)
        def pairs_for(self, mints): return {m: pairs.get(m, []) for m in mints}
        def sol_usd(self): return SOL_USD

    class FakeGT:
        def new_pools(self, pages): return []
        def ohlcv(self, pool, hours): return candles.get(pool, [])

    class FakeRC:
        def report(self, mint): return {"score_normalised": 90 if mint == "RUG" else 5, "risks": []}

    rpc = chain.rpc()
    rpc.mint_info = lambda m: {"program": "x", "supply": SUPPLY, "decimals": 6,
                               "mint_authority": None, "freeze_authority": None, "extensions": []}
    monkeypatch.setattr(scan_mod, "DATA", tmp_path)
    monkeypatch.setattr(scan_mod, "DexScreener", FakeDS)
    monkeypatch.setattr(scan_mod, "GeckoTerminal", FakeGT)
    monkeypatch.setattr(scan_mod, "RugCheck", FakeRC)
    monkeypatch.setattr(scan_mod, "SolanaRPC", lambda *a: rpc)
    monkeypatch.setenv("SOLANA_RPC_URL", "http://fake")
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg))
    assert scan_mod.main(["--force", "--config", str(cfg_file)]) == 0

    tokens = scan_mod.read_json(tmp_path / "tokens.json", {})
    assert tokens[MINT]["verdict"] == "analyzed" and tokens[MINT]["origin"] == "pumpfun"
    assert tokens["RUG"]["reasons"] == ["rugcheck_score_90"]
    assert tokens["HONEY"]["reasons"] == ["no_sells_possible_honeypot"]
    assert "OTHER" not in tokens  # orca launch filtered out
    wallets = scan_mod.read_json(tmp_path / "wallets.json", {})
    assert list(wallets) == ["ALPHA"]
    hit = wallets["ALPHA"]["hits"][0]
    assert hit["tier"] == "10x+" and hit["multiple"] == pytest.approx(10.0, rel=0.01)
    # candle starting at 1500 reached $60K -> crossing by its end (2400); the chain shows it at 1600
    assert tokens[MINT]["peak_mcap_usd"] == 55000 and tokens[MINT]["target_time"] == 1600
    assert "DUDpump" not in tokens
    assert "DUDpump" in scan_mod.read_json(tmp_path / "watchlist.json", {})


LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
STONK_PC = "StonkPlatformConfig1111111111111111111111111"
BONK_PC = "FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1"


def creation_tx(platform_config, via_cpi=False):
    ix = {"programId": LAUNCHLAB, "accounts": ["payer", "creator", "global", platform_config, "auth", "pool", "mint"]}
    keys = [{"pubkey": k} for k in ["payer", "creator", "global", platform_config, LAUNCHLAB]]
    if via_cpi:  # launched through the platform's own program, which CPIs into LaunchLab
        return {"transaction": {"message": {"accountKeys": keys,
                                            "instructions": [{"programId": "Wrapper", "accounts": []}]}},
                "meta": {"innerInstructions": [{"index": 0, "instructions": [ix]}]}}
    return {"transaction": {"message": {"accountKeys": keys, "instructions": [ix]}}, "meta": {}}


def test_stonk_fun_detection(cfg):
    from agent.safety import allowed_launch, launchlab_platform
    pairs = [{"dexId": "raydium", "pairCreatedAt": 1}]
    # before stonk.fun's platform config is pinned: still allowed, as a Raydium LaunchLab launch
    o = launch_origin("Rand0mMint", pairs, cfg, creation_tx(STONK_PC))
    assert o == "raydium-launchlab" and allowed_launch(o, cfg)
    assert launchlab_platform(creation_tx(STONK_PC), cfg) == STONK_PC
    # after pinning it in config.yaml
    cfg["launchpads"]["launchlab_platforms"][STONK_PC] = "stonk"
    for cpi in (False, True):
        o = launch_origin("Rand0mMint", pairs, cfg, creation_tx(STONK_PC, via_cpi=cpi))
        assert o == "stonk" and allowed_launch(o, cfg)
    # letsbonk (also LaunchLab) is not on the user's list
    o = launch_origin("Rand0mMint", pairs, cfg, creation_tx(BONK_PC))
    assert o == "letsbonk" and not allowed_launch(o, cfg)
    # Meteora DBC
    dbc = {"transaction": {"message": {"accountKeys": [], "instructions": [
        {"programId": cfg["launchpads"]["programs"]["meteora_dbc"], "accounts": []}]}}, "meta": {}}
    assert launch_origin("x", pairs, cfg, dbc) == "meteora-dbc"


def test_trade_paid_in_tokenized_stock():
    """stonk.fun lets launches quote in tokenized stocks: value them in SOL."""
    STOCK = "XStock1111111111111111111111111111111111111"
    tx = {"slot": 5, "blockTime": 10,
          "transaction": {"message": {"accountKeys": [{"pubkey": "W"}, {"pubkey": "ata"}, {"pubkey": "ata2"}]}},
          "meta": {"err": None, "fee": 5000, "preBalances": [10**9, 0, 0], "postBalances": [10**9 - 5000, 0, 0],
                   "preTokenBalances": [
                       {"accountIndex": 2, "mint": STOCK, "owner": "W", "uiTokenAmount": {"uiAmountString": "3"}}],
                   "postTokenBalances": [
                       {"accountIndex": 1, "mint": MINT, "owner": "W", "uiTokenAmount": {"uiAmountString": "1000000"}},
                       {"accountIndex": 2, "mint": STOCK, "owner": "W", "uiTokenAmount": {"uiAmountString": "1"}}]}}
    assert signer_trade("s", tx, MINT) is None or not signer_trade("s", tx, MINT).is_buy  # invisible without quotes
    t = signer_trade("s", tx, MINT, quotes={STOCK: 1.5})  # 1 stock = 1.5 SOL; paid 2 stock = 3 SOL
    assert t.is_buy and t.sol_delta == pytest.approx(-3.0)


def test_token2022_defaults_not_flagged(cfg):
    info = {"program": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "mint_authority": None, "freeze_authority": None,
            "extensions": [{"extension": "transferFeeConfig", "state": {
                "transferFeeConfigAuthority": None, "olderTransferFee": {"transferFeeBasisPoints": 0},
                "newerTransferFee": {"transferFeeBasisPoints": 0}}},
                {"extension": "metadataPointer", "state": {}}]}
    assert onchain_checks(info, cfg) == []
    info["extensions"][0]["state"]["newerTransferFee"]["transferFeeBasisPoints"] = 500
    assert onchain_checks(info, cfg) == ["token2022_risky_extensions:transferFeeConfig"]


def test_candle_cross_uses_candle_end():
    """Regression: a coin launched mid-candle must not get a target time before its launch."""
    from agent.scan import peak_from_candles
    launch = 1794337390  # 16:23:10, inside the candle that starts 16:15:00
    candles = [[1794336300, 0, 2e-4, 0, 1.5e-4, 0], [1794337200, 0, 3e-4, 0, 2.8e-4, 0]]
    peak, cross = peak_from_candles(candles, 1e9, 250000)
    assert peak == pytest.approx(280000) and cross == 1794337200 + 900 and cross > launch


def test_single_spike_is_not_a_peak():
    """Regression (USRP on Base): one odd trade spiked a 15-min high to $219K on a 7-transaction coin."""
    from agent.scan import peak_from_candles
    candles = [[0, 0, 30e-6, 0, 20e-6, 0], [900, 0, 219e-6, 0, 25e-6, 0], [1800, 0, 26e-6, 0, 24e-6, 0]]
    peak, cross = peak_from_candles(candles, 1e9, 100000)
    assert peak < 100000 and cross is None
    candles[2][2] = 120e-6  # a second candle also trades above: a real move
    peak, cross = peak_from_candles(candles, 1e9, 100000)
    assert peak == pytest.approx(219000) and cross == 1800 + 900


def test_dust_trade_does_not_fake_target_cross(cfg):
    """Regression (BUZZ): a 25-token buy costing 0.002 SOL (fees + rent) implied a $5M mcap at launch,
    so the coin 'reached the target' in the launch block and no early buyer could qualify."""
    c = launch()
    c.trade("DUSTBOT", 101, 1005, 0.002, 5_000_000)
    c.order.insert(2, c.order.pop())  # put it right after launch
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["target_time"] == 1600
    assert {w["wallet"] for w in res["wallets"]} == {"ALPHA"}


def test_launch_tx_outlier_does_not_fake_target_cross(cfg):
    """Regression (NOBUSHIBA/AIB/MAX): the dev's launch tx pays ~0.02-0.05 SOL in setup fees for a tiny buy,
    implying a huge market cap at launch. Passing the dust filter, it still faked an instant target cross."""
    c = launch()
    c.trade("BUNDLER", 100, 1000, 0.05, 2_000_000)  # in the launch block, priced at $2M
    c.order.insert(1, c.order.pop())
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["target_time"] >= 1500 and {w["wallet"] for w in res["wallets"]} == {"ALPHA"}
    hinted = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000), target_time_hint=2400)
    assert hinted["target_time"] >= 1500 and {w["wallet"] for w in hinted["wallets"]} == {"ALPHA"}


def test_paper_profits_do_not_count_by_default(cfg):
    """Regression (ape hao, VEYL): wallets that never sold looked like 6-36x winners until the coin rugged."""
    assert load_config()["wallets"]["pnl_mode"] == "realised"
    c = launch()
    sell = [s for s, w in c.order if w == "ALPHA"][-1]  # ALPHA never sells
    c.order = [o for o in c.order if o[0] != sell]
    del c.txs[sell]
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["wallets"] == []
    alpha = next(w for w in res["near_misses"] if w["wallet"] == "ALPHA")
    assert alpha["realised_multiple"] == 0 and alpha["total_multiple"] == pytest.approx(55000 / 6000, rel=0.01)


def test_revived_old_coin_is_skipped(cfg):
    """Regression (VEYL): launched a week earlier, revived with a new pool that looked young."""
    cfg["discovery"]["max_token_age_hours"] = 48
    res = TokenAnalyzer(launch().rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["status"] == "skipped" and res["reason"].startswith("launched_") and res["reason"].endswith("d_ago")


def test_bundled_launch_is_rejected(cfg):
    """Regression (BULLHARIS): 24 winners all bought 0-24s after launch, then passed tokens around."""
    c = launch()
    for i in range(6):  # six wallets buying within 20s of launch at $5-9K, all selling at $60K
        c.trade(f"B{i}", 100 + i + 1, 1001 + i * 3, 1.0, 5000 + i * 700)
    for i in range(6):
        c.trade(f"B{i}", 140 + i, 2050 + i, -(60000 / (5000 + i * 700)), 60000)
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["status"] == "rejected" and res["reason"].startswith("bundled_launch (7 winners")  # 6 + ALPHA at +10s


def test_insider_network_rejected(cfg):
    """Regression (mihao): RugCheck found a 237-wallet insider network."""
    rep = {"score_normalised": 1, "risks": [], "insiderNetworks": [{"size": 237}, {"size": 4}]}
    assert rugcheck_checks(rep, None, load_config()) == ["insider_network_237_wallets"]
    assert rugcheck_checks({"insiderNetworks": [{"size": 19}]}, None, load_config()) == []


def test_peak_checks_rotate(cfg, tmp_path, monkeypatch):
    """Regression (PENNY): peak checks always went to the biggest coins by current market cap, so a coin
    that had peaked above the target and fallen back was never checked."""
    import yaml
    from agent import scan as scan_mod
    chain = launch()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000

    def pair(mint, mcap):
        return {"baseToken": {"address": mint, "symbol": mint[:4]}, "quoteToken": {"symbol": "SOL"},
                "dexId": "pumpswap", "pairAddress": "P" + mint, "priceUsd": str(mcap / SUPPLY),
                "priceNative": str(price_sol_at(mcap)), "marketCap": mcap, "liquidity": {"usd": 30000},
                "pairCreatedAt": now_ms - 3.6e6, "txns": {"h24": {"buys": 500, "sells": 50}}, "volume": {"h24": 1e5}}

    pairs = {"BIGpump": [pair("BIGpump", 45000)], MINT: [pair(MINT, 30000)]}   # MINT is smaller now...
    candles = {"PBIGpump": [[1200, 0, 46000 / SUPPLY, 0, 45000 / SUPPLY, 0]],
               "P" + MINT: [[1500, 0, 60000 / SUPPLY, 0, 55000 / SUPPLY, 0]]}  # ...but peaked above $50K
    checked = []

    class FakeDS:
        def latest_solana_mints(self): return list(pairs)
        def pairs_for(self, mints): return {m: pairs.get(m, []) for m in mints}
        def sol_usd(self): return SOL_USD

    class FakeGT:
        def new_pools(self, pages): return []
        def ohlcv(self, pool, hours): checked.append(pool); return candles.get(pool, [])

    class FakeRC:
        def report(self, mint): return {"score_normalised": 5, "risks": []}

    rpc = chain.rpc()
    rpc.mint_info = lambda m: {"program": "x", "supply": SUPPLY, "decimals": 6,
                               "mint_authority": None, "freeze_authority": None, "extensions": []}
    for name, val in (("DATA", tmp_path), ("DexScreener", FakeDS), ("GeckoTerminal", FakeGT), ("RugCheck", FakeRC),
                      ("SolanaRPC", lambda *a: rpc)):
        monkeypatch.setattr(scan_mod, name, val)
    monkeypatch.setenv("SOLANA_RPC_URL", "http://fake")
    cfg["discovery"]["max_peak_checks_per_run"] = 1
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump(cfg))

    assert scan_mod.main(["--force", "--config", str(cfg_file)]) == 0
    assert checked == ["PBIGpump"] and MINT not in scan_mod.read_json(tmp_path / "tokens.json", {})
    assert scan_mod.main(["--force", "--config", str(cfg_file)]) == 0   # next scan: MINT's turn
    assert checked[1] == "P" + MINT
    tokens = scan_mod.read_json(tmp_path / "tokens.json", {})
    assert tokens[MINT]["verdict"] == "analyzed" and tokens[MINT]["peak_mcap_usd"] == 55000
    peaks = scan_mod.read_json(tmp_path / "peaks.json", {})
    assert "BIGpump" in peaks and MINT not in peaks   # only coins still on the watchlist are remembered
