# Sentinel — The Handbook

> The complete "understand it all" guide: what every feature does, how the pieces
> wire together, how to run and use it, and where it can go next. A personal,
> single-user, **paper-only** trading-intelligence copilot that lives in Discord
> and a localhost web dashboard.

For the wiring diagram and table-of-everything see `ARCHITECTURE.md`; for code
conventions see `../CLAUDE.md`.

---

## 1. The one-paragraph mental model

Sentinel continuously **ingests** the market's information surface (SEC filings,
Reddit, Hacker News, prices, macro/geopolitical news, crypto microstructure),
**reasons** over it with an LLM to produce connected reads and concrete
directional calls, **acts** on those calls in autonomous paper wallets,
**measures** whether the calls actually worked, **auto-corrects** by fading
sources it's measurably bad at, and **watches itself** for silent failure — all
surfaced into Discord channels and a web dashboard, queryable with `!commands`
and a copilot chat. It is opinionated by design: it's allowed to conclude,
predict, and advise. The only hard rule is **it never fabricates a number** — it
bets, but it never invents the evidence.

```
ingest → reason → call → trade(paper) → measure → auto-fade → self-monitor
   ^________________________________________________________________|
                (the scorecard feeds the next reason)
```

---

## 2. Design spine (the rules everything obeys)

These are invariants, not preferences.

- **Noise reduction over coverage.** A pipeline stays silent unless it has
  something real to say. An empty channel beats a noisy one — enforced in prompts
  and gates, not hoped for.
- **Never fabricate.** A stale/zero price never becomes P&L; an unscoreable call
  retires unscored; a verdict is arithmetic, never an LLM guess; the health report
  is deterministic. Reason boldly, invent nothing. And now it's *checked*:
  `verify.py` extracts the hard ticker-bound numbers the LLM emits (price, 1d/5d
  move, volume multiple, direction) and compares them to live `PriceContext` at
  the call + post chokepoints — flagging a contradiction with a ⚠ field, a
  `grounded=False` stamp, a floored conviction, and a `#meta` line. It only ever
  annotates; it never blocks a post or drops a call, and it fails open.
- **Accountable.** Every directional opinion is logged, marked to market, and
  scored. The track record is visible and feeds back into behavior.
- **Free to advise.** Single-user paper tool, no compliance surface — no "not
  financial advice" disclaimers, no refusing a take. The call *is* the product.
- **One chokepoint per concern.** Calls funnel through `scorecard.record_call`;
  embeds through `discord_client.post_embed`; prompts through `get_prompt`; DB
  through `session_scope`; wallet policy through `funds._POLICIES`; ticker→channel
  through `routing.channel_for`. Fix once, propagate everywhere.
- **Scheduler ↔ registry parity.** Every scheduled job is also a manual
  `--run-once` job (sole exception: the weekly watchlist rebuild, a sync bootstrap).
- **One process, one DB, one voice.** Discord and the web dashboard share the same
  accessors and the same SQLite database. No forked logic, no second writer.
- **Tested.** ~365 deterministic tests pin the load-bearing math and gates.

---

## 3. Setup & running

### Prerequisites

1. **An LLM for each tier.** Two independent choices:
   - *Local:* Ollama running with the configured models pulled
     (`LLM_MODEL_LIGHT`, `LLM_MODEL_HEAVY`; defaults `gemma4:e4b` and
     `qwen3:30b-a3b`).
   - *Serverless:* any OpenAI-compatible API (OpenRouter, Novita, DeepInfra,
     Google AI, …) via `LLM_API_*`, with per-tier overrides if light and heavy
     should use different providers. This deployment routes both tiers to
     `deepseek/deepseek-v4-flash` on OpenRouter, with local `qwen2.5:14b-instruct`
     as the heavy fallback.
2. **`.env`** with the Discord bot token + channel IDs and an EDGAR user-agent
   (see §4).
3. **`uv`** for dependency management.

### Commands

```bash
# Live bot + scheduler + web dashboard (this is "running it")
uv run python -m sentinel.main

# Boot self-check (go/no-go) — run this before the first real run
uv run python -m sentinel.main --preflight

# Single-cycle debug — run any one job once and exit
uv run python -m sentinel.main --run-once synthesis
uv run python -m sentinel.main --run-once filings --skip-watchlist

# Archive the live DB to data/backups/ and start from an empty schema
uv run python -m sentinel.main --reset

# Tests (~365) and lint
uv run pytest -q
uv run ruff check src tests
```

CLI flags: `--run-once <job>`, `--skip-watchlist`, `--skip-llm`, `--preflight`,
`--reset`. `--run-once` accepts any scheduled job name: `filings, reddit,
hackernews, prices, prices_daily, prices_backfill, sentiment, social_pulse,
digest, tuning, convergence, movers, hot_movers, thesis_generate, thesis_review,
auto_thesis, auto_exits, risk_circuit, auto_research_pre_earnings, briefing, news,
macro_themes, news_impact, mark_calls, call_review, book_risk, health,
funds_cycle, funds_digest, funds_meta, news_alerts, crypto_trending, crypto_micro,
synthesis, why_moved, watches, catalysts, lounge, reddit_feed`.

### Running it for real

- Smoke-test first: `--preflight`, then `--run-once prices`, `--run-once filings`,
  `--run-once macro_themes` — watch `#meta`.
- Run under something durable: `nohup uv run python -m sentinel.main >
  data/logs/run.out 2>&1 &`, tmux, or systemd.
- **Start on a weekday.** Equities only poll during NYSE hours; on a weekend most
  price-driven pipelines correctly look quiet.
- A day shows **liveness + content quality**. The edge layers
  (scorecard/verdicts/wallet-meta/auto-fade) need **1–3 weeks** because calls
  mature at 1d/5d/20d. Don't judge edge on day one.

### Preflight checks

`--preflight` runs eight boot checks in <5s and exits 0 (no criticals) or 1:
required env present (DISCORD_TOKEN critical), DB writable, schema initializes
with all tables, every `config/*.yaml` parses, channel IDs look like valid
snowflakes, the dashboard port is free, the watchlist is seeded (warning), and
both LLM tiers respond (critical only if both are down — one down degrades to the
fallback).

### Operational note (SQLite concurrency)

Many pipelines write concurrently from worker threads. The engine is WAL +
`busy_timeout=60000` + `synchronous=NORMAL` so a contended writer **waits** rather
than errors. The price poller bulk-inserts (`INSERT … ON CONFLICT DO NOTHING`) so
the write lock is held for milliseconds, not seconds.

---

## 4. Configuration (`.env`)

| Var | Purpose |
|---|---|
| `DISCORD_TOKEN`, `DISCORD_GUILD_ID` | Bot auth / server |
| `DISCORD_USER_ID` | The owner — receives `@mention` on priority posts |
| **Streams (raw-ish, time-ordered):** | |
| `DISCORD_FILINGS_CHANNEL_ID` | All SEC filings (with materiality scoring) |
| `DISCORD_INSIDERS_CHANNEL_ID` | Form 4 / 13F insider activity |
| `DISCORD_NEWS_CHANNEL_ID` | Per-ticker news + breaking alerts |
| `DISCORD_MACRO_CHANNEL_ID` | Macro/geopolitical news only (→ news → pulse) |
| `DISCORD_CRYPTO_CHANNEL_ID` | All per-coin crypto content (→ news) |
| `DISCORD_REDDIT_CHANNEL_ID` | Notable r/ posts — *skips* when unset (firehose) |
| `DISCORD_PULSE_CHANNEL_ID` | Social-mention spikes only |
| **Curated (the bot's reasoning):** | |
| `DISCORD_PRIORITY_CHANNEL_ID` | Material 8-Ks + high-conviction signals |
| `DISCORD_CONVERGENCE_CHANNEL_ID` | Multi-source agreement (→ priority) |
| `DISCORD_HOT_CHANNEL_ID` | Watchlist movers NOW — *skips* when unset (opt-in) |
| `DISCORD_CALLS_CHANNEL_ID` | Call-resolution verdicts (→ digest → meta) |
| `DISCORD_RISK_CHANNEL_ID` | Book-risk alerts on open positions (→ priority → meta) |
| `DISCORD_FUNDS_CHANNEL_ID` | Autonomous-wallet trade narrations (→ digest → meta) |
| **Daily / system:** | |
| `DISCORD_DIGEST_CHANNEL_ID` | EOD digest + pre-market briefing |
| `DISCORD_CATALYSTS_CHANNEL_ID` | Forward catalyst calendar (→ digest → news) |
| `DISCORD_GENERAL_CHANNEL_ID` | The Lounge — geopolitics↔market musings (→ digest) |
| `DISCORD_META_CHANNEL_ID` | Ops / health / errors / tuning — the bot's console |
| **LLM:** | |
| `OLLAMA_BASE_URL`, `LLM_MODEL_LIGHT`, `LLM_MODEL_HEAVY` | Local Ollama tiers |
| `LLM_API_BASE/KEY/MODEL_LIGHT/MODEL_HEAVY` | Shared OpenAI-compatible API |
| `LIGHT_LLM_API_*`, `HEAVY_LLM_API_*` | Per-tier provider overrides |
| `LLM_API_PROVIDER_LIGHT/HEAVY` | OpenRouter provider/quant hint |
| `LLM_REASONING` | `low`/`medium`/`high`/`off` (default `medium`) |
| `LLM_PRICE_IN_PER_M`, `LLM_PRICE_OUT_PER_M` | Token pricing for the `$` estimate |
| **Data sources:** | |
| `REDDIT_USER_AGENT` | Override the rotating UA pool (leave blank) |
| `EDGAR_USER_AGENT` | Required by SEC EDGAR fair-use |
| **Cadences:** | |
| `POLL_*_MINUTES`, `*_HOURS`, `*_HOUR_ET`, `NEWS_ALERTS_MINUTES`, `SYNTHESIS_HOURS`, `WHY_MOVED_MINUTES`, `WATCHES_MINUTES` | Pipeline timing |
| **Wallets + dashboard:** | |
| `FUND_STARTING_CASH` (10,000), `FUNDS_CYCLE_MINUTES` | Wallets |
| `DASHBOARD_ENABLED/HOST/PORT` (127.0.0.1:8730) | Web server |

**Channel fallback is universal:** an unset optional channel degrades to a
sensible parent (never crashes). `#reddit` and `#hot` are the exceptions — they
*skip* rather than fall back, to avoid firehosing a shared channel.

**Config YAML** (in `config/`): `indices.yaml` (sp500, nasdaq100),
`etfs.yaml` (~54), `crypto.yaml` (~51 coins), `macro_assets.yaml` (~37 futures +
rates), `macro_calendar.yaml` (FOMC/CPI fixed dates), `news_feeds.yaml` (~35
feeds, each `macro: true/false`), `subreddits.yaml` (~72), `tracked_entities.yaml`
(~20 13F filers by CIK), and `world_anchor.yaml` (ground-truth facts so the LLM
doesn't anchor on stale training priors).

---

## 5. Ingestion layer (the senses)

Each runs on a scheduler interval, off the event loop in a thread, with a
top-level catch that posts errors to `#meta`. Per-item failures are skipped.

| Ingester | Source | ~Cadence | Writes |
|---|---|---|---|
| `filings` | SEC EDGAR `getcurrent` → per-CIK submissions | 3 min | `Filing` (+ posts) |
| `reddit` | Public Reddit RSS (UA-rotated, circuit-broken) + Google-News fallback | 15 min | `RedditMention` |
| `hackernews` | HN Algolia (6h lookback) | 30 min | `HnMention` |
| `prices` (intraday) | yfinance 1m bars, NYSE-gated (crypto/futures 24/7) | 3 min | `PriceBar`, `PriceContext` |
| `prices` (daily / backfill) | yfinance daily / multi-year | 17:00 ET / 6h | `PriceBar` |
| `news` | ~35 RSS/Google-News feeds (macro + geopolitical + crypto) + yfinance per-ticker | 5 min | `NewsItem` |
| `crypto_trending` | CoinGecko trending → promotes coins to watchlist (14d TTL) | 30 min | `Watchlist` |
| `crypto_micro` | Binance/OKX funding, OI, orderbook imbalance | 20 min | `CryptoMicro` |

Key behaviors:

- **Ticker extraction** is disciplined: `$cashtag` / bare-ticker / company-name,
  each gated by watchlist membership and a 54-word blocklist; bare tickers need a
  corroborating signal. News adds an LLM tagging pass, always re-validated against
  the watchlist. Spurious matches that slip through get killed downstream by the
  LLM curators.
- **Self-healing watchlist:** yfinance-dead tickers are pruned after 3 empty
  cycles and their orphaned `PriceContext`/`PriceBar` swept, so a dead coin can't
  resurface as a fake mover. Bad/zero bars are rejected at the source.
- **The watchlist (~700+ names) is autonomous:** S&P 500 + Nasdaq 100 (Wikipedia)
  + curated ETFs/crypto/macro + activity-promoted filers (≥3 filings/30d or any
  8-K/7d, 60d TTL) + crypto-trending (14d TTL), rebuilt weekly.
- **Article bodies** are fetched on demand (direct httpx + BeautifulSoup, Jina
  Reader fallback) and cached in `ArticleBody`; failed paywalls persist as stubs
  so they aren't re-fetched.

---

## 6. Reasoning layer (the pipelines)

These consume ingested data and post to Discord / publish SSE. Heavy = the
reasoning tier, Light = the fast tier. "Calls?" = emits a scored `TradingCall`.
See `ARCHITECTURE.md §5` for the full table; the load-bearing ones:

- **`filings`** — summarize + materiality-score every filing (cheap triage first,
  full form-typed summary only if it clears the bar), then route by score to
  filings/insiders/priority. The materiality prompt is the one prompt the monthly
  tuner owns at runtime.
- **`why_moved`** — reverse-causality explainer: an unexplained price/volume move
  → gather evidence → heavy read → optional forward call.
- **`convergence`** — where filing + social + price + news stack on the same name
  within a window → a synthesized call.
- **`synthesis`** (the octopus) — every 6h, a system-wide connected read across
  all arms and asset classes that ingests its own prior reads and how its recent
  calls resolved, then writes an *update* and may commit calls.
- **`macro_themes`** (macro desk) — news → transmission chain → exposed names →
  committed read, every 4h, from the ~35 curated feeds.
- **`funding_squeeze`** — deterministic crypto setups (squeeze-long on deeply
  negative funding + price up; funding-fade on crowded positive funding; OI-confirm
  on a leverage surge), gated by BTC regime and orderbook agreement.
- **`book_risk`** — proactive scan of *your* open paper positions, pinging only on
  real trouble (drawdown bucket, earnings imminent, or a fresh filing/news
  contradicting the thesis), with cooldown + escalation.
- **`position_review`** — pre-market hold/trim/close/flag verdicts on open
  positions (deterministic gate first, heavy LLM only on flagged names).
- **`game_plan`** — the **Morning Game Plan** (08:45 ET weekdays, web-only): a
  deterministic assembler fuses book risk, maturing calls, today's catalysts and
  fresh ideas into one bundle of real figures; the heavy LLM only *ranks, dedupes
  and phrases* it (never invents a number) into a single prioritised action list
  surfaced on the Overview. Fail-open — if the LLM is down, the unranked bundle is
  persisted so the panel still renders.
- **`lounge`** — twice-daily #general aside: a grounded geopolitics→mechanism→name
  chain or an absurd-but-true observation, gated `SKIP` when there's nothing real.
- **Automation pipelines** (no LLM): `auto_exits` (enforce stops/targets/trailing
  stops), `auto_thesis` (promote 5/5 calls to theses), `risk_circuit` (pause new
  opens on −15% wallet drawdown), `call_review` (post matured verdicts),
  `auto_research_pre_earnings` (queue research ahead of earnings).

---

## 7. The accountability spine (how it's all tied together)

This is the part to understand — it's why the system is a loop, not a feed.

1. **A call is made.** `synthesis`, `why_moved`, `convergence`, `macro_themes`,
   and `funding_squeeze` emit `CALL: $TICKER LONG|SHORT <1-5>`.
2. **One chokepoint: `scorecard.record_call`.** It de-dupes a re-emitted standing
   idea; **auto-fades** (over ≥12 scored calls in a 90-day window, a source with
   measured negative edge has its conviction mechanically trimmed — −1 at 40–45%
   hit-rate, −2 at 33–40%, hard fade below 33%, floored at 1, never inflated, and
   self-healing as the window rolls); **fact-verifies** the thesis against live
   `PriceContext` (a contradicted figure → `grounded=False`, conviction floored,
   a ⚠ note, and a `#meta` alert — but the call is *always* recorded, fail-open);
   and stores a `TradingCall` with the price at call time.
3. **It propagates for free.** Conviction lives on the call, so the fade
   automatically shrinks fund position size, can drop a call below a wallet's
   `min_conviction` gate, lowers `call_review` notability, and rebuckets
   `wallet_meta`. Nothing else needs changing.
4. **It's marked to market.** `mark_calls` fills 1d/5d/20d returns from `PriceBar`;
   an unscoreable call (no/stale price) retires *unscored*, never fabricated.
5. **It's graded & shown.** `scorecard` computes hit-rate by source and conviction
   (`!scorecard`); `call_review` posts the deterministic verdict on matured calls.
6. **It's traded.** The wallets consume the same `TradingCall` stream.
7. **It's measured as edge.** `wallet_meta` attributes realized P&L by source /
   conviction / asset and runs the headline experiments.
8. **It feeds the next read.** `synthesis` ingests its own track-record brief and
   wallet edge and is told to fade what it's bad at. The loop closes.

Other shared chokepoints: `discord_client.post_embed` (consistent timestamp +
importance badge + actions view); `get_prompt` (DB active row → code constant —
code-authoritative except the tuner-owned `materiality`); `narrative.record_event`
(per-ticker dated memory that synthesis and `!timeline` read back, and the
supersede/dedup backbone for story coalescing).

---

## 8. The autonomous wallets (the live experiment)

Seven paper accounts trade the **same** `TradingCall` stream under different
deterministic policies (`funds._POLICIES`), starting at $10,000 each. No LLM in
the trade loop — pure rules, reproducible, a clean apples-to-apples comparison of
which mandate works on the bot's ideas. An eighth wallet, **research**, trades
only when you execute a Research Desk recommendation.

| Wallet | Mandate | Distinguishing rule |
|---|---|---|
| 🦍 degen | Fast momentum off why_moved/convergence, crypto-friendly | aggressive sizing, tight leash (min conv 3) |
| 🎯 catalyst | convergence/synthesis, equities only, high-conviction | patient (min conv 4) |
| 🌐 macro | synthesis cross-asset only | few big, long hold (max 4 positions) |
| 🪙 crypto | coins only, 24/7 | requires fresh funding/OI microstructure |
| 🔭 sniper | ONLY 5/5-conviction convergence/synthesis | are the best calls elite? (max 3) |
| 📈 leaders | **trend-aligned** momentum; rides names already trending its way | never fades strength (requires trend alignment) |
| 🚀 hype | only calls the crowd is **also** loud about (≥4 Reddit posts/18h) | does retail confirmation help? |
| 🔬 research | user-directed via the Research Desk | trades only on `execute()`, conviction floor 3, 3/day |

> **Note for readers of the old docs:** the earlier "contrarian" wallet has been
> **retired and replaced with `leaders`** (trend-aligned). On first boot the old
> contrarian row is renamed in place and its open positions are risk-managed out
> under the new policy. The current roster is the eight above.

The **degen vs leaders vs hype** triangle is a designed hypothesis test: does the
bot's momentum signal have real edge, and does trend-structure or crowd
confirmation sharpen or dull it? `wallet_meta` (`!meta`, the web Portfolio page,
the weekly Sunday post) answers it — **sample-gated**: it refuses to call an edge
real below its minimum closed-trade count.

Mechanics, all symmetric long/short and verified by tests:

- **Cash convention:** long open `cash -= qty·entry`; short open `cash += qty·entry`;
  `equity = cash + Σ long·mark − Σ short·mark`.
- **No leverage:** committed notional + new size ≤ equity (symmetric; short
  proceeds can't back-door leverage into longs).
- **Fixed-risk sizing:** size scales with conviction (`·conviction/5`), a
  drawdown scale, and a per-source **edge multiplier** (0.3×–1.5×, derived from
  90-day signal attribution, sample-shrunk toward 1.0 on low n).
- **Earnings blackout:** no wallet opens or flips into a name reporting within 2
  days (1 day after). Existing holds ride.
- **Stale/zero price never invents P&L:** a dead-feed position is force-closed at
  entry at max-hold, never stopped out at a fabricated −100%.
- **Reasoning is narrated:** each cycle posts *why* it opened/closed (the
  triggering call's thesis + the mechanical exit reason).

Inspect with `!funds`, `!fund <name>`, `!meta`, or the web **Portfolio** page
(standings, per-wallet detail, live policy editor, reset).

---

## 9. The proactive copilot layer

These make it an assistant, not a dashboard:

- **book_risk** — watches *your* open positions and pings only when one is in
  trouble (adverse-drawdown bucket, earnings imminent, fresh contradicting
  filing/news), with cooldown + escalation. Deterministic detection; the LLM only
  writes the call (cut/trim/hold/add). Logged as a narrative event so synthesis
  knows it warned you.
- **position_review** — the 08:00 ET pre-market pass: a deterministic gate flags
  positions (material filing since entry, opposite high-conviction call, adverse
  >1.5× ATR move, earnings in 3 days, thesis invalidated), then one batched heavy
  call writes hold/trim/close/flag verdicts into the trade journal.
- **macro desk + lounge** — the same connective reasoning in two registers: the
  macro desk commits an accountable scored call; the lounge drops the
  non-consensus version in #general.
- **Research Desk** — you ask a plain-English question (`!research …` or the web
  Research page); it builds a dossier and a TRADE/WATCHLIST/PASS recommendation
  with ticker/direction/conviction/size; nothing trades until you `execute` it
  (conviction floor 3/5, max 3 executions/day). Full audit trail in `ResearchTask`.

---

## 10. Operations & self-monitoring

`#meta` is the bot's console. The **daily health diagnostic** (`!health` or the
08:00 post, also the web **System** page) leads with a ✅/⚠️/🔴 verdict and hunts
*silent* rot:

- **dropped/hung jobs** — flagged when a job's gap exceeds 3× its own 7-day median
  cadence (self-calibrating, so a 3-min poller alarms at ~10m while a weekly job
  only after ~3 weeks);
- **dead ingest streams** — zero price bars in 24h = critical; zero
  filings/news/reddit = warning;
- **stale crypto marks** — a 24/7 feed, so staleness is unambiguous;
- **LLM failure rate** — counted at the `complete()` boundary, with a live
  token-spend and `$` estimate;
- **auto-fade activity** — which sources are currently being dampened;
- **grounding** — the fact-verification contradiction rate over the last 24h/7d
  (from `ClaimCheck` rows), with the worst sample; warns when the bot is
  repeatedly stating figures that disagree with its own ground truth (>10% over a
  real sample). Also rendered as a card on the web **System** page.

Expected, non-bug log lines: LLM timeouts under local compute saturation (the
pipeline skips, no corruption) and APScheduler "maximum number of running
instances reached" (correctly skipping an overlapping slow run).

---

## 11. Chat command reference (Discord)

`@mention` the bot or use `!`:

| Command | Does |
|---|---|
| `!ask <q>` / `@mention` | Free-form Q&A grounded in the DB + research; long answers are chunked, not truncated. In a bot-created thread, every message is answered with post + thread history as context |
| `!status` · `!recent [N]` · `!ticker NVDA` · `!news [T] [N]` · `!filing <accession>` | Readouts (ticker accepts equities, crypto, futures) |
| `!hold T` · `!unhold T` · `!holdings` | Relevance watch (tags a ticker everywhere; not P&L) |
| `!buy T QTY [px] [note]` · `!short T QTY` · `!close T` | Manual paper book (one open position per ticker; positive price enforced) |
| `!positions` · `!pnl` | Book + net unrealized/realized |
| `!tv` | Importable TradingView watchlist + chart links |
| `!scorecard` · `!calls` | Call track record (by source/conviction) and itemized maturing + resolved verdicts |
| `!funds` · `!fund <name>` · `!meta` | Wallet standings / detail / edge readout |
| `!theses` · `!thesis <id>` | Active running hypotheses and their detail |
| `!research <plain English>` | Kick off a Research Desk task (dossier + recommendation appear on the web Research page) |
| `!watch <plain English>` · `!watches` · `!unwatch <id>` | Custom NL alerts compiled to a constrained spec |
| `!timeline T` · `!catalysts` · `!world` · `!health` · `!help` | Per-ticker memory, forward calendar, grounding anchor, diagnostic, help |

Post buttons: **🤖 Ask AI** opens a discussion thread on any post; **👍 Useful** /
**👎 Noise** feed the monthly materiality tuner.

---

## 12. The web dashboard

The same process serves a web app on `127.0.0.1:8730` (configurable, localhost by
design). It reads through the same WAL engine and shares accessors with the
Discord bot — one voice, no second writer. A mount failure logs and the bot runs
on.

- **SvelteKit app at `/app`** (Svelte 5 + SvelteKit 2 + Tailwind 4 + TanStack
  Query, built static into `frontend/build/`). Pages: **Overview** (the **Morning
  Game Plan** ranked action list up top, then KPIs, equity curve, activity), **Markets** (watchlist + funding screener + movers),
  **Symbol** detail, **Crypto** (screener + funding setups + BTC regime),
  **Book** (open positions + risk controls), **Journal** (closed trades + notes),
  **Calls**, **Intel** (news + filings + Reddit), **Feed**, **Analytics**,
  **Theses**, **Research**, **Copilot** (the same grounded chat as Discord),
  **Lookup**, **Watches**, **Portfolio** (wallets + policy editor), **Compare**,
  **Settings** (prompt editor), **System** (health, resources, tool-call log,
  log tail).
- **FastAPI at `/api`** — ~22 routers backing those pages, plus `/api/events`, a
  Server-Sent-Events stream (with `Last-Event-ID` replay) so the UI updates live
  as pipelines publish.
- **NiceGUI at `/`** — the original in-process cockpit, still mounted at root. The
  swap to make SvelteKit primary is planned but not yet flipped.

To develop the frontend live, run `pnpm dev` in `frontend/` (Vite proxies `/api`
to the running bot). For production it's pre-built and committed, so a deploy
target needs no Node.

---

## 13. Data model & prompts

**Tables (34):** see `ARCHITECTURE.md §3` for the grouped list. The spine is
`TradingCall` (every call, marked to market) with `Thesis`/`ThesisEvent` (running
hypotheses) and `NarrativeEvent` (per-ticker memory) around it; `Fund`/`FundTrade`/
`FundEquity` for the wallets; `Filing`, `NewsItem`, `RedditMention`, `HnMention`,
`PriceBar`/`PriceContext`/`CryptoMicro` for inputs; and `PromptVersion`,
`Feedback`, `JobRun` for ops. New tables auto-create; column adds are additive
migrations.

**Prompts** live in `prompts.py` as module constants, are DB-seeded into
`PromptVersion`, and resolve via `get_prompt` (DB active row → code constant). All
are **code-authoritative** (edit the constant, restart, it reconciles) **except
`materiality`**, which the monthly feedback tuner owns at runtime. Literal `$` in
prompt bodies is escaped `$$`. Tests pin that every prompt registers and
substitutes cleanly. The filing summarizers are form-typed (`summarize_8k`,
`summarize_form4`, `summarize_10q`, `summarize_10k`, `summarize_13f`,
`summarize_offering`, `summarize_proxy`, `summarize_generic`); the scorer is
`materiality` (0–3 with reason); plus `tag_sentiment`, `social_pulse`,
`daily_digest`, `tuning_suggest`, the synthesis/why_moved/convergence/macro reads,
and the dossier/copilot prompts.

---

## 14. A day in the life

- **An 8-K drops.** `filings` triages it, summarizes (form-typed), scores
  materiality (tunable prompt); if material it posts to filings/priority and
  records a `NarrativeEvent`. If social is also spiking on the name, `convergence`
  stacks the signals into a `CALL` → `record_call` (auto-fade may trim conviction)
  → wallets size a position → 5 days on, `mark_calls` + `call_review` post the
  verdict → `wallet_meta` attributes the P&L to source `convergence`.
- **"Iran–Israel strikes escalate" hits the feeds.** Tagged `is_macro`. 4h later
  the macro desk walks `strikes → tanker insurance → crude → $XOM/$XLE`, commits a
  lean, emits a scored call. The lounge may drop the non-consensus version in
  #general that evening. If you're long an exposed name, `book_risk` flags it.
- **You `!buy NVDA 50`.** It enters your paper book. `book_risk` now watches it; if
  NVDA reports in 2 days or an 8-K breaks the thesis or it draws down, you get a
  ping with the actual call. `synthesis` leads its next read with your held names.
- **why_moved has been wrong a lot.** After 12+ scored calls under 45% hit-rate,
  `record_call` auto-fades every new why_moved call's conviction; wallets size them
  smaller or skip them; the health digest shows "Auto-fade active: why_moved …";
  and `leaders`/`hype` vs `degen` in `!meta` starts telling you whether the
  momentum signal has edge at all.

---

## 15. Non-goals & stance

**Deliberately not built** (it fights the spine): auto-trading or broker
integration (paper-only, by design), backtesting framework (the wallets *are* the
forward test), multi-user / permissions / ACLs, a *public or remote* web surface
(the dashboard is single-user localhost), paid news APIs, X/Twitter scraping, and
adding raw feeds purely for breadth.

**On opinions, calls, and advice.** This is a single-user, personal, paper-only
copilot — not a regulated product and not a neutral newswire. It is *expected* to
think: form conclusions, take directional views, size conviction, and own the
outcome. The scorecard and the autonomous wallets exist to keep it honest about
those calls — accountability, not abstention, is the discipline. So: no
disclaimers, no refusing a take, predictions and directional calls are core. The
one rule that is never relaxed: **never fabricate a number, price, or fact** —
now backed by a real check (`verify.py` flags any hard figure that disagrees
with live `PriceContext`, at the call + post chokepoints). Ground every claim in
the data or real market knowledge, separate fact from inference from bet — but
still bet.

---

## 16. Where the project can go next

The system is at a maturity inflection — more arms now has diminishing returns and
fights the spine. Honest next moves, roughly in value order:

- **Pre-mortem on bold calls.** Before a conviction-≥4 call is logged, a cheap
  "argue the other side" pass; indefensible → conviction cut. A proactive
  complement to the reactive auto-fade.
- **User memory.** A lightweight store of your stated views / risk tolerance /
  decisions ("I don't short", "took profits on X") that synthesis / book_risk /
  copilot consult — turning a stateless oracle into a copilot that knows *you*.
- **Unified morning game-plan.** One synthesized pre-open brief (book risk +
  catalysts + overnight macro + maturing calls + what I'd do) that genuinely
  dedupes across pipelines rather than stacking them.
- **Let the data drive the wallets.** Opt the macro wallet into trading
  `macro_themes` calls if their scorecard edge proves out; auto-tilt synthesis
  conviction from `wallet_meta`'s momentum verdict; per-source/regime edge-decay
  analysis → dynamic cadence.
- **Flip the UI.** Finish SvelteKit feature parity and make `/app` primary,
  retiring (or demoting) the NiceGUI cockpit.

---

*Paper only. Opinionated by design. It bets — it never invents the evidence.*
