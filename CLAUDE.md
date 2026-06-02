# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo. This reflects how the
code **actually** works as of June 2026. For the full picture read
`docs/ARCHITECTURE.md` (wiring) and `docs/HANDBOOK.md` (features).

## What this is

**Sentinel** — a personal, single-user, **paper-only** trading-intelligence
copilot. One Python 3.12 process ingests SEC filings, Reddit, Hacker News, news,
prices, and crypto microstructure; reasons over them with an LLM to produce
directional calls; trades those calls in autonomous paper wallets; measures and
auto-fades by realized edge; and surfaces everything in Discord and an in-process
web dashboard. It is **opinionated by design** — it concludes, predicts, and
advises. The one inviolable rule: **never fabricate a number, price, or fact.**

## Run / test / lint

```bash
uv sync                                        # deps
uv run python -m sentinel.main                 # live: scheduler + Discord + web
uv run python -m sentinel.main --preflight     # boot go/no-go checks
uv run python -m sentinel.main --run-once <job>  # single-cycle debug
uv run python -m sentinel.main --reset         # archive DB → data/backups/, recreate
uv run pytest -q                               # ~365 tests, ~seconds
uv run ruff check src tests                    # lint
```

`--run-once <job>` takes any scheduled job name (see `_RUN_ONCE_REGISTRY` in
`main.py`). Useful flags: `--skip-watchlist`, `--skip-llm`. Start the live bot on
a **weekday** — equities only poll during NYSE hours.

Frontend (only when changing the SvelteKit UI): `cd frontend && pnpm install &&
pnpm dev` (proxies `/api` to the running bot on :8730). Production build is
`pnpm build` → `frontend/build/`, which **is committed** so deploys need no Node.

## Architecture in one breath

Four subsystems on one asyncio loop, one SQLite DB (`data/radar.db`, WAL):

1. **Ingestion** (`ingesters/`, `edgar/`) — passive, scheduled, **no LLM**.
2. **Reasoning** (`pipelines/`, `analytics/`) — LLM + analytic processors.
3. **Accountability + wallets** (`scorecard.py`, `funds.py`) — calls logged,
   marked to market, graded, auto-faded, traded.
4. **Surface** — Discord (`discord_client.py`, `chat.py`, `interactions.py`) and
   web (`api/` FastAPI + `dashboard/` NiceGUI/SvelteKit mount + `frontend/`).

`scheduler.py` runs 44 jobs; `main.py` is the entrypoint; `config.py` + `.env` are
settings; `models.py` is 36 SQLModel tables; `db.py` is the engine + migrations.

## The chokepoints — route through these, don't fork them

This is the single most important convention. Each concern has exactly one funnel:

| Concern | Funnel | Notes |
|---|---|---|
| Record a directional call | `scorecard.record_call(...)` | de-dupes + auto-fades + **fact-verifies** + stores `TradingCall`. Never insert `TradingCall` directly. |
| Post to Discord | `discord_client.post_embed(...)` | stamps timestamp + importance badge + `PostActionsView`; **fact-verifies** the embed (importance ≥ `VERIFY_MIN_IMPORTANCE`). |
| Verify emitted numbers | `verify.verify_text(text, tickers, …)` | extracts hard ticker-bound figures, checks vs `PriceContext`. Annotates/flags, never blocks; fail-open. |
| Pick a ticker's channel | `routing.channel_for(ticker, default)` | crypto → `#crypto`, else the default. |
| Read a prompt | `prompts.get_prompt(name)` | DB active row → code constant. |
| DB transaction | `db.session_scope()` | auto rollback/close, `expire_on_commit=False`. |
| Per-ticker memory | `narrative.record_event(...)` | feeds synthesis + `!timeline`; supersede/dedup. |
| Live UI event | `events.publish(kind, payload)` | drives the `/api/events` SSE stream. |
| LLM call | `llm.get_llm().complete(...)` | tier-aware, retry, JSON parse, token/cost tracking. |
| Wallet policy | `funds._POLICIES` | one dict; sizing/risk/gates derive from it. |

When you add a feature, wire it through the relevant funnel so the behavior
(fade, dedup, badge, routing, telemetry) propagates for free.

## Invariants (do not break)

- **Never fabricate.** A stale/zero price must not become P&L; an unscoreable call
  retires *unscored*; verdicts and the health report are deterministic arithmetic,
  never an LLM guess. Reason boldly, invent nothing. This is now *checked*:
  `verify.py` extracts the hard ticker-bound numbers the LLM emits and compares
  them to `PriceContext` at the call + post chokepoints — it annotates and flags
  (⚠ field / `grounded=False` / floored conviction / `#meta` line), but **never
  blocks**, and is fully fail-open. Don't make it block, and don't widen its
  metric set to anything that isn't deterministically ground-truthable.
- **No disclaimers / no abstention.** This is a private paper tool with no
  compliance surface. Do **not** add "not financial advice" boilerplate or make
  pipelines refuse to take a directional view — the call *is* the product.
- **Noise reduction over coverage.** Pipelines stay silent unless they have
  something real to say; gates and `SKIP` returns are load-bearing, not optional.
- **Scheduler ↔ run-once parity.** Every scheduled job must also work via
  `--run-once` (only the weekly watchlist rebuild is exempt).
- **One process, one DB, one voice.** Discord and the web share accessors and the
  same DB. Don't add a second writer or fork logic between surfaces.
- **Paper only.** No broker/auto-trading, no real money, no multi-user, no
  public/remote web surface (the dashboard is localhost by design).

## Conventions

- **Settings:** all config flows through `config.py`'s `Settings` singleton. No
  `os.getenv` elsewhere. Add new knobs there + to `.env.example`.
- **Time:** store UTC; convert to ET only at display / market-hours boundaries.
- **DB migrations:** additive only. New tables auto-create on boot; new columns go
  in `db.py`'s `_migrate_add_columns` as nullable `ADD COLUMN`. Don't rewrite or
  drop columns.
- **Failure policy:** every pipeline catches at the top level, logs with traceback,
  posts a one-line alert to `#meta`, and continues. Per-item failures are skipped,
  never fatal. The system must never crash on bad data.
- **LLM tiers:** `light` for high-volume/classification/JSON; `heavy` for
  reasoning/narrative. JSON/structured calls force reasoning off. Heavy calls may
  `fallback_light`. Defaults are local Ollama (`gemma4:e4b` / `qwen3:30b-a3b`) but
  `.env` can route either tier to any OpenAI-compatible API — this deployment uses
  `deepseek/deepseek-v4-flash` for both. Check `.env` for the live config.
- **Grounding:** LLM calls get a grounding preamble (`grounding.py` +
  `config/world_anchor.yaml`) to counter training-cutoff bias. Keep world facts in
  the YAML, not hardcoded.
- **Prompts:** code-authoritative in `prompts.py` except `materiality`, which the
  monthly tuner owns at runtime. Escape literal `$` as `$$`. Every prompt must
  register and substitute cleanly (tests enforce this).
- **Tests:** ~365 deterministic tests in `tests/` pin the load-bearing math and
  gates (sizing, cash mechanics, auto-fade, scoring, ticker extraction, dedup).
  Add/adjust tests when you touch that math; keep them deterministic (no network,
  no real LLM).
- **Style:** match the surrounding code; keep modules focused; prefer extending a
  chokepoint over adding a parallel path. Run `ruff` before finishing.

## Where things live

```
src/sentinel/
  main.py scheduler.py config.py db.py models.py        # backbone
  llm.py llm_tools.py grounding.py                       # LLM
  scorecard.py funds.py thesis.py narrative.py portfolio.py  # spine + wallets + book
  research_desk.py research.py dossier.py                # research
  routing.py discord_client.py chat.py feedback.py interactions.py ui.py  # Discord
  edgar/ ingesters/                                      # ingestion (no LLM)
  pipelines/                                             # ~25 reasoning/automation pipelines
  analytics/                                             # ~19 read-only compute modules
  api/                                                   # FastAPI routers (/api)
  dashboard/                                             # NiceGUI cockpit (/) + SvelteKit mount (/app)
config/    *.yaml universe + feeds + world anchor
frontend/  SvelteKit app (build/ committed, served at /app)
tests/     ~365 deterministic tests
docs/      ARCHITECTURE.md, HANDBOOK.md
```

## Gotchas

- `pyproject.toml` still lists `praw`, but the Reddit ingester now uses **public
  RSS** (UA-rotated, circuit-broken), not PRAW. The dep is vestigial.
- The NiceGUI cockpit (`/`) and the SvelteKit app (`/app`) both run today; the
  swap to make SvelteKit primary is planned but **not yet flipped**. Don't assume
  `/` is the modern UI.
- `data/` (the DB, logs, backups) and `.claude/` are gitignored. `frontend/build/`
  is **not** — keep it in sync when you change the frontend.
- The "contrarian" wallet was retired and replaced by the trend-aligned
  **`leaders`** wallet; the current roster is degen, catalyst, macro, crypto,
  sniper, leaders, hype, plus the user-directed research wallet.
- `verify.py` runs an extra **light-LLM** call per recorded call and per
  high-importance post. `change_*_pct` in `PriceContext` are **fractions**
  (0.116) but the verifier (and the extractor prompt) speak **percentage
  points** (11.6) — the conversion is explicit in `_check_pct`; keep it that
  way. The verifier is gated by `VERIFY_ENABLED` (+ the `VERIFY_*` tolerances);
  with it off, behavior is byte-identical to before (no calls, fields, or
  `ClaimCheck` rows). It is **inline at the chokepoints — no scheduled job.**
