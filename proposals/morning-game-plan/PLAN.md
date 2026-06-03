# Plan — Morning Game Plan (web-only)

## Context

Sentinel's purpose is to help one trader act better without refreshing anything,
but it currently surfaces across a 19-page dashboard and (separately) 17 Discord
channels. Each surface is individually gated for noise, but the *aggregate* is a
firehose — and synthesizing across all of it is exactly the work the system was
meant to remove. The weakest link in delivering value is now the **last mile**:
turning everything the system produces into one decision the user can act on.

There's already a narrative `briefing` pipeline (heavy LLM, pre-open) and a
`Briefing` row rendered on the Overview page, plus a `DailyPlan` table for the
user's own morning intent. What's missing is a **single, ranked, book-centric
action list** that fuses every arm, dedupes against what's already been said, and
ends each item in a concrete suggested action — the "if you read one thing, read
this" artifact.

**Scope decision: web-only for now.** No Discord post. The pipeline produces a
structured artifact persisted to the DB and surfaced on the dashboard
(Overview panel + API). A Discord post is a trivial future add (one `post_embed`
call) once the web version is dialed in — explicitly out of scope here.

**Outcome:** every morning (and on demand) the dashboard shows one ranked Game
Plan, centered on the user's open paper book, that fuses risk, maturing calls,
catalysts, and the best fresh ideas into prioritized items with suggested actions.

## What it fuses (all from existing accessors — no new data sources)

Deterministic, structured inputs gathered from what's already computed:

- **Book risk** — `analytics/risk_monitor.py` (positions near stop/target, $ at
  risk), `analytics/earnings_exposure.py` (holdings reporting soon),
  `analytics/holdings_news.py` (fresh filings/news touching held names).
- **Maturing & resolved** — `scorecard` (calls maturing today; how recent ones
  resolved) + `call_review`.
- **Catalysts today** — `pipelines/catalysts.py` / `config/macro_calendar.yaml`
  + `EarningsDate`, filtered to the user's names first.
- **Fresh ideas** — recent `TradingCall`s (now `grounded`-checked by `verify.py`),
  ranked by `conviction × measured source-edge` (`funds` edge multipliers /
  `analytics/perf_by_source.py`), deduped vs the open book.
- **Continuity** — the user's `DailyPlan` row + recent `NarrativeEvent`s
  (`narrative.recent_*`) so the plan references prior pings once instead of
  restating them.

## Core design

The LLM **ranks and phrases; it does not fetch numbers.** A deterministic
assembler builds a structured input bundle (every figure is real, pulled from the
accessors above). The heavy LLM is handed that bundle and asked only to: pick the
top items, dedupe against `NarrativeEvent`s from the last ~18h, write a one-line
suggested action per item, and a 2–3 sentence overall "the read". Output is
**structured JSON** (ranked sections of items), not prose — so the web renders it
as interactive cards and the numbers are grounded by construction. Any free text
it does write still flows through the existing `verify.py` at the call/post
chokepoints if it ever emits a call.

Structured output shape (illustrative):

```
{ "the_read": "...2-3 sentences...",
  "sections": [
    {"kind": "book_risk",  "items": [{"ticker","headline","trigger","action","priority"}]},
    {"kind": "maturing",   "items": [...]},
    {"kind": "catalysts",  "items": [...]},
    {"kind": "fresh_ideas","items": [...]}
  ] }
```

Fail-open: if the LLM is unavailable or returns junk, persist the **deterministic
bundle unranked** (still useful) rather than nothing — consistent with the repo's
failure policy.

## New/changed files

- **`src/sentinel/pipelines/game_plan.py`** (new) — `build_inputs(session) -> dict`
  (pure, deterministic; reuses the analytics/scorecard/catalysts accessors) and
  `run_game_plan()` (heavy LLM ranks/dedupes/writes; persists; publishes event).
- **`prompts.py`** — register a `game_plan` prompt (inputs = the structured
  bundle + recent narrative; output = the ranked JSON above; "rank and phrase,
  do not invent numbers; dedupe against recent events"). `$` escaped `$$`.
- **`models.py`** — add a `GamePlan` table (`plan_date` PK = ET date,
  `generated_at`, `sections_json` str, `the_read` str, `model`). New table
  auto-creates; no migration needed.
- **`scheduler.py`** + **`main.py`** — register a `game_plan` job (~08:45 ET
  weekdays) and add `game_plan` to `_RUN_ONCE_REGISTRY` (scheduler↔run-once parity).
- **`api/plan.py`** — add `GET /api/plan/gameplan/today` and
  `GET /api/plan/gameplan/{ymd}` returning the structured plan.
- **`events.py`** usage — `events.publish("game_plan", {...})` on generation so the
  Overview updates live via the existing SSE stream.
- **Frontend** — a "Game Plan" panel on the Overview page (or a small dedicated
  `/plan` route): fetch via the existing api client + TanStack Query, render the
  ranked sections as cards with priority accents. Rebuild `frontend/build/`.
- **`tests/test_game_plan.py`** (new) — deterministic.
- **Docs** — note the new pipeline in `docs/HANDBOOK.md` §6 + `docs/ARCHITECTURE.md`
  §5/§9 and the run-once registry.

## Phasing (each phase ends green)

1. **Deterministic assembler + tests.** `build_inputs()` only — gather the
   structured bundle from existing accessors; `tests/test_game_plan.py` seeds an
   open `PaperTrade`/`FundTrade`, a couple of `TradingCall`s, an `EarningsDate`,
   and asserts the bundle's contents/ranking. No LLM, no schedule. This is the
   real engine and is fully testable.
2. **LLM ranking + persistence.** `game_plan` prompt + `run_game_plan()` (heavy,
   `parse_json_response`, fail-open to the unranked bundle); `GamePlan` table;
   `events.publish`. Test the fail-open path (monkeypatch the LLM).
3. **Schedule + API.** Scheduler job (08:45 ET weekday) + `--run-once game_plan`
   parity; the two `api/plan.py` endpoints. Confirm `--preflight` still green.
4. **Frontend panel.** Overview "Game Plan" card fed by the new endpoint + SSE;
   rebuild `frontend/build/`.

## Tests (deterministic)

`tests/test_game_plan.py` (use the `conftest.py` in-memory DB): seed an open
position near its stop, a held name with an `EarningsDate` in 2 days, two
`TradingCall`s of differing conviction/source, and a `NarrativeEvent` for one of
them; assert `build_inputs()` surfaces the at-risk position, the earnings
exposure, ranks the higher conviction×edge idea first, and marks the
already-narrated item as deduped. Plus: `run_game_plan()` falls back to the
unranked bundle when the LLM is monkeypatched to fail (still persists a row).

## Verification (end-to-end)

- `uv run pytest -q tests/test_game_plan.py`, then full `uv run pytest -q` and
  `uv run ruff check src tests`; `uv run python -m sentinel.main --preflight` green.
- On a weekday with an open paper position + recent calls:
  `uv run python -m sentinel.main --run-once game_plan` → confirm a `GamePlan` row,
  `GET /api/plan/gameplan/today` returns the structured plan, and the Overview
  panel renders it (and updates live via SSE).

## Guardrails / non-goals

- **LLM ranks/phrases; never fetches numbers** — figures come from the
  deterministic bundle, so they're grounded by construction.
- **Fail-open** to the unranked bundle; never produce nothing, never crash a boot.
- **Reuse existing accessors** — don't recompute risk/edge/catalysts; this is a
  consumer/synthesizer, not a new data arm ("more arms fights the spine").
- **Dedupe** against recent `NarrativeEvent`s so it doesn't restate book_risk /
  why_moved pings.
- **No Discord** (deferred), **no new ingester**, no wallet-math changes, don't
  touch the `materiality` prompt.
- Consider later merging the prose `briefing` into this structured plan to avoid
  two overlapping morning artifacts — flagged, not done here.
