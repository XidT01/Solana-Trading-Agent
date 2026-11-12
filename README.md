# Solana Smart Wallet Agent

Finds Solana meme-coin wallets that got in early on coins that ran to $100K+ and made 5x or 10x.
It runs around the clock for 30 days, publishes a daily report, and serves a dashboard on your website.

## What it does

Every 30 minutes, day and night, from **2026-11-11 to 2026-12-10**:

1. **Discover** fresh launches from GeckoTerminal's new pools and DexScreener's latest tokens. Young coins go on
   a watchlist and are re-checked every run for up to 48 hours. Coins still under $8K after 3 hours are dropped.
2. **Peak $100K+.** Only coins whose market cap **peaked** at $100K or more move on. The peak comes from 15-minute
   price candles, so a coin that hit $100K and then dumped still counts.
3. **Launch platform.** Keeps only Pump.fun, Raydium, Meteora and stonk.fun launches. The platform
   is read from each token's creation transaction on-chain (see [stonk.fun](#stonkfun) below).
4. **Safety.** Rejects a token if any of these apply:
   - Liquidity under $15K, or under 5% of market cap.
   - Mint authority still enabled.
   - Freeze authority enabled (the classic Solana honeypot).
   - Risky Token-2022 extensions: transfer hooks, transfer fees, permanent delegate, and similar.
   - Buys but zero sells.
   - RugCheck score above 50 or any "danger" risk.
   - Top 10 holders own more than 35%.
5. **Wallets.** For each coin that passed, the agent reads its first transactions straight from the Solana chain.
   It finds wallets that:
   - Bought below **$30K** market cap on average (excluding the dev wallet and same-block snipers).
   - Made at least **5x**: SOL from sells plus the current value of what they still hold, divided by SOL spent.

   Wallets under 5x are not tracked. Wallets at **10x+** are tagged and called out separately.
   To also require holding to $100K, set `min_hold_fraction_at_target` in `config.yaml`.
6. **Daily report at 23:50 UTC.** Covers the day's stats, the new qualifying wallets, a campaign-to-date
   leaderboard (wallets that win on several different coins rank first), and a written summary by Claude.
   It can optionally be sent to Telegram.

Every number in `config.yaml` can be changed.

## Setup (about 15 minutes)

1. **Create a GitHub repo** (public is recommended: Actions minutes are free and unlimited) and push this folder to it.
2. **Get a Solana RPC URL.** Use [Helius](https://helius.dev) (the free tier is enough at the default budget) or any
   other provider. It looks like `https://mainnet.helius-rpc.com/?api-key=...`.
3. **Add repository secrets** under Settings → Secrets and variables → Actions:
   | Secret | Required | Purpose |
   |---|---|---|
   | `SOLANA_RPC_URL` | yes | chain data for wallet analysis |
   | `ANTHROPIC_API_KEY` | optional | AI-written summary in the daily report |
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional | daily report pushed to Telegram |
4. **Turn on the website** under Settings → Pages → Source: **GitHub Actions**.
5. **Test it once.** Go to Actions → "Scan" → Run workflow, and tick *force*.
   Then run "Daily report" the same way.

From then on it runs by itself and stops automatically after 2026-12-10.

## BSC, Base and Robinhood Chain

The same rules run on three EVM chains, in a separate workflow (`Scan EVM`) that runs alongside the
Solana scan. The website has a chain switcher at the top.

| Chain | Launchpads tracked | How the launchpad is recognised |
|---|---|---|
| BSC | four.meme, Flap | GeckoTerminal's `four-meme` label, the four.meme / Flap contracts, `…4444` / `…7777` / `…8888` addresses |
| Base | Clanker, Zora, Bankr, o1 | the Clanker, Zora and o1 factory contracts, GeckoTerminal's `bankr` / `o1-launchpad` labels |
| Robinhood | Pons, Pools.trade, Long.xyz, launchpad.meme, o1 | GeckoTerminal's launchpad labels, the Pons contract |

How it works on EVM:
- **Trades** come from each coin's `Transfer` logs on a free public RPC (publicnode for BSC and Base,
  Robinhood's official RPC). Wallets that gain tokens from a market contract (bonding curve, pool, or the
  Uniswap v4 PoolManager) are buyers; routers and fee wallets are filtered out.
- **Prices** come from GeckoTerminal's 1-minute candles, so PnL and multiples are in **USD**.
- **Safety:** GoPlus (honeypot, taxes over 10%, mintable, blacklist, hidden owner, holder concentration) on
  all three chains, plus a honeypot.is buy/sell simulation on BSC and Base. Robinhood Chain has no
  honeypot scanner yet, so there it relies on GoPlus where available plus the liquidity and sells checks.
- Coins from launchpads the agent can't identify are skipped and counted; each run records the most common
  unknown factory contracts (`data/chains/<chain>/runs.json`) so they can be named in `config.yaml`.

Optional secrets for faster, more reliable RPCs: `EVM_RPC_BSC`, `EVM_RPC_BASE`, `EVM_RPC_ROBINHOOD`.
Without them the free public RPCs are used.

## stonk.fun

stonk.fun runs on Raydium LaunchLab, and its tokens have no address suffix (unlike pump.fun's `...pump`).
The agent therefore identifies each launch by the LaunchLab platform-config account in its creation transaction.
Other LaunchLab platforms such as letsbonk.fun have their own config account, so they can be told apart and
filtered out.

stonk.fun's platform config is `6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt` (already set in `config.yaml`).
It was verified on-chain: the account belongs to LaunchLab and is named "StonkFun". To check how any other
token is classified, run:

```bash
SOLANA_RPC_URL=... python -m agent.identify <token address>
```

stonk.fun launches can also be paired against tokenized stocks instead of SOL. The agent converts those
trades to SOL value using each quote asset's current price, so entries and PnL stay comparable.
stonk.fun tokens are also Token-2022 tokens. They're rejected only if a dangerous setting is actually armed:
a non-zero or changeable transfer fee, a transfer hook, a permanent delegate, frozen-by-default accounts,
or a pause switch.

## Putting it on your personal website

The dashboard is published at `https://<you>.github.io/<repo>/`. You can:
- link to it,
- point a subdomain at it (for example `wallets.yoursite.com`) under Settings → Pages → Custom domain, or
- embed it on any page:
  ```html
  <iframe src="https://<you>.github.io/<repo>/" style="width:100%;height:1400px;border:0"></iframe>
  ```

You can also host it anywhere else: copy `site/index.html` together with the `data/` folder to any static host.

## Run locally

```bash
pip install -r requirements.txt
python -m pytest tests          # offline tests on a simulated launch
SOLANA_RPC_URL=... python -m agent.scan --force
python -m agent.report --force
```

## Files

| Path | What |
|---|---|
| `config.yaml` | all filters, the schedule window and budgets |
| `agent/scan.py` | one scan cycle (discovery → filters → wallet analysis) |
| `agent/safety.py` | launchpad + honeypot / liquidity / scam checks |
| `agent/wallets.py` | on-chain entry, hold and PnL analysis |
| `agent/report.py` | daily report, AI summary, Telegram |
| `agent/identify.py` | shows which launch platform a token is detected as |
| `agent/evm_scan.py` | one scan cycle on BSC, Base and Robinhood Chain |
| `agent/evm.py` | EVM transfer history, market detection, USD pricing, wallet analysis |
| `agent/evm_safety.py` | GoPlus + honeypot.is checks |
| `site/index.html` | the dashboard |
| `data/` | results, written back by GitHub Actions |

## Limits to know

- **Fresh tokens only.** Coins older than 48 hours, or with more than 200,000 transactions, are skipped.
  This keeps RPC usage down.
- **Entry market cap is estimated from each trade's SOL in/out.** Fees and priority tips make entries look
  slightly *worse*, so the estimate is conservative. It uses the current SOL price.
- **Transfers.** Tokens a wallet sends to another wallet count as zero value, and the wallet is flagged
  `transferred_out`.
- **Snipers and bundles.** Wallets this early are often bots, snipers or insiders. The agent drops the dev
  wallet and same-block buys. The AI summary flags other suspicious patterns. Wallets that win on 2 or more
  coins are the meaningful signal.
- **RPC budget.** Analysis is capped at 20 coins a day, spread across the day, at roughly 1–2K RPC credits each.
  Raise `max_tokens_per_day` if your RPC plan allows.
- **Not financial advice.** Past wallet performance doesn't predict future results.
