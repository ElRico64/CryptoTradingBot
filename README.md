# memebot: a memecoin trading bot

A self-hosted bot that scans memecoins around the clock, rejects rugs and honeypots, buys only
when several independent signals agree, and manages exits with a fixed profit-taking plan
(stake back at 2x, then a trailing stop on the rest).

> **Read this first.** Most memecoins go to zero, and no strategy wins every trade. This bot is
> built to make the *expected value* positive: small losses, occasional large wins, strict risk
> limits. It cannot guarantee profit. Start in paper mode, then go live with an amount you can
> afford to lose completely.

---

## Why not Axiom?

Axiom (like Photon, BullX and GMGN's web app) has **no public trading API**. The only way to
automate it is to script its website with your login. That breaks its terms of service, breaks
whenever the site changes, and means handing your account to a script. So the bot does what
Axiom does under the hood and trades directly on-chain:

| Chain | How trades are placed | What you need |
|---|---|---|
| Solana | [Jupiter](https://jup.ag) aggregator (routes through Raydium, PumpSwap, Meteora, Orca, pump.fun…) | a dedicated wallet's private key + an RPC URL |
| Base / BSC / Ethereum | [0x Swap API v2](https://0x.org) | a dedicated wallet's private key + RPC URL + free 0x API key |

The key stays on your machine. Swaps are built by Jupiter/0x **unsigned**, signed locally, and
sent to your own RPC.

**Tip:** Axiom lets you export your wallet's private key. If you put that key in the bot, you can
keep watching the bot's trades in Axiom's interface. A **fresh wallet** used only by the bot is
still the safer setup.

Never paste a private key into a chat, an issue or a commit. Put it only in `.env` on the
machine that runs the bot.

---

## How it trades

```
 DISCOVERY (always on)                  ANALYSIS                              EXECUTION
 ─────────────────────                  ────────                              ─────────
 DexScreener new profiles/boosts ┐
 GeckoTerminal trending + new    │      1. Hard filters (liquidity, mcap,     risk checks: position caps,
 pump.fun graduations (live WS)  ├──►      age, activity, not vertical)  ──►  daily loss kill switch,
 KOL wallet consensus            │      2. Safety gates (rug/honeypot)        loss-streak pause,
 Narrative engine (posts → $TKR) ┘      3. Chart structure (1-min candles)    1 position per narrative
                                        4. Score + confluence                       │
                                        5. Learned model (from your trades)   buy-then-sell quote check
                                                                              (catches hidden taxes)
                                                                                    │
                                        EXIT ENGINE (every 3s) ◄─────────────── BUY
```

### 1. Discovery
- **DexScreener**: newly listed token profiles and boosted tokens. Boosts are paid ads, so they
  only put a token on the watchlist and add nothing to its score.
- **GeckoTerminal**: trending pools (organic) and brand-new pools.
- **PumpPortal**: real-time pump.fun graduations, the moment a bonding-curve token migrates to a
  real AMM pool.
- **Smart money (KOL copy-trading)**: tracks wallets you choose and fires only on *consensus*,
  meaning several tracked wallets buying the same token within 30 minutes, weighted by how much you
  trust each one. Copying a single KOL often makes you their exit liquidity. When those same
  wallets sell, the bot sells with them.
- **Narrative engine**: built for cases like `$SI` after Trump's "superintelligence" post. It
  watches chosen X accounts, RSS/news feeds and a local `narratives.txt` file. For each post it
  extracts the phrases and the tickers people are likely to launch ("Super Intelligence" → `SI`,
  `SUPERINTELLIGENCE`). It searches the DEXs right away and keeps searching while the narrative
  is fresh, because the winning token often launches minutes after the post. It also flags
  brand-new pump.fun launches that match a live narrative. Optionally, Claude can be asked which
  tickers a post will spawn, which catches non-obvious ones.

### 2. Safety gates (any failure = never buy)
- **Solana**: mint and freeze authority must be revoked (checked on-chain). Token-2022 tokens
  with a permanent delegate, transfer hook, or high transfer fee are rejected. The
  [RugCheck](https://rugcheck.xyz) report must show no "danger" risks, top-10 holders under 30%
  (pool accounts excluded), and limited insider holdings.
- **EVM**: [GoPlus](https://gopluslabs.io) security data. Rejects honeypots, buy/sell tax over 5%,
  mintable or unverified contracts, hidden owners, blacklists, pausable transfers, and
  concentrated holders.
- **Both**: right before buying, the bot quotes a buy and then a sell of the same size. If the
  round trip loses more than 12%, the token is blacklisted. This catches honeypots and hidden
  taxes the scanners missed.
- If a safety API is unreachable, the token is skipped (`fail_closed: true`).

### 3. Scoring
Each token gets a score out of 100 from weighted parts:

| Component | What it rewards |
|---|---|
| order flow | buy/sell ratio, transactions speeding up, real trade sizes (not bot dust) |
| chart structure | first pullback after an impulse that holds VWAP, tight-range breakouts with volume, higher lows. Overextended, distributing, or failed-impulse charts are penalised. |
| momentum | rising but not already parabolic |
| liquidity | depth, liquidity/mcap ratio, healthy turnover |
| earliness | young pair, small cap |
| safety | holder spread, RugCheck score, warning count |

Bonuses are added for **smart-money consensus** (up to +15) and a **live narrative match**
(up to +15).

**Quality over quantity:** a trade needs a score ≥ 72 *and* at least two independent
confirmations (smart money, narrative, chart, order flow, momentum). The one exception is a
score ≥ 85, which needs only one confirmation. Defaults also cap the bot at 8 trades a day.

**It learns from its own results.** Every analysed token is logged with its features, and every
closed trade records its outcome. Run `memebot train` and a model fits which features actually
paid in your market. Once it has 40+ trades it is blended into the score.

### 4. Profit-taking: the exit engine
Checked every 3 seconds, in this priority order:

| Rule | Default |
|---|---|
| Liquidity pulled | pool liquidity −40% vs entry → sell everything |
| Hard stop | −30% before the first target |
| **Take-profit ladder** | **2x: sell 50%** (stake recovered, the rest is house money) · 5x: sell half of what's left · 10x: half again |
| Breakeven floor | after the 2x sale, the remainder never closes below 1.1x |
| Trailing stop | from the peak: −35% (peak ≥1.6x), −30% (≥3x), −25% (≥6x), −20% (≥15x) |
| Smart money exits | the KOLs that triggered the entry sell → sell all (or half, if already past 2x) |
| Sellers take over | losing position, buys <32% of the last 5 minutes → out |
| Time stop | no 1.25x within 30 minutes → free the capital |
| Max hold | 24h |

Why 50% at 2x instead of selling everything? Taking the stake back at 2x locks in a profitable
trade. Memecoin returns are extremely skewed, though: a handful of 10x–100x runners make the
year, and the trailing "moon bag" is what catches them. If you want a full exit at 2x, set the
first take-profit's `sell_fraction: 1.0`.

### 5. Risk management
- 4% of equity per normal trade, scaled 0.7×–1.5× by score.
- Never more than 1.5% of the pool's liquidity, so slippage stays low and exits stay possible.
- Max 4 open positions, and 1 per narrative (no doubling up on copycats).
- **Kill switch:** −12% realised in a UTC day → no new entries until tomorrow.
- **Cool-off:** 3 losses in a row → 90-minute pause.
- Open positions are saved to SQLite and picked up again after a restart.

---

## Setup

```bash
git clone <this repo> && cd CryptoTradingBot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml
cp .env.example .env          # paper mode needs nothing in here
pytest                        # 72 tests
```

### Step 1: paper trade (no keys, no money)
```bash
memebot run                   # watches the market, trades a simulated $1,000
memebot stats                 # win rate, P&L, profit factor, 2x hits
```
Paper fills include price impact from pool depth, latency slippage and fees on both sides, so
the results are realistic rather than optimistic. Run it for at least a week or two.

### Step 2: use the analysis tools
```bash
memebot check solana <mint>   # full breakdown: filters, safety, chart, score, decision
memebot check base 0x...      # EVM tokens too
memebot narrative "We will name it Super Intelligence"   # which tickers it would hunt, and what it finds
echo "Elon: my new dog is called Floki Junior" >> narratives.txt   # inject a headline into a running bot
memebot backtest --chain solana --pool <pool address>    # replay the exit rules on real candles
```

### Step 3: turn on the edges
- **KOL wallets:** find consistently profitable wallets on kolscan.io, GMGN's KOL leaderboard,
  or Cielo, and check their history yourself. Add them under `kol.wallets` and set
  `kol.enabled: true`. A paid Solana RPC (Helius, Triton, QuickNode) makes tracking much faster.
- **X accounts:** add `X_BEARER_TOKEN` (X API, paid tier for reading timelines) and list
  accounts under `narrative.x_accounts`. Trump posts mainly on Truth Social, which has no
  official API. Add a trusted RSS mirror to `narrative.rss_feeds`, or use `narratives.txt`.
- **Claude ticker prediction:** set `narrative.use_llm: true` and `ANTHROPIC_API_KEY`.
- **Telegram alerts:** `notify.telegram: true` plus `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.

### Step 4: go live (small)
1. Create a fresh wallet and fund it with a small amount of SOL (or ETH/BNB).
2. In `.env`, set `SOLANA_PRIVATE_KEY`, a paid `SOLANA_RPC_URL`, and `MEMEBOT_I_ACCEPT_THE_RISK=yes`.
3. In `config.yaml`, set `mode: live`. Consider `max_position_usd: 20` for the first days.
4. Run `memebot wallet` to confirm the address and balance, then `memebot run`.
5. Watch the first trades on Solscan/Axiom and check them against `memebot stats`.

**Not yet tested against the live APIs.** The build environment blocks every crypto API, so the
live executors (Jupiter and 0x), the RPC calls and the data clients were tested against mocked
responses in the documented formats, not real endpoints. Paper-trade first, then do one tiny
live trade, to confirm that quotes, signing, confirmation and balance tracking all work with
your RPC before you size up.

---

## Commands

| Command | What it does |
|---|---|
| `memebot run` | run the bot (paper or live, per config) |
| `memebot check <chain> <address>` | analyse one token without trading |
| `memebot narrative "<text>"` | show how a post becomes tickers to hunt, and current matches |
| `memebot backtest --csv f.csv` / `--pool <addr>` | replay the exit strategy on candles |
| `memebot train` | fit the learned model from closed trades |
| `memebot stats` | performance summary and recent trades |
| `memebot wallet` | show the configured wallet address and balance |

## Project layout

```
memebot/
  engine.py            pipeline: discovery → analysis → entries → exit monitoring
  app.py               wires config + secrets into the engine
  config.py            all tunables (see config.example.yaml)
  safety.py            rug / honeypot gates (RugCheck, on-chain mint, GoPlus)
  risk.py              sizing, caps, kill switch, cool-off
  journal.py           SQLite log of signals, trades and positions
  backtest.py          exit-strategy replay
  analysis/            features, chart patterns, scorer, learned model
  strategy/exits.py    the exit engine
  sources/             DexScreener/GeckoTerminal/PumpPortal scanners, KOL tracker, narrative engine
  execution/           paper, Jupiter (Solana), 0x (EVM)
  data/                API clients (DexScreener, GeckoTerminal, Solana RPC, HTTP)
tests/                 72 tests, including a full trade lifecycle
```
