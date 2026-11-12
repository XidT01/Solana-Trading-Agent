"""Anti-manipulation checks shared by the Solana and EVM analysers.

Learned from coins flagged as controlled charts / rugs (BULLHARIS, mihao, ape hao, VEYL):
- bundled launches: many "winning" wallets all buying in the first seconds, then passing tokens around;
- paper profits: wallets that never sold look like 10-30x winners right up until the rug.
The paper-profit rule lives in the analysers (wallets.pnl_mode: realised); the coin-level checks are here.
"""
from __future__ import annotations


def bundled_launch(results: list[dict], launch_time: int | None, cfg: dict) -> int | None:
    """Number of would-be winners (by total multiple, before the stricter wallet rules) whose first buy came
    within the bundle window after launch, if it reaches the bundle threshold; otherwise None."""
    i = cfg["integrity"]
    if not launch_time:
        return None
    n = sum(1 for r in results
            if r.get("total_multiple", r["multiple"]) >= cfg["wallets"]["min_multiple"]
            and r["first_buy_time"] - launch_time <= i["bundle_window_seconds"])
    return n if n >= i["bundle_min_wallets"] else None


def wallet_passes(r: dict, cfg: dict) -> bool:
    """Wallet-level integrity: no tokens sent on to other wallets (bundle distribution)."""
    return not (cfg["wallets"]["exclude_transfer_out"] and r.get("transferred_out"))
