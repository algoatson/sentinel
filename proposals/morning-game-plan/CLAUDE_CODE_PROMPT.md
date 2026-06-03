# Claude Code prompt — implement the Morning Game Plan (web-only)

> Paste everything below the line into Claude Code, run from the repo root
> (`/home/algoatson/Work/tradingbot`). The repo's `CLAUDE.md`,
> `docs/ARCHITECTURE.md`, and `docs/HANDBOOK.md` are accurate — read them first.
> Fuller rationale: `proposals/morning-game-plan/PLAN.md`.

---

You are implementing a new feature in **Sentinel** (a single-user, paper-only
trading-intelligence bot). Read `CLAUDE.md`, `docs/ARCHITECTURE.md`, and
`docs/HANDBOOK.md` before writing any code and follow every convention there —
especially: route through chokepoints (don't fork them), additive-only DB
migrations, all config via `config.py`'s `Settings` singleton, prompts registered
in `prompts.py` (`$` escaped as `$$`), scheduler↔`--run-once` parity, top-level
catch-and-continue failure policy, deterministic tests (no network/LLM), and
reuse the **structured** `analytics/` accessors rather than recomputing.

## Goal

Build a **Morning Game Plan**: one ranked, book-centric action list that fuses
every arm of the system into a single decision surface on the **web dashboard**.
It attacks the last-mile problem — the system over-produces across many surfaces
and the user has to synthesize it themselves. **Web-only: do NOT post to Discord.**

## Hard requirements (do not violate)

- **The LLM ranks and phrases; it never fetches numbers.** A deterministic
  assembler builds a structured input bundle where every figure is real (pulled
  from existing accessors). The LLM is handed that bundle and only: selects/ranks
  the top items, dedupes against recent `NarrativeEvent`s, writes a one-line
  suggested action per item, and a 2–3 sentence overall read. Output is
  **structured JSON**, not prose — so numbers are grounded by construction.
- **Fail-open.** If the LLM is unavailable or returns junk
  (`llm.parse_json_response` → None / `[LLM_ERROR]`), persist the **deterministic
  bundle unranked** rather than nothing. Never crash a boot or a cycle.
- **Reuse existing accessors** — `analytics/risk_monitor.py`,
  `analytics/earnings_exposure.py`, `analytics/holdings_news.py`,
  `analytics/perf_by_source.py`, `scorecard` (maturing/resolved + track record),
  `pipelines/catalysts.py`, `portfolio.py` (held tickers / open positions),
  `funds` (edge multipliers), `narrative.recent_*`, and the `DailyPlan` table.
  Don't write parallel queries for things already computed.
- **No Discord, no new ingester, no wallet-math changes**, don't touch the
  `materiality` prompt.

## Build in 4 phases; each ends with `uv run pytest -q` green and `uv run ruff check src tests` clean.

### Phase 1 — deterministic assembler + tests (no LLM, no schedule)

- New `src/sentinel/pipelines/game_plan.py` with `build_inputs(session=None) -> dict`:
  a pure, deterministic bundle with these sections, each item carrying the real
  numbers + a machine "trigger":
  - `book_risk`: open positions near stop/target, $ at risk, adverse drawdown
    (from `risk_monitor`), held names reporting soon (`earnings_exposure`), fresh
    filings/news on held names (`holdings_news`).
  - `maturing`: `TradingCall`s maturing today + how recent ones resolved (scorecard).
  - `catalysts`: today's/this-week's catalysts filtered to held + watchlist names.
  - `fresh_ideas`: recent `grounded` `TradingCall`s (last ~24h) ranked by
    `conviction × source-edge multiplier`, deduped vs the open book.
  - `prior`: the user's `DailyPlan` for today + recent `NarrativeEvent` keys (for
    the LLM to dedupe against).
- `tests/test_game_plan.py` (use the `conftest.py` in-memory DB): seed an open
  `PaperTrade` near its stop, a held name with an `EarningsDate` ~2 days out, two
  `TradingCall`s of differing conviction/source, and a `NarrativeEvent` for one;
  assert the bundle surfaces the at-risk position + earnings exposure, ranks the
  higher conviction×edge idea first, and flags the already-narrated item.

### Phase 2 — LLM ranking + persistence

- Register a `game_plan` prompt in `prompts.py`: input = the structured bundle +
  recent narrative keys; output = strict JSON `{the_read, sections:[{kind,
  items:[{ticker, headline, trigger, action, priority}]}]}`. Instruct: "rank and
  phrase only; do not invent or alter numbers; dedupe against the recent events;
  drop low-signal items." `$` escaped `$$`.
- `run_game_plan()` in `game_plan.py`: build bundle → heavy LLM (`json_mode`,
  reasoning off) → `parse_json_response`; **fail-open** to the unranked bundle.
- `models.py`: add a `GamePlan` table (`plan_date` PK = ET date string,
  `generated_at`, `sections_json` str, `the_read` str ≤2000, `model` ≤120). New
  table auto-creates (no migration). Upsert one row per ET date.
- `events.publish("game_plan", {plan_date, n_items})` after persisting (best-effort).
- Test the fail-open path (monkeypatch the LLM to raise) still writes a row.

### Phase 3 — schedule + API

- `scheduler.py`: register a `game_plan` cron job at ~08:45 ET on weekdays
  (after the inputs it depends on are fresh). `main.py`: add `game_plan` to
  `_RUN_ONCE_REGISTRY` so `--run-once game_plan` works (parity is required).
- `api/plan.py`: add `GET /api/plan/gameplan/today` and
  `GET /api/plan/gameplan/{ymd}` returning the structured plan (parse
  `sections_json`). Follow the existing router's patterns / error handling.
- Confirm `uv run python -m sentinel.main --preflight` still green (new table
  initializes) and `--run-once game_plan` produces a row + endpoint response.

### Phase 4 — frontend panel

- Add a "Game Plan" panel to the Overview page (`frontend/src/routes/overview/`)
  — or a small dedicated `/plan` route — that fetches `/api/plan/gameplan/today`
  via the existing api client (`frontend/src/lib/api.ts`) + TanStack Query, and
  renders the ranked sections as cards with priority accents and per-item
  trigger/action. Subscribe to the `game_plan` SSE event for live refresh
  (`frontend/src/lib/events.svelte.ts`). Match the existing component/theme
  patterns. Rebuild: `cd frontend && pnpm install && pnpm build` (the committed
  `frontend/build/` must be updated). If `pnpm` is unavailable in your
  environment, implement the Svelte + api/types changes and clearly note that
  `pnpm build` still needs to run.
- Update `docs/HANDBOOK.md` (§6 pipeline table + §12 dashboard pages) and
  `docs/ARCHITECTURE.md` (§5 pipelines + §9 scheduler) to include `game_plan`.

## Acceptance criteria

- Full `uv run pytest -q` green (existing + new), `ruff` clean, `--preflight` green.
- `uv run python -m sentinel.main --run-once game_plan` on a weekday (with an open
  paper position + recent calls) writes a `GamePlan` row; `GET
  /api/plan/gameplan/today` returns the structured plan; the Overview panel renders
  it and updates on the `game_plan` SSE event.
- With the LLM forced to fail, a row is still written from the deterministic
  bundle (unranked) and the endpoint/panel still render.
- No Discord code paths touched.

## Notes

- Keep it to one new pipeline + one table + the prompt + the two endpoints + the
  panel. It's a consumer/synthesizer of existing arms, not a new data source.
- A Discord post is intentionally deferred; the artifact is structured so adding a
  later `post_embed` is trivial — leave a `# TODO(discord)` note, nothing more.
- Consider (but don't do here) later merging the prose `briefing` into this
  structured plan to avoid two overlapping morning artifacts.
- Commit per phase; branch first (don't commit to `main` directly).
