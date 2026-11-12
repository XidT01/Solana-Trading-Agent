"""Filters 1 & 2: launch platform, liquidity, honeypot and scam checks."""
from __future__ import annotations

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


# ---------------------------------------------------------------- launch platform

def _instructions(tx: dict) -> list[tuple[str, list[str]]]:
    """(program id, account list) for every top-level and inner (CPI) instruction."""
    out = []
    ixs = list(tx["transaction"]["message"].get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs += inner.get("instructions") or []
    for ix in ixs:
        accounts = ix.get("accounts") or []
        out.append((ix.get("programId"), [a["pubkey"] if isinstance(a, dict) else a for a in accounts]))
    return out


def _all_keys(tx: dict) -> set[str]:
    return {k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]}


def launchlab_platform(tx: dict | None, cfg: dict) -> str | None:
    """The LaunchLab platform-config account used by the creation tx (identifies stonk.fun, letsbonk, ...)."""
    if not tx:
        return None
    lp = cfg["launchpads"]
    keys = _all_keys(tx)
    for addr in lp.get("launchlab_platforms") or {}:
        if addr in keys:
            return addr
    for program, accounts in _instructions(tx):
        if program == lp["programs"]["launchlab"] and len(accounts) > 3:
            return accounts[3]  # initialize / buy / sell: payer, creator|authority, global_config, platform_config
    return None


def launch_origin(mint: str, pairs: list[dict], cfg: dict, creation_tx: dict | None = None) -> str:
    lp = cfg["launchpads"]
    progs = lp["programs"]
    if mint.endswith("pump"):
        return "pumpfun"
    if creation_tx:
        programs = {p for p, _ in _instructions(creation_tx)}
        if progs["pumpfun"] in programs:
            return "pumpfun"
        if progs["launchlab"] in programs:
            pc = launchlab_platform(creation_tx, cfg)
            return (lp.get("launchlab_platforms") or {}).get(pc) or lp["unknown_launchlab_origin"]
        if progs["meteora_dbc"] in programs:
            return "meteora-dbc"
    dated = [p for p in pairs if p.get("pairCreatedAt")]
    oldest = min(dated, key=lambda p: p["pairCreatedAt"]) if dated else (pairs[0] if pairs else {})
    return (oldest.get("dexId") or "unknown").lower()


def allowed_launch(origin: str, cfg: dict) -> bool:
    return any(a.lower() in origin for a in cfg["launchpads"]["allowed"])


# ---------------------------------------------------------------- market

def market_snapshot(pairs: list[dict]) -> dict:
    """Aggregate DexScreener pairs for one token."""
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    created = [p["pairCreatedAt"] for p in pairs if p.get("pairCreatedAt")]
    h24 = [(p.get("txns") or {}).get("h24") or {} for p in pairs]
    price_usd = float(best.get("priceUsd") or 0)
    price_native = float(best.get("priceNative") or 0)
    quote = (best.get("quoteToken") or {}).get("symbol", "").upper()
    return {
        "symbol": (best.get("baseToken") or {}).get("symbol"),
        "name": (best.get("baseToken") or {}).get("name"),
        "dex": best.get("dexId"),
        "pair": best.get("pairAddress"),
        "url": best.get("url"),
        "quote": quote,
        "price_usd": price_usd,
        "price_sol": price_native if quote in ("SOL", "WSOL") else None,
        "mcap_usd": best.get("marketCap") or best.get("fdv") or 0,
        "liquidity_usd": sum((p.get("liquidity") or {}).get("usd") or 0 for p in pairs),
        "first_pair_ms": min(created) if created else None,
        "buys_24h": sum(x.get("buys", 0) for x in h24),
        "sells_24h": sum(x.get("sells", 0) for x in h24),
        "volume_24h": sum((p.get("volume") or {}).get("h24") or 0 for p in pairs),
    }


def market_checks(m: dict, cfg: dict) -> list[str]:
    s = cfg["safety"]
    reasons = []
    if m["liquidity_usd"] < s["min_liquidity_usd"]:
        reasons.append(f"low_liquidity (${m['liquidity_usd']:,.0f})")
    elif m["mcap_usd"] and m["liquidity_usd"] / m["mcap_usd"] < s["min_liquidity_to_mcap"]:
        reasons.append("thin_liquidity_vs_mcap")
    if s["require_sells_in_24h"] and m["buys_24h"] > 20 and m["sells_24h"] == 0:
        reasons.append("no_sells_possible_honeypot")
    return reasons


# ---------------------------------------------------------------- on-chain

def _risky_extension(ext: dict) -> bool:
    """Token-2022 extensions only matter when they are actually armed (stonk.fun mints are Token-2022)."""
    name, st = ext.get("extension"), ext.get("state") or {}
    if name == "transferFeeConfig":
        bps = [(st.get(k) or {}).get("transferFeeBasisPoints") or 0 for k in ("olderTransferFee", "newerTransferFee")]
        return max(bps) > 0 or bool(st.get("transferFeeConfigAuthority"))
    if name == "transferHook":
        return bool(st.get("programId") or st.get("authority"))
    if name == "permanentDelegate":
        return bool(st.get("delegate"))
    if name == "defaultAccountState":
        return str(st.get("accountState", "")).lower() == "frozen"
    if name == "pausableConfig":
        return bool(st.get("authority") or st.get("paused"))
    return name == "nonTransferable"


def onchain_checks(info: dict | None, cfg: dict) -> list[str]:
    s = cfg["safety"]
    if not info:
        return ["mint_account_unreadable"]
    reasons = []
    if s["require_revoked_mint_authority"] and info["mint_authority"]:
        reasons.append("mint_authority_enabled")
    if s["require_revoked_freeze_authority"] and info["freeze_authority"]:
        reasons.append("freeze_authority_enabled_honeypot")
    if info["program"] == TOKEN_2022:
        bad = sorted({e.get("extension") for e in info["extensions"]
                      if e.get("extension") in s["reject_token2022_extensions"] and _risky_extension(e)})
        if bad:
            reasons.append("token2022_risky_extensions:" + ",".join(bad))
    return reasons


def rugcheck_checks(report: dict | None, pair_address: str | None, cfg: dict) -> list[str]:
    s = cfg["safety"]
    if not report:
        return ["rugcheck_unavailable"] if s["require_rugcheck"] else []
    reasons = []
    if report.get("rugged"):
        reasons.append("rugcheck_rugged")
    nets = [n.get("size") or 0 for n in report.get("insiderNetworks") or []]
    if nets and max(nets) >= s["max_insider_network"]:
        reasons.append(f"insider_network_{max(nets)}_wallets")
    score = report.get("score_normalised")
    if score is not None and score > s["max_rugcheck_score_normalised"]:
        reasons.append(f"rugcheck_score_{score}")
    if s["reject_rugcheck_danger_risks"]:
        for r in report.get("risks") or []:
            if (r.get("level") or "").lower() == "danger":
                reasons.append("rugcheck:" + (r.get("name") or "danger"))
    known = report.get("knownAccounts") or {}
    pools = {pair_address}
    for mk in report.get("markets") or []:  # pool, its vaults, and the vaults' owner (the pool authority)
        pools |= {mk.get("pubkey"), mk.get("liquidityA"), mk.get("liquidityB")}
        for side in ("liquidityAAccount", "liquidityBAccount"):
            acct = mk.get(side)
            pools.add(acct.get("owner") if isinstance(acct, dict) else acct)
    pools.discard(None)
    holders = [h for h in report.get("topHolders") or []
               if h.get("owner") not in pools and h.get("address") not in pools
               and (known.get(h.get("owner")) or {}).get("type") != "AMM"]
    top10 = sum(float(h.get("pct") or 0) for h in holders[:10])
    if top10 > s["max_top10_holder_pct"]:
        reasons.append(f"concentrated_holders_top10_{top10:.0f}pct")
    return reasons
