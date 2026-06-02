# Sentinel

A personal, single-user, **paper-only** trading-intelligence copilot. It
continuously ingests the market's information surface (SEC filings, Reddit,
Hacker News, prices, macro/geopolitical news, crypto microstructure), reasons
over it with an LLM to produce connected reads and concrete directional calls,
acts on those calls in a set of autonomous paper wallets, measures whether the
calls worked, and auto-corrects by fading the sources it's measurably bad at.

It surfaces everything two ways: a **Discord bot** (channels + `!commands`) and
an **in-process web dashboard** (a SvelteKit app served at `/app`, with a legacy
NiceGUI cockpit at `/`). It is opinionated by design — it's allowed to conclude,
predict, and advise. The one hard rule is that it **never fabricates a number**.

> For the full picture, read `docs/HANDBOOK.md` (the complete feature guide) and
> `docs/ARCHITECTURE.md` (how it's wired). `CLAUDE.md` documents the code
> conventions for anyone (human or agent) working in the repo.

## What's in the box

- **Ingestion** — passive, scheduled, no-LLM collectors for SEC EDGAR, Reddit
  (public RSS), Hacker News, ~35 news/RSS feeds, yfinance prices, and crypto
  microstructure (Binance/OKX funding/OI/orderbook + CoinGecko trending).
- **Reasoning** — ~25 LLM/analytic pipelines: filing summarization + materiality
  scoring, `why_moved`, `convergence`, the system-wide `synthesis` ("octopus")
  read, a macro desk, social pulse, news alerts, theses, and more.
- **Accountability** — every directional call funnels through `scorecard.record_call`,
  is marked-to-market at 1d/5d/20d, graded by source/conviction, and feeds an
  auto-fade loop that mechanically trims conviction on sources with negative edge.
- **Autonomous wallets** — paper accounts (degen, catalyst, macro, crypto, sniper,
  leaders, hype) that trade the same call stream under different deterministic
  policies, plus a user-directed `research` wallet. No LLM in the trade loop.
- **Surfaces** — a Discord bot and a localhost web dashboard, both reading the
  same SQLite database through the same accessors.

## Tech stack

Python 3.12 (managed with `uv`) · `discord.py` · `apscheduler` · `sqlmodel` over
SQLite (WAL) · `httpx` · `yfinance` · `feedparser` · FastAPI + NiceGUI (the
in-process web server) · LLM via Ollama (local) **or** any OpenAI-compatible API
(OpenRouter, Novita, DeepInfra, Google AI, …). Frontend: SvelteKit 2 + Svelte 5
+ Tailwind 4 + TanStack Query, built to static assets.

## Setup

```bash
uv sync
cp .env.example .env
# fill .env: Discord token + channel IDs, EDGAR user-agent, and either a local
# Ollama endpoint or an OpenAI-compatible LLM API key. See docs/HANDBOOK.md §4.
```

LLM options (pick one per tier; they can differ):

- **Local** — run Ollama and pull the configured models (`LLM_MODEL_LIGHT`,
  `LLM_MODEL_HEAVY`). Defaults are `gemma4:e4b` (light) and `qwen3:30b-a3b` (heavy).
- **Serverless** — set `LLM_API_BASE` / `LLM_API_KEY` / `LLM_API_MODEL_*` to an
  OpenAI-compatible endpoint. Per-tier overrides (`LIGHT_LLM_API_*`,
  `HEAVY_LLM_API_*`) let light and heavy point at different providers.

Boot self-check before the first real run:

```bash
uv run python -m sentinel.main --preflight
```

## Running

```bash
# Live bot + scheduler + web dashboard (this is "running it")
uv run python -m sentinel.main

# Single-cycle debug — run any one job once and exit
uv run python -m sentinel.main --run-once synthesis
uv run python -m sentinel.main --run-once filings --skip-watchlist

# Archive the DB and start clean (backup goes to data/backups/)
uv run python -m sentinel.main --reset

# Tests (~365 tests) and lint
uv run pytest -q
uv run ruff check src tests
```

The web dashboard comes up in the same process on `http://127.0.0.1:8730`
(`/app` for the SvelteKit UI, `/` for the legacy NiceGUI cockpit). The frontend
is pre-built into `frontend/build/` and committed; to develop it live, run
`pnpm dev` in `frontend/` (it proxies `/api` to the running bot).

Start on a weekday — equities only poll during NYSE hours, so on a weekend most
price-driven pipelines correctly look quiet. A day shows liveness and content
quality; the edge layers (scorecard, verdicts, wallet meta, auto-fade) need
**1–3 weeks** because calls mature at 1d/5d/20d.

## Project layout

```
config/        # YAML: indices, etfs, crypto, macro assets/calendar, news feeds,
               # subreddits, tracked 13F entities, world-state anchor
data/          # radar.db (SQLite, WAL) + logs/ + backups/   (gitignored)
docs/          # ARCHITECTURE.md, HANDBOOK.md
frontend/      # SvelteKit app (build/ is committed; served at /app)
src/sentinel/  # the bot: ingesters/, pipelines/, analytics/, api/, dashboard/,
               # edgar/, plus the spine (scorecard, funds, scheduler, llm, …)
tests/         # ~365 deterministic tests pinning the load-bearing math + gates
```

*Paper only. Opinionated by design. It bets — it never invents the evidence.*
