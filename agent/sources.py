"""Clients for the free market-data APIs (DexScreener, GeckoTerminal, RugCheck) and Solana RPC."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .common import WSOL, Http, log


class DexScreener:
    BASE = "https://api.dexscreener.com"

    def __init__(self):
        self.http = Http(rps=4)

    def latest_solana_mints(self) -> list[str]:
        mints: list[str] = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            for item in self.http.get(self.BASE + path) or []:
                if item.get("chainId") == "solana" and item.get("tokenAddress"):
                    mints.append(item["tokenAddress"])
        return mints

    def pairs_for(self, mints: list[str]) -> dict[str, list[dict]]:
        """mint -> list of DexScreener pairs where the mint is the base token."""
        out: dict[str, list[dict]] = {m: [] for m in mints}
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            for p in self.http.get(f"{self.BASE}/tokens/v1/solana/{','.join(chunk)}") or []:
                base = (p.get("baseToken") or {}).get("address")
                if base in out:
                    out[base].append(p)
        return out

    def sol_usd(self) -> float | None:
        pairs = self.http.get(f"{self.BASE}/tokens/v1/solana/{WSOL}") or []
        best = None
        for p in pairs:
            if (p.get("baseToken") or {}).get("address") != WSOL:
                continue
            if (p.get("quoteToken") or {}).get("symbol", "").upper() not in ("USDC", "USDT"):
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if best is None or liq > best[0]:
                best = (liq, float(p.get("priceUsd") or 0))
        return best[1] if best and best[1] > 0 else None


class GeckoTerminal:
    BASE = "https://api.geckoterminal.com/api/v2"

    def __init__(self):
        self.http = Http(rps=0.4)  # free tier ~30 calls/min

    def new_pools(self, pages: int) -> list[tuple[str, str]]:
        """Returns (mint, dex_id) for the newest Solana pools."""
        out = []
        for page in range(1, pages + 1):
            res = self.http.get(f"{self.BASE}/networks/solana/new_pools", params={"page": page})
            for pool in (res or {}).get("data", []):
                rel = pool.get("relationships", {})
                base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                dex_id = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
                if base_id.startswith("solana_"):
                    out.append((base_id[len("solana_"):], dex_id))
        return out


class RugCheck:
    BASE = "https://api.rugcheck.xyz/v1"

    def __init__(self):
        self.http = Http(rps=1)

    def report(self, mint: str) -> dict | None:
        return self.http.get(f"{self.BASE}/tokens/{mint}/report")


class SolanaRPC:
    """Minimal JSON-RPC client. Works with any provider (Helius, QuickNode, Triton, public)."""

    def __init__(self, url: str, rps: float = 8):
        self.url = url
        self.http = Http(rps=rps, timeout=40)
        self.tx_cache: dict[str, dict | None] = {}
        self.calls = 0

    def call(self, method: str, params: list):
        self.calls += 1
        res = self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not res:
            return None
        if "error" in res:
            log.warning("RPC %s error: %s", method, res["error"])
            return None
        return res.get("result")

    def mint_info(self, mint: str) -> dict | None:
        res = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = (res or {}).get("value")
        if not value:
            return None
        parsed = (value.get("data") or {}).get("parsed") or {}
        info = parsed.get("info") or {}
        decimals = int(info.get("decimals", 0))
        return {
            "program": value.get("owner"),
            "supply": int(info.get("supply", 0)) / 10 ** decimals,
            "decimals": decimals,
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "extensions": [e.get("extension") for e in info.get("extensions", []) if isinstance(e, dict)],
        }

    def signatures_asc(self, address: str, max_pages: int) -> tuple[list[dict], bool]:
        """All successful signatures for an address, oldest first. complete=False if history exceeded max_pages."""
        sigs: list[dict] = []
        before = None
        for _ in range(max_pages):
            opts = {"limit": 1000}
            if before:
                opts["before"] = before
            page = self.call("getSignaturesForAddress", [address, opts])
            if page is None:
                return [], False
            sigs.extend(page)
            if len(page) < 1000:
                sigs.reverse()
                return [s for s in sigs if s.get("err") is None], True
            before = page[-1]["signature"]
        return [], False

    def get_transaction(self, sig: str) -> dict | None:
        if sig not in self.tx_cache:
            self.tx_cache[sig] = self.call("getTransaction", [sig, {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
        return self.tx_cache[sig]

    def get_transactions(self, sigs: list[str], workers: int = 8) -> dict[str, dict | None]:
        with ThreadPoolExecutor(workers) as pool:
            return dict(zip(sigs, pool.map(self.get_transaction, sigs)))
