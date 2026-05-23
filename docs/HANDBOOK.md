# Sentinel — The Handbook

> The complete "understand it all" guide: every feature, how each piece works,
> how they're wired together, how to run and use it, and where it can go next.
> Personal, single-user, **paper-only** trading-intelligence copilot that lives
> in Discord.

---

## 1. The one-paragraph mental model

Sentinel continuously **ingests** the market's information surface (SEC
filings, Reddit, Hacker News, prices, macro/geopolitical news, crypto
micro-structure), **reasons** over it with a local LLM to produce connected
reads and concrete directional calls, **acts** on those calls in seven
autonomous paper wallets, **measures** whether the calls actually worked,
**auto-corrects** by fading sources it's measurably bad at, and **watches
itself** for silent failure — all posted into Discord channels and queryable
with `!` commands. It is opinionated by design: it's allowed to conclude,
predict, and give advice. The only hard rule is **it never fabricates a
number** — it bets, but it never invents the evidence.

The whole system is one closed loop:

```
ingest → reason → call → trade(paper) → measure → auto-fade → self-monitor
   ↑__________________________________________________________________│
                 (the scorecard feeds the next reason)
```

---

## 2. Design spine (the rules everything obeys)

These are invariants, not preferences. Every feature was built to honor them.

- **Noise reduction over coverage.** A pipeline stays silent unless it has
  something real to say. "An empty channel beats a noisy one" is enforced in
  prompts and gates, not hoped for.
- **Never fabricate.** A stale/zero price never becomes P&L; an unscoreable
  call retires unscored; a verdict is arithmetic, never an LLM guess; the
  health report is deterministic. Reason boldly, invent nothing.
- **Accountable.** Every directional opinion is logged, marked-to-market, and
  scored. The bot's track record is visible and feeds back into its behavior.
- **Free to advise.** Single-user paper tool, no compliance surface — no
  "not financial advice" disclaimers, no refusing a take. The call *is* the
  product. (Earlier spec drafts had template restrictions; removed on purpose.)
- **One chokepoint per concern.** Calls funnel through `record_call`; embeds
  through `post_embed`; prompts through `get_prompt`; DB through
  `session_scope`; wallet policy through `_POLICIES`. Fix once, propagate
  everywhere.
- **Scheduler ↔ registry parity.** Every scheduled job is also a manual
  `--run-once` job (sole intentional exception: the weekly watchlist
  rebuild, a sync bootstrap step).
- **Tested.** ~136 deterministic tests pin the load-bearing math and gates.

---

## 3. Setup & running

### Prerequisites
1. **Ollama** running locally with the two configured models pulled
   (`ollama list`):
   - light: `gemma4:e4b` (fast, used for triage/curation/lounge/chat)
   - heavy: `qwen3:30b-a3b` (reasoning: synthesis / why_moved / convergence /
     macro desk). Large — pre-pull it or first boot stalls downloading.
   - Optional: route "heavy" to an OpenAI-compatible API instead
     (`HEAVY_LLM_API_*`) to stay fast without local GPU.
2. **`.env`** with the Discord bot token + channel IDs (see §4).
3. **`uv`** for dependency management.

### Commands
```bash
# Run the live bot (this is "running it")
uv run python -m sentinel.main

# Single-cycle debug — run any one job once and exit
uv run python -m sentinel.main --run-once synthesis
uv run python -m sentinel.main --run-once filings --skip-watchlist

# The test suite (≠ running the bot; ~5s)
uv run pytest -q

# Lint
uv run ruff check src tests
```

`--run-once <job>` accepts any of: `filings, reddit, hackernews, prices,
prices_daily, prices_backfill, sentiment, social_pulse, digest, tuning,
convergence, movers, briefing, news, macro_themes, news_impact, mark_calls,
call_review, book_risk, health, funds_cycle, funds_digest, funds_meta,
news_alerts, crypto_trending, crypto_micro, synthesis, why_moved, watches,
catalysts, lounge, reddit_feed`. Flags: `--skip-watchlist`, `--skip-llm`.

### Running it for real
- Smoke-test first: `--run-once prices`, `--run-once filings`,
  `--run-once macro_themes`, `--run-once book_risk` — watch `#meta`.
- Then run under something durable: `nohup uv run python -m
  sentinel.main > data/logs/run.out 2>&1 &`, tmux, or systemd.
- **Start on a weekday.** Equities only poll during NYSE hours; on a weekend
  most price-driven pipelines look dead (correctly).
- A day shows **liveness + content quality**. The edge layers
  (scorecard/verdicts/wallet-meta/auto-fade) need **1–3 weeks** because calls
  mature at 1d/5d/20d. Don't judge edge on day one.

### Operational note (SQLite concurrency)
Many pipelines write concurrently from worker threads. The DB engine is
configured WAL + `busy_timeout=30000` + `synchronous=NORMAL` so a contended
writer **waits** instead of erroring. If you ever see `database is locked`
again it means a single write transaction exceeded 30s (e.g. a huge price
backfill) — the structural fix is bulk-insert in the price poller (see §15).

---

## 4. Configuration (`.env`)

| Var | Purpose |
|---|---|
| `DISCORD_TOKEN`, `DISCORD_GUILD_ID` | Bot auth / server |
| `DISCORD_USER_ID` | The owner — receives `@mention` on priority posts |
| **Streams (raw-ish, time-ordered):** | |
| `DISCORD_FILINGS_CHANNEL_ID` | All SEC filings (with materiality scoring) |
| `DISCORD_INSIDERS_CHANNEL_ID` | Form 4 / 13F insider activity |
| `DISCORD_NEWS_CHANNEL_ID` | Per-ticker news + breaking alerts (→ pulse) |
| `DISCORD_MACRO_CHANNEL_ID` | Macro/geopolitical news ONLY (→ news → pulse) |
| `DISCORD_CRYPTO_CHANNEL_ID` | All per-coin crypto content (→ news) |
| `DISCORD_REDDIT_CHANNEL_ID` | Notable r/ posts — *skips* when unset (firehose) |
| `DISCORD_PULSE_CHANNEL_ID` | Social-mention spikes only |
| **Curated (bot's reasoning, lower volume):** | |
| `DISCORD_PRIORITY_CHANNEL_ID` | Material 8-Ks + high-conviction signals |
| `DISCORD_CONVERGENCE_CHANNEL_ID` | Multi-source agreement (→ priority) |
| `DISCORD_HOT_CHANNEL_ID` | Watchlist movers NOW — *skips* when unset (opt-in) |
| `DISCORD_CALLS_CHANNEL_ID` | Call-resolution verdicts (→ digest → meta) |
| `DISCORD_RISK_CHANNEL_ID` | Book-risk alerts on OPEN positions (→ priority → meta) |
| `DISCORD_FUNDS_CHANNEL_ID` | Autonomous-wallet trade narrations (→ digest → meta) |
| **Daily / scheduled / system:** | |
| `DISCORD_DIGEST_CHANNEL_ID` | EOD digest |
| `DISCORD_CATALYSTS_CHANNEL_ID` | Forward catalyst calendar (→ digest → news → pulse) |
| `DISCORD_GENERAL_CHANNEL_ID` | The Lounge — geopolitics↔market musings (→ digest) |
| `DISCORD_META_CHANNEL_ID` | Ops / health / errors — the bot's console |
| `OLLAMA_BASE_URL`, `LLM_MODEL_LIGHT`, `LLM_MODEL_HEAVY` | Local LLM |
| `HEAVY_LLM_API_BASE/KEY/MODEL` | Optional remote heavy model |
| `REDDIT_USER_AGENT` | Override the rotating UA pool (leave blank) |
| `EDGAR_USER_AGENT` | Required by SEC EDGAR fair-use |
| `POLL_*_MINUTES`, `*_HOURS`, `*_HOUR_ET` | Pipeline cadences |
| `FUND_STARTING_CASH` (10,000), `FUNDS_CYCLE_MINUTES` | Wallets |

**Channel fallback** is universal: an unset optional channel degrades to a
sensible parent (never crashes). Unset `DISCORD_REDDIT_CHANNEL_ID` is the one
that *skips* rather than falls back (it would firehose a shared channel).

---

## 5. Ingestion layer (the senses)

Each runs on a scheduler interval, off the event loop in a thread, with a
top-level catch that posts errors to `#meta`. Per-item failures are skipped,
never fatal.

| Ingester | Source | ~Cadence | Writes |
|---|---|---|---|
| `filings` | SEC EDGAR `getcurrent` for watchlist CIKs | 10m | `Filing` (+ posts) |
| `reddit` | Public Reddit RSS (UA-rotated, circuit-broken) + Google-News fallback | 15m | `RedditMention` |
| `hackernews` | HN Algolia | 30m | `HnMention` |
| `prices` (intraday) | yfinance 1m bars, NYSE-hours-gated (crypto/futures 24/7) | 5m | `PriceBar`, `PriceContext` |
| `prices` (daily/backfill) | yfinance daily/60d | 17:00 ET / 6h | `PriceBar` |
| `news` | 32 RSS/Google-News feeds (macro+geopolitical+crypto) + yfinance per-ticker | 5m | `NewsItem` |
| `crypto_trending` | CoinGecko trending → promotes coins to the watchlist (auto-expire) | 30m | `Watchlist` |
| `crypto_micro` | Binance/OKX funding, OI, orderbook imbalance | 20m | `CryptoMicro` |

Key behaviors:
- **Ticker extraction** is disciplined: `$cashtag` regex + watchlist
  membership + a blocklist of common English words. Spurious matches that
  slip through are killed downstream by the LLM curator.
- **Self-healing watchlist**: yfinance-dead tickers get struck 3× then
  auto-pruned; their orphaned `PriceContext`/`PriceBar` are swept so a dead
  coin can't resurface as a fake mover. Bad/zero price bars are rejected at
  the source.
- The watchlist (~700 tickers) is **autonomous**: S&P/Wikipedia index +
  ETFs + activity-promoted + crypto-trending, rebuilt weekly.

---

## 6. Reasoning layer (the pipelines)

These consume the ingested data and post to Discord. Heavy = `qwen3`,
Light = `gemma4`. "Calls?" = does it emit scored `TradingCall`s.

| Pipeline | What it does | LLM | ~Cadence | Calls? | Channel |
|---|---|---|---|---|---|
| `filings` | Summarize + score materiality of each filing | both | 10m | — | filings/insiders/priority |
| `why_moved` | Explains an unexplained price/volume move from evidence, then commits a forward read | heavy | 30m | ✅ | priority/crypto |
| `convergence` | Where filing + social + price + news stack on the same name → a call | heavy | 30m | ✅ | priority |
| `synthesis` | The "octopus": system-wide connected read across all arms, continuous (reads its last briefing + its own track record) | heavy | 6h | ✅ | priority |
| `macro_themes` (macro desk) | News → transmission chain → exposed names → a committed read | heavy | 4h | ✅ | news |
| `lounge` | Off-clock #general: a grounded geopolitics↔market causal chain or absurd-but-true take, gated `SKIP` | light | 11:20 & 17:20 ET | — | general |
| `social_pulse` | Tickers with abnormal Reddit volume + substance/noise judgment | light | 1h | — | pulse |
| `sentiment` | Tags recent Reddit mentions bullish/bearish/thesis | light | 1h | — | (db) |
| `movers` | EOD biggest movers + one-line hypothesis | light | 16:15 ET | — | pulse |
| `news_alerts` | Breaking per-ticker news triage | light | 10m | — | news/crypto |
| `news_impact` | Measures news→price correlation after the fact | — | 1h | — | (db) |
| `briefing` | Pre-market: positions take, not a neutral wire | heavy | 08:30 ET | — | priority |
| `digest` | End-of-day narrative summary + "the read" | heavy | 16:30 ET | — | digest |
| `catalysts` | Computed calendar (OPEX, jobs, FOMC, **earnings dates** persisted) | — | 07:00 ET | — | digest |
| `watches` | User natural-language alerts compiled to a constrained spec, evaluated each cycle | light(compile) | 15m | — | priority |
| `tuning` | Monthly: rewrites the materiality prompt from 👍/👎 feedback | heavy | monthly | — | meta |
| `reddit_feed` | LLM-curated stream of genuinely notable r/ posts (kills spurious matches) | light | 20m | — | reddit |
| `book_risk` | Proactive risk scan of *your* open paper positions | light(read) | 30m | — | risk |
| `call_review` | Posts the verdict on each notable matured call | — | 2h | — | calls |
| `funds_*` | The autonomous wallets (see §8) | — | 1h / EOD / weekly | (consumes) | funds |
| `health` | Self-diagnostic (see §10) | — | 08:00 ET | — | meta |

---

## 7. The accountability spine (how it's *all* tied together)

This is the part to understand — it's why the system is a loop, not a feed.

1. **A call is made.** `synthesis`, `why_moved`, `convergence`, and
   `macro_themes` emit machine lines `CALL: $TICKER LONG|SHORT <1-5>`.
2. **One chokepoint: `scorecard.record_call(...)`.** Every call funnels
   here. It:
   - de-dupes a re-emitted standing idea (won't double-count);
   - **auto-fades**: if that *source* has a measured negative edge over ≥12
     scored calls, conviction is mechanically reduced (fade-only, floors at
     1, never inflates, self-heals via the 90d window). Tagged in the thesis
     (`⚖︎ faded why_moved 4/19`) so it's visible.
   - stores a `TradingCall` with the price at call time.
3. **It propagates for free.** Because conviction lives on the `TradingCall`
   the auto-fade automatically shrinks fund position size
   (`size = equity·size_pct·conviction/5`), can drop a call below a fund's
   `min_conviction` gate, lowers its `call_review` notability, and rebuckets
   `wallet_meta` — *no other system needed changing*.
4. **It's marked to market.** `mark_calls` fills 1d/5d/20d returns from
   `PriceBar` history; an unscoreable call (no/stale price) retires
   *unscored* rather than get a fabricated grade.
5. **It's graded & shown.** `scorecard` computes hit-rate by source &
   conviction (`!scorecard`); `call_review` posts the deterministic verdict
   on notable matured calls (`📒 Called It`).
6. **It's traded.** The wallets consume the same `TradingCall` stream.
7. **It's measured as edge.** `wallet_meta` attributes realized P&L by
   source / conviction / asset and runs the headline experiments.
8. **It feeds the next read.** `synthesis` ingests its own
   `track_record_brief` and `wallet_edge` and is told to fade what it's bad
   at. The loop closes.

Other shared chokepoints: `discord_client.post_embed` (every embed gets a
consistent timestamp + actions view + importance badge); `get_prompt` (DB
PromptVersion → falls back to the code constant; `seed_prompts` reconciles
on boot, code-authoritative except the tuning-owned `materiality`);
`narrative.record_event` (per-ticker dated memory that synthesis and
`!timeline` read back, and the dedup backbone for story coalescing).

---

## 8. The autonomous wallets (the live experiment)

Seven paper accounts trade the **same** `TradingCall` stream under different
deterministic policies (`funds._POLICIES`), starting at $10,000 each. No LLM
in the trade loop — pure rules, reproducible, a clean apples-to-apples
comparison of which mandate works on the bot's ideas.

| Wallet | Mandate | Distinguishing rule |
|---|---|---|
| 🦍 degen | Fast momentum off why_moved/convergence | aggressive sizing, tight leash |
| 🎯 catalyst | convergence/synthesis, equities, high-conviction | patient |
| 🌐 macro | synthesis cross-asset only | few big, long hold |
| 🪙 crypto | coins only, any source | wide bands for crypto vol |
| 🔭 sniper | ONLY 5/5-conviction convergence/synthesis | are the best calls elite? |
| 🪞 contrarian | **FADES** the bot's momentum calls (`invert`) | if it beats degen, the edge is a mirage |
| 🚀 hype | only momentum calls the crowd is **also** surging on | does retail confirmation help? |

The triangle **degen vs contrarian vs hype** is a designed hypothesis test:
does the bot's momentum signal have real edge, and does crowd-confirmation
sharpen or dull it. `wallet_meta` (`!meta`, weekly post) answers it —
**sample-gated**: it refuses to call an edge real below 15 closed trades.

Mechanics, all symmetric long/short and verified by tests:
- **Cash convention**: long open `cash-=qty·entry`; short open
  `cash+=qty·entry`; `equity = cash + Σlong·mark − Σshort·mark`.
- **No leverage, symmetric**: total committed notional + new size ≤ equity
  (closed the old hole where short proceeds back-doored leverage into longs).
- **Earnings blackout**: no wallet *opens or flips into* a position when the
  name reports within 2 days (binary risk). Existing holds ride.
- **Stale/zero price never invents P&L**: a dead-feed position is force-
  closed at entry at max-hold, never stopped out at a fabricated −100%.
- **Reasoning is narrated**: each cycle posts *why* it opened/closed (the
  triggering call's verbatim thesis + the mechanical exit reason).

Inspect with `!funds` (standings), `!fund <name>` (detail, with a live
`marks live · updated Nm ago` freshness line), `!meta` (edge readout).

---

## 9. The proactive copilot layer

These make it an assistant, not a dashboard:

- **book_risk** — watches *your* open paper positions and pings only when
  one is actually in trouble: adverse drawdown bucket, earnings imminent, or
  a fresh material filing/news contradicting the thesis. Cooldown +
  escalation (it won't nag a non-worsening situation; a deeper drawdown or a
  new trigger breaks through). Deterministic detection; the LLM only writes
  the *call* (cut/trim/hold/add). Logged as a `book_risk` narrative event so
  synthesis knows it warned you.
- **lounge** — the connective-reasoning voice: a grounded
  `event → mechanism → exposed name` chain ("if this Hormuz thing holds, $X
  is the trade nobody's pricing"), an absurd-but-true data observation, or a
  community riff. `SKIP`s when there's nothing real. Twice daily.
- **macro desk** — the same connective reasoning, but accountable: news →
  chain → committed read → scored `CALL`. 32 curated feeds feed it.

---

## 10. Operations & self-monitoring

`#meta` is the bot's console. The **daily health diagnostic** (`!health` or
the 08:00 post) leads with a `✅ / ⚠️ / 🔴` verdict and hunts *silent* rot:

- **dropped/hung jobs** — flagged when a job's gap exceeds 3× its own 7-day
  median cadence (self-calibrating: a 3-min poller alarms at ~10m, a weekly
  job only after ~3 weeks — no false alarms);
- **dead ingest streams** — zero bars in 24h = critical (price feed dead);
  zero filings/news/reddit = warning;
- **stale crypto marks** — 24/7 feed, so staleness is unambiguous;
- **LLM failure rate** — counted at the `complete()` boundary;
- **auto-fade activity** — which sources are currently being dampened.

Expected, *non-bug* behaviors you'll see in logs: LLM timeouts under local
compute saturation (degrade gracefully — pipeline skips, no corruption);
`maximum number of running instances reached` (APScheduler correctly
skipping an overlapping slow run — not an error).

---

## 11. Chat command reference

@-mention the bot or use `!`:

| Command | Does |
|---|---|
| `!ask <q>` / @mention | Free-form Q&A grounded in the bot's DB + research; long answers are chunked, not truncated |
| `!status` · `!recent` · `!ticker NVDA` · `!news [T]` | Readouts |
| `!hold T` · `!unhold T` · `!holdings` | Relevance watch (not P&L) |
| `!buy T QTY [px] [note]` · `!short T QTY` · `!close T` | Manual paper book (one open position per ticker; positive price enforced) |
| `!positions` · `!pnl` | Book + net unrealized/realized |
| `!tv` | Importable TradingView watchlist + chart links |
| `!scorecard` | Aggregate call track record (by source/conviction) |
| `!calls` | Itemized: maturing + recently-resolved verdicts |
| `!funds` · `!fund <name>` · `!meta` | Wallet standings / detail / edge readout |
| `!watch <plain English>` · `!watches` · `!unwatch <id>` | Custom NL alerts |
| `!timeline T` | The bot's dated memory for a ticker |
| `!catalysts` | Upcoming computed calendar + earnings |
| `!filing <accession>` · `!health` · `!help` | Misc |

Post reactions: 👍 / 👎 feed the monthly materiality tuner; 🤖 "Ask AI"
opens a discussion thread on any post.

---

## 12. Data model (tables)

`Watchlist`, `TrackedEntity` (the universe) · `Filing`, `SeenFiling` ·
`RedditMention`, `HnMention`, `NewsItem` (social/news) · `PriceBar`,
`PriceContext`, `CryptoMicro`, `EarningsDate` (market) · `SocialPulse` ·
`Feedback` (👍/👎) · `PaperTrade`, `Holding` (your book) · `Fund`,
`FundTrade`, `FundEquity` (wallets) · `TradingCall` (the accountability
spine) · `NarrativeEvent` (per-ticker memory) · `JobRun` (health) ·
`Watch` (NL alerts) · `PromptVersion` (live-tunable prompts) ·
`PendingTuning` (durable tuning proposal). New tables auto-create; column
adds are additive migrations.

---

## 13. The prompt system

18 registered prompts live in `prompts.py`, are DB-seeded into
`PromptVersion`, and resolved via `get_prompt` (DB active row → code
constant). All are **code-authoritative** (edit the constant, restart, it
reconciles) **except `materiality`**, which the monthly feedback tuner owns
at runtime. Literal `$` in prompt bodies is escaped `$$`. Tests pin that
every prompt registers and substitutes cleanly.

---

## 14. Worked examples (a day in the life)

- **An 8-K drops.** `filings` summarizes it (light) + scores materiality
  (LLM, tunable prompt); if material it posts to filings/priority, records a
  `NarrativeEvent`. If social is also spiking on the name, `convergence`
  later stacks the signals into a `CALL` → `record_call` (auto-fade may
  trim conviction) → wallets size a position → 5 days on, `mark_calls` +
  `call_review` post the verdict → `wallet_meta` attributes the P&L to
  source `convergence`.
- **"Iran-Israel strikes escalate" hits the feeds.** It's tagged
  `is_macro`. 4h later the **macro desk** walks
  `strikes → tanker insurance → crude → $XOM/$XLE`, commits a lean, emits a
  scored `CALL`. The **lounge** may drop the non-consensus version in
  #general that evening. If you're long an exposed name, **book_risk** flags
  it.
- **You `!buy NVDA 50`.** It enters your paper book. **book_risk** now
  watches it; if NVDA reports in 2 days or an 8-K breaks the thesis or it
  draws down, you get a ping with the actual call. `synthesis` leads its
  next read with your held names.
- **why_moved has been wrong a lot.** After 12+ scored calls at <45%
  hit-rate, `record_call` auto-fades every new why_moved call's conviction;
  wallets size them smaller or skip them; the health digest shows
  "Auto-fade active: why_moved …"; the contrarian wallet (which fades them)
  starts visibly out-performing degen in `!meta`.

---

## 15. Possibilities, ideas & roadmap

The system is at a **maturity inflection** — more arms now has diminishing
returns and fights the spine. The honest next moves, in rough value order:

**Shipped since first draft** (kept here so the doc stays the source of truth)
- ✅ **SQLite concurrency**: WAL + `busy_timeout` engine config, *and* the
  root fix — `_persist_bars` is now one bulk `INSERT … ON CONFLICT DO
  NOTHING` (lock held ms, not seconds; dedup via the existing constraint).
- ✅ **Reddit top comments**: lazy, breaker-aware enrichment of notable
  candidates — the curator now judges the discussion, not just the post.
- ✅ **Ask-AI thread ordering**: deterministic seed-first (placeholder →
  edited-in brief; follow-ups held until the brief lands).
- ✅ **In-process cockpit** (`dashboard/`, NiceGUI): a localhost-only web
  console mounted on the bot's own event loop (uvicorn as a loop task;
  reads through the same WAL engine — no second writer). Live health/jobs/
  wallets/scorecard/book/resources/log-tail, a control surface (pause·
  resume·run-now a job, manual CALL via `scorecard.record_call`), and a
  chatbox on the **shared** `chat.answer_question` path (one voice with
  Discord). Isolated: a mount failure logs and the bot runs on. Flags:
  `DASHBOARD_ENABLED/HOST/PORT`. Presentation rule: panels render real
  structured components (tables, stat tiles, a level-coloured log viewer,
  chat bubbles) fed by the **structured** accessors — never by dumping a
  Discord markdown string into a card. To that end `health.health_report()`
  is the structured twin of `health_text()` (same detectors/verdict; the
  text fn is left byte-identical because its output is pinned + shipped to
  Discord). One injected stylesheet (`app._THEME_CSS`, `shared=True`) is
  the single source of visual truth (tokens, card chrome, tables, alerts).

**Operational / low-risk (open)**
- **Preflight `--preflight`.** Boot-time go/no-go: Ollama + models present,
  channels resolve, DB writable. The boot-half of the self-diagnostic.

**Quality / intelligence**
- **Pre-mortem on bold calls.** Before a conviction≥4 call is logged, a
  cheap "argue the other side" pass; indefensible → conviction cut. A
  proactive complement to the reactive auto-fade.
- **User memory.** A lightweight store of your stated views / risk
  tolerance / decisions ("I don't short", "took profits on X") that
  synthesis / book_risk / chat consult — turns a stateless oracle into a
  copilot that knows *you*.
- **Unified morning game-plan.** One synthesized pre-open brief (book risk +
  catalysts + overnight macro + maturing calls + what I'd do) — must
  genuinely dedupe across pipelines, not stack them.

**Experiments the data will unlock (weeks in)**
- Opt the **macro** wallet into trading `macro_themes` calls if their
  scorecard edge proves out (one-line `_POLICIES` change).
- Read `wallet_meta`'s momentum verdict to **auto-tilt** synthesis
  conviction (currently it only reads it as text).
- Per-source/regime edge decay analysis → dynamic cadence (poll/think more
  where edge is live).

**Deliberately *not* doing** (fights the spine): more raw feeds for
breadth's sake, auto-trading/broker integration, multi-user, a *public or
remote* web surface (the cockpit is single-user localhost by design — it
augments Discord, it doesn't become a product), backtesting framework (the
wallets *are* the forward test).

---

## 16. Where to look in the code

| Concern | File |
|---|---|
| Orchestration (all cadences) | `scheduler.py` |
| Manual single-cycle / entrypoint | `main.py` |
| Accountability + auto-fade | `scorecard.py` |
| The wallets + meta-analysis | `funds.py` |
| The brain | `pipelines/synthesis.py` |
| News desk | `pipelines/macro_themes.py` + `config/news_feeds.yaml` |
| Proactive risk | `pipelines/book_risk.py` |
| All prompts | `prompts.py` |
| Embed/timestamp/colors | `discord_client.py`, `ui.py` |
| DB engine / concurrency | `db.py` |
| Per-ticker memory | `narrative.py` |
| Self-diagnostic | `health.py` (`health_text` → Discord, `health_report` → cockpit) |
| In-process cockpit | `dashboard/` (`app.py` page+theme, `sysinfo.py`, `logbuf.py`) |
| Tests (the behavioral spec) | `tests/` (~159) |

---

*Paper only. Opinionated by design. It bets — it never invents the evidence.*
