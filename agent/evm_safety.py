"""Honeypot / scam checks for EVM tokens: GoPlus token security (all chains) + honeypot.is buy/sell
simulation (BSC, Base). Liquidity checks are shared with Solana (safety.market_checks)."""
from __future__ import annotations

from .common import Http

GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
HONEYPOT_IS = "https://api.honeypot.is/v2/IsHoneypot"
_http = Http(rps=1)

# GoPlus flags that make a token unsafe whenever they are set.
GOPLUS_DANGER = {
    "is_honeypot": "honeypot",
    "cannot_sell_all": "cannot_sell_all",
    "cannot_buy": "cannot_buy",
    "owner_change_balance": "owner_can_change_balances",
    "hidden_owner": "hidden_owner",
    "selfdestruct": "can_selfdestruct",
    "slippage_modifiable": "tax_modifiable",
    "honeypot_with_same_creator": "creator_made_honeypots_before",
}


def goplus(chain_id: int, token: str) -> dict | None:
    res = _http.get(GOPLUS.format(chain_id=chain_id), params={"contract_addresses": token})
    return ((res or {}).get("result") or {}).get(token.lower()) or None


def honeypot_is(chain_id: int, token: str) -> dict | None:
    return _http.get(HONEYPOT_IS, params={"address": token, "chainID": chain_id})


def _pct(v) -> float:
    try:
        return float(v) * 100
    except (TypeError, ValueError):
        return 0.0


def goplus_checks(gp: dict | None, cfg: dict, markets: set[str] = frozenset()) -> list[str]:
    s = cfg["evm"]["safety"]
    if not gp:
        return []
    reasons = [label for key, label in GOPLUS_DANGER.items() if str(gp.get(key)) == "1"]
    owner_live = gp.get("owner_address") not in (None, "", "0x0000000000000000000000000000000000000000",
                                                  "0x000000000000000000000000000000000000dead")
    if str(gp.get("is_mintable")) == "1" and owner_live:
        reasons.append("mintable_by_owner")
    if str(gp.get("transfer_pausable")) == "1" and owner_live:
        reasons.append("transfers_pausable")
    if str(gp.get("is_blacklisted")) == "1" and owner_live:
        reasons.append("blacklist_function")
    for side in ("buy_tax", "sell_tax"):
        if _pct(gp.get(side)) > s["max_tax_pct"]:
            reasons.append(f"{side}_{_pct(gp.get(side)):.0f}pct")
    holders = [h for h in gp.get("holders") or []
               if str(h.get("is_contract")) != "1" and str(h.get("is_locked")) != "1"
               and (h.get("address") or "").lower() not in markets]
    top10 = sum(_pct(h.get("percent")) for h in holders[:10])
    if top10 > cfg["safety"]["max_top10_holder_pct"]:
        reasons.append(f"concentrated_holders_top10_{top10:.0f}pct")
    return reasons


def honeypot_is_checks(hp: dict | None, cfg: dict) -> list[str]:
    if not hp:
        return []
    reasons = []
    if (hp.get("honeypotResult") or {}).get("isHoneypot"):
        reasons.append("honeypot_simulation_failed_sell")
    sim = hp.get("simulationResult") or {}
    for side in ("buyTax", "sellTax"):
        if float(sim.get(side) or 0) > cfg["evm"]["safety"]["max_tax_pct"]:
            reasons.append(f"simulated_{side}_{float(sim[side]):.0f}pct")
    return reasons


def evm_safety(chain: dict, token: str, cfg: dict) -> tuple[list[str], str]:
    """(reasons to reject, coverage note). Coverage is 'full', 'goplus_only' or 'basic'."""
    gp = goplus(chain["chain_id"], token)
    reasons = goplus_checks(gp, cfg)
    hp = honeypot_is(chain["chain_id"], token) if chain.get("honeypot_is") else None
    reasons += honeypot_is_checks(hp, cfg)
    coverage = "full" if gp and hp else "goplus_only" if gp else "honeypot_is_only" if hp else "basic"
    if coverage == "basic" and cfg["evm"]["safety"]["require_security_scan"]:
        reasons.append("no_security_scan_available")
    return sorted(set(reasons)), coverage
