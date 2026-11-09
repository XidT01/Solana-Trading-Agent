"""Show how the agent classifies a token's launch platform.

    SOLANA_RPC_URL=... python -m agent.identify <mint> [<mint> ...]

Use it once on any token you know was launched on stonk.fun: the printed `launchlab platform config`
is stonk.fun's address. Add it to config.yaml under launchpads.launchlab_platforms as `<address>: stonk`.
"""
from __future__ import annotations

import argparse
import os
import sys

from .common import load_config, setup_logging
from .safety import allowed_launch, launch_origin, launchlab_platform
from .sources import DexScreener, SolanaRPC


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mints", nargs="+")
    ap.add_argument("--pages", type=int, default=100, help="max 1000-signature pages to walk back to creation")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(args.config)
    if not os.environ.get("SOLANA_RPC_URL"):
        print("Set SOLANA_RPC_URL first.", file=sys.stderr)
        return 2
    rpc = SolanaRPC(os.environ["SOLANA_RPC_URL"], cfg["rpc"]["requests_per_second"])
    pairs = DexScreener().pairs_for(args.mints)
    for mint in args.mints:
        sigs, complete = rpc.signatures_asc(mint, args.pages)
        tx = rpc.get_transaction(sigs[0]["signature"]) if sigs else None
        origin = launch_origin(mint, pairs.get(mint) or [], cfg, tx)
        quotes = sorted({(p.get("quoteToken") or {}).get("symbol", "?") for p in pairs.get(mint) or []})
        print(f"\n{mint}")
        print(f"  creation tx               : {sigs[0]['signature'] if sigs else 'not found (history too long? raise --pages)'}")
        print(f"  launchlab platform config : {launchlab_platform(tx, cfg) or '-'}")
        print(f"  detected origin           : {origin}  ({'ALLOWED' if allowed_launch(origin, cfg) else 'filtered out'})")
        print(f"  quote assets              : {', '.join(quotes) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
