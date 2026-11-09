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
    c.trade("ALPHA", 130, 2000, -10.0, 60000)   # ALPHA exits: 10 SOL back
    c.trade("HALF", 131, 2100, -4.444, 40000)   # HALF exits at 40K
    return c


@pytest.fixture
def cfg():
    return load_config()


@pytest.mark.parametrize("max_early", [300, 5])  # 5 forces the sampled/binary search path
def test_wallet_analysis(cfg, max_early):
    cfg["wallets"]["max_early_txs"] = max_early
    c = launch()
    res = TokenAnalyzer(c.rpc(), cfg).analyze(MINT, SUPPLY, SOL_USD, price_sol_at(55000))
    assert res["status"] == "analyzed"
    assert res["creator"] == "DEV"
    assert res["target_time"] == 1600
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
    assert near["PAPER"]["hold_fraction_at_target"] == pytest.approx(0, abs=0.01)


def test_unrealised_counts_in_total_mode(cfg):
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
    assert launch_origin("abcpump", []) == "pumpfun"
    assert launch_origin("abc", [{"dexId": "meteora", "pairCreatedAt": 1}, {"dexId": "raydium", "pairCreatedAt": 5}]) == "meteora"
    m = {"liquidity_usd": 5000, "mcap_usd": 80000, "buys_24h": 300, "sells_24h": 0}
    r = market_checks(m, cfg)
    assert any("low_liquidity" in x for x in r) and "no_sells_possible_honeypot" in r
    info = {"program": "TokenzQdBNbLqP5VEhdkAS6EF4tbsScdEnpz8iEEyBF", "mint_authority": None,
            "freeze_authority": "X", "extensions": ["transferHook", "metadataPointer"]}
    r = onchain_checks(info, cfg)
    assert "freeze_authority_enabled_honeypot" in r and "token2022_risky_extensions:transferHook" in r
    rep = {"score_normalised": 80, "risks": [{"name": "Mutable metadata", "level": "warn"}],
           "topHolders": [{"owner": "POOL", "pct": 60}] + [{"owner": f"h{i}", "pct": 4} for i in range(10)],
           "knownAccounts": {"POOL": {"type": "AMM"}}}
    r = rugcheck_checks(rep, "pair", cfg)
    assert r == ["rugcheck_score_80", "concentrated_holders_top10_40pct"]
    assert rugcheck_checks(None, None, cfg) == []


def test_campaign_window(cfg):
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    assert in_campaign_window(cfg, at("2026-11-15T15:00"))[0]
    assert not in_campaign_window(cfg, at("2026-11-15T05:00"))[0]
    assert not in_campaign_window(cfg, at("2026-12-10T15:00"))[0]


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
    assert report_mod.main(["--date", "2026-11-10"]) == 0
    md = (tmp_path / "reports" / "2026-11-10.md").read_text()
    assert "`ALPHA` | AAA | $6,000 | 10.0x" in md
    rep = report_mod.read_json(tmp_path / "reports" / "2026-11-10.json", {})
    assert dict(rep["rejection_reasons"]) == {"low_liquidity": 1, "rugcheck_score": 1}
    assert rep["leaderboard"][0]["hits"] == 1


def test_scan_end_to_end(cfg, tmp_path, monkeypatch):
    """Full scan cycle with fake market APIs + the simulated chain."""
    from agent import scan as scan_mod
    chain = launch()
    now_ms = datetime(2026, 11, 15, 15, tzinfo=timezone.utc).timestamp() * 1000

    def pair(mint, dex, liq, mcap, sells=50):
        return {"baseToken": {"address": mint, "symbol": mint[:4]}, "quoteToken": {"symbol": "SOL"},
                "dexId": dex, "pairAddress": "P" + mint, "priceUsd": str(55000 / SUPPLY),
                "priceNative": str(price_sol_at(55000)), "marketCap": mcap, "liquidity": {"usd": liq},
                "pairCreatedAt": now_ms - 3.6e6, "txns": {"h24": {"buys": 500, "sells": sells}},
                "volume": {"h24": 1e5}}

    pairs = {MINT: [pair(MINT, "pumpswap", 30000, 55000)],
             "RUG": [pair("RUG", "raydium", 30000, 90000)],
             "HONEY": [pair("HONEY", "meteora", 40000, 70000, sells=0)],
             "OTHER": [pair("OTHER", "orca", 50000, 90000)]}

    class FakeDS:
        def latest_solana_mints(self): return list(pairs)
        def pairs_for(self, mints): return {m: pairs.get(m, []) for m in mints}
        def sol_usd(self): return SOL_USD

    class FakeGT:
        def new_pools(self, pages): return []

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
    assert scan_mod.main(["--force"]) == 0

    tokens = scan_mod.read_json(tmp_path / "tokens.json", {})
    assert tokens[MINT]["verdict"] == "analyzed" and tokens[MINT]["origin"] == "pumpfun"
    assert tokens["RUG"]["reasons"] == ["rugcheck_score_90"]
    assert tokens["HONEY"]["reasons"] == ["no_sells_possible_honeypot"]
    assert "OTHER" not in tokens  # orca launch filtered out
    wallets = scan_mod.read_json(tmp_path / "wallets.json", {})
    assert list(wallets) == ["ALPHA"]
