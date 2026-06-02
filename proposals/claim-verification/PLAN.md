# Plan — Claim-verification layer ("grounding audit")

## Context

Sentinel's one inviolable rule is **never fabricate a number, price, or fact**
(`CLAUDE.md`, `docs/HANDBOOK.md §2`). Today that rule is enforced only
*preventively*: `grounding.py` prepends a "trust the data" preamble to LLM calls.
Nothing checks the numbers the LLM actually *emits*. Pipelines feed real DB
figures **into** prompts (e.g. `pipelines/why_moved.py` rounds real
`PriceContext` values into the payload) but the model's free-text thesis and
embed prose can still restate a wrong figure or invent one, and that text is what
becomes a `TradingCall` and a Discord post.

Why this matters more than anything else right now: a single fabricated number
doesn't fail locally — it **poisons the whole loop**. A bad "NVDA +12% on 3x
volume" becomes a `TradingCall`, sizes a wallet position, gets graded by the
scorecard, and is re-ingested by the next `synthesis` read as "track record." So
verification protects both **trust** (the binding constraint for a tool of one)
and the **integrity of the self-correction loop**. It's also the rare addition
that's measurable in days (caught-fabrication rate) rather than the weeks the
edge layers need.

**Outcome:** a deterministic verifier that, at the two existing chokepoints
(`scorecard.record_call` and `discord_client.post_embed`), extracts the hard
numeric claims from generated text and checks each against the DB ground truth,
then flags/annotates mismatches and records a telemetry trail — without ever
blocking a post or crashing a pipeline.

## Design decisions (chosen; override if desired)

1. **Enforcement = annotate + flag, never block.** On a contradiction: the post
   still goes out with a `⚠ Unverified figures` field; the call is still recorded
   but with `grounded=False` and (by flag) conviction floored to 1; a one-line
   alert goes to `#meta`. This protects the invariant and the learning loop
   without letting a fuzzy extractor silence real signal. All behind config flags.
2. **Fail-open everywhere.** If extraction is unavailable or anything throws, the
   item proceeds *unverified* (`grounded=None`). The verifier must never crash a
   pipeline or block a post — consistent with the repo's top-level failure policy.
3. **Only check ground-truthable, ticker-bound numbers.** Closed metric set:
   last price, 1d %, 5d %, volume-vs-20d multiple, and up/down direction sign.
   Everything else (forward-looking, qualitative, macro reasoning) is
   `unverifiable` and **never penalized** — this is what keeps false positives
   from eroding trust in the verifier itself.

## Core mechanism

Three stages — only stage 1 uses the LLM; the check is pure arithmetic.

1. **Extract** (`light` tier, JSON, reasoning off): a new `extract_claims` prompt
   turns generated text + the tickers named into a JSON array of structured
   claims `{ticker, metric, value, direction_word, raw}`, metric constrained to a
   closed enum. Fail-open → `[]`.
2. **Check** (pure Python, deterministic): per claim, fetch ground truth from
   `PriceContext` (and `PriceBar`/`EarningsDate` as needed) and compare within
   configured tolerances → `supported | contradicted | unverifiable`.
3. **Decide**: `grounded = (no contradictions)`. Build a human note of the worst
   contradictions; log a `ClaimCheck` row; publish a `claim_check` SSE event.

Tolerances (rounding-friendly, all in `config.py`): price within
`VERIFY_PRICE_TOL_PCT` (2%); % move within `VERIFY_PCT_TOL_PP` (1.5pp) **or** 25%
relative, whichever is looser; volume multiple within `VERIFY_VOL_TOL` (0.5x);
**direction sign mismatch is always a contradiction** (cheapest, highest-value
check). Stale `PriceContext` (older than a small window) → `unverifiable`.

## New/changed files

- **`src/sentinel/verify.py`** (new) — the whole feature: `Claim`,
  `ClaimVerdict`, `VerifyResult` dataclasses; `check_claims(...)` (pure);
  `extract_claims(text, tickers)` (light LLM, fail-open); `verify_text(text,
  tickers, surface, source, session=None)` orchestrator that checks, logs a
  `ClaimCheck`, and publishes an event. Sync-callable (uses `llm.complete`).
- **`prompts.py`** — register a new `extract_claims` constant (closed metric enum,
  "only hard checkable ticker-bound numbers; ignore forward/qualitative"). Uses
  the existing registry + `get_prompt`; `$` escaped as `$$`. `test_prompts.py`
  will auto-cover registration/substitution.
- **`models.py`** — add `ClaimCheck` table (id, ts indexed, surface, source,
  ticker?, n_claims, n_contradicted, grounded, note, sample). New table
  auto-creates on boot. Add nullable `grounded` (bool) + `verify_note` (str) to
  `TradingCall`.
- **`db.py`** — add the two `TradingCall` columns to `_migrate_add_columns`
  (additive `ADD COLUMN`, idempotent).
- **`config.py`** + **`.env.example`** — `VERIFY_ENABLED` (True),
  `VERIFY_MIN_IMPORTANCE` (3), `VERIFY_PRICE_TOL_PCT` (2.0), `VERIFY_PCT_TOL_PP`
  (1.5), `VERIFY_VOL_TOL` (0.5), `VERIFY_FLOOR_CONVICTION_ON_CONTRADICTION` (True).
- **`scorecard.py`** — in `record_call`, after validation/before persist: if
  enabled, `verify_text(thesis, [ticker], surface="call", source=source)`; set
  `tc.grounded` / `tc.verify_note`; floor conviction on contradiction (per flag);
  best-effort `#meta` alert; always record. Fully fail-open.
- **`discord_client.py`** — `post_embed(..., verify: bool | None = None)`; default
  verify when `VERIFY_ENABLED` and `importance >= VERIFY_MIN_IMPORTANCE`. Flatten
  embed text, find candidate tickers, run `verify_text` via `asyncio.to_thread`
  (don't block the loop); on contradiction append a `⚠ Unverified figures` field.
  Fail-open.
- **`health.py`** — add a grounding section to `health_report()` (structured) +
  `health_text()` (Discord): 24h/7d checked count, contradiction rate, worst
  sample; `⚠` verdict above a threshold. Surfaces on the System page for free.
- **`tests/test_verify.py`** (new) — deterministic, no network/LLM.

## Phasing (each phase ends green)

1. **Deterministic core + config + tests.** `verify.py` dataclasses + `check_claims`
   + tolerances + the `VERIFY_*` settings; `tests/test_verify.py` injecting
   `Claim`s against seeded `PriceContext`. No LLM, no hooks.
2. **Extraction.** `extract_claims` prompt + `verify_text` orchestrator (light
   LLM, fail-open, `parse_json_response`). Manual smoke via a one-off script.
3. **Persistence + telemetry.** `ClaimCheck` table, `TradingCall.grounded/
   verify_note` + migration, `events.publish("claim_check", …)`, health detector
   + tests (detector + migration idempotency).
4. **Hooks + docs.** Wire `record_call` (sync) and `post_embed` (async via
   `to_thread`) behind the flags + importance gate. Update `docs/HANDBOOK.md`,
   `docs/ARCHITECTURE.md` (chokepoints + spine), and `CLAUDE.md` (chokepoint table
   + gotcha). Full suite + `ruff`.

## Tests (deterministic)

`tests/test_verify.py` seeds `PriceContext` rows in the in-memory DB
(`conftest.py` fixture) and injects `Claim`s directly (no LLM):
supported/contradicted price at tolerance edges; %-move absolute + relative
tolerance edges; volume-multiple edge; **direction sign mismatch → contradicted**;
unknown/forward metric → `unverifiable`; stale context → `unverifiable`; ticker
absent → `unverifiable`; empty claims → `grounded=True, ok=True`. Plus: `verify_text`
fails open when `extract_claims` is monkeypatched to raise/return `[]`; and a
`record_call` integration test (monkeypatch `verify.verify_text` to a canned
contradicted result) asserting `grounded=False` persisted + conviction floored —
reuse `tests/test_autofade.py` setup.

## Verification (end-to-end)

- `uv run pytest -q tests/test_verify.py` then the full `uv run pytest -q` (~365
  green) and `uv run ruff check src tests`.
- `uv run python -m sentinel.main --preflight` still green (new table initializes).
- On a weekday with data: `uv run python -m sentinel.main --run-once why_moved`
  (and `synthesis`) → confirm calls get `grounded` set, any contradiction adds a
  `⚠` field + a `#meta` line, and the System/health page shows the grounding
  section. Deliberately seed a wrong number (or loosen a tolerance to 0) to force
  a contradiction and watch the flag/annotate path fire.

## Guardrails / non-goals

- Fail-open; never block a post or crash a pipeline on verifier error.
- Bound cost: one light JSON call only for posts `>= VERIFY_MIN_IMPORTANCE`, only
  over named tickers.
- No new scheduled job — verification is inline at the chokepoints.
- Don't touch the `materiality` prompt (tuner-owned) or any wallet math.
- Keep it one module + the two hooks (the "extend a chokepoint, don't fork"
  convention). The verifier is read-only against ground-truth tables.
