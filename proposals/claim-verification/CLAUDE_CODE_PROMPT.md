# Claude Code prompt — implement the claim-verification layer

> Paste everything below the line into Claude Code, run from the repo root
> (`/home/algoatson/Work/tradingbot`). The repo's `CLAUDE.md`, `docs/ARCHITECTURE.md`,
> and `docs/HANDBOOK.md` are accurate — read them first. A fuller design rationale
> lives in `proposals/claim-verification/PLAN.md`.

---

You are implementing a new feature in **Sentinel** (a single-user, paper-only
trading-intelligence bot). Read `CLAUDE.md`, `docs/ARCHITECTURE.md`, and
`docs/HANDBOOK.md` before writing any code, and follow every convention there —
especially: route through chokepoints (don't fork them), additive-only DB
migrations, all config via `config.py`'s `Settings` singleton (no `os.getenv`),
prompts registered in `prompts.py` with `$` escaped as `$$`, top-level
catch-and-continue failure policy, and deterministic tests (no network, no real
LLM).

## Goal

Enforce the project's one inviolable rule — **never fabricate a number, price, or
fact** — with a verification layer. Today `grounding.py` only *prevents* (a
preamble); nothing checks the numbers the LLM *emits*. Build a deterministic
verifier that, at the two existing chokepoints, extracts the hard numeric claims
from generated text and checks them against the DB ground truth, then flags and
annotates mismatches and records telemetry.

## Hard requirements (do not violate)

- **Annotate + flag, never block.** On a contradiction the Discord post still
  goes out (with a warning field); the call is still recorded (with
  `grounded=False`, conviction floored per a flag); a one-line alert goes to
  `#meta`. Never drop a call or hold a post because of the verifier.
- **Fail-open.** If extraction is unavailable or anything throws, the item
  proceeds *unverified* (`grounded=None`). The verifier must never crash a
  pipeline or block a post.
- **Only check ground-truthable, ticker-bound numbers.** Closed metric set:
  `price` (last price), `change_1d_pct`, `change_5d_pct`, `vol_mult`
  (volume-vs-20d multiple), `direction` (up/down sign). Anything else —
  forward-looking, qualitative, macro reasoning — is `unverifiable` and **never
  penalized**. False positives here are worse than misses; they erode trust in
  the verifier.
- **All behind config flags**, defaulting to enabled but conservative.
- Everything read-only against the ground-truth tables; reuse existing accessors.

## Ground truth to check against

`PriceContext` (one row per ticker: `last_price`, `change_1d_pct`,
`change_5d_pct`, `volume_vs_20d_avg`, `last_updated`), and if needed `PriceBar` /
`EarningsDate`. Find and reuse any existing per-ticker `PriceContext` accessor
(look in `portfolio.py`, `analytics/`, `market_tools.py`) rather than writing raw
queries. Note `change_*_pct` are stored as fractions (e.g. `0.116`) and pipelines
display them ×100 — handle the unit conversion explicitly.

## Build in 4 phases; each phase must end with `uv run pytest -q` green and `uv run ruff check src tests` clean.

### Phase 1 — deterministic core + config + tests (no LLM, no hooks)

- New module `src/sentinel/verify.py` with dataclasses:
  - `Claim(ticker, metric, value: float|None, direction_word: str|None, raw: str)`
  - `ClaimVerdict(claim, status: "supported"|"contradicted"|"unverifiable", actual: float|None, detail: str)`
  - `VerifyResult(verdicts, n_checked, n_supported, n_contradicted, n_unverifiable, grounded: bool, note: str, ok: bool)`
- `check_claims(claims, *, session=None) -> VerifyResult`: pure/deterministic.
  Per claim, load `PriceContext` for `claim.ticker`; compare within tolerances:
  - `price`: within `VERIFY_PRICE_TOL_PCT` % of `last_price`.
  - `change_1d_pct` / `change_5d_pct`: within `VERIFY_PCT_TOL_PP` percentage
    points **or** 25% relative, whichever is looser.
  - `vol_mult`: within `VERIFY_VOL_TOL` of `volume_vs_20d_avg`.
  - `direction`: sign of `change_1d_pct` must match the up/down word — **mismatch
    is always `contradicted`**.
  - No row for the ticker, or `last_updated` older than a small staleness window
    → `unverifiable`. Unknown metric → `unverifiable`.
  - `grounded = (n_contradicted == 0)`; `note` summarizes the worst contradictions.
- Add settings to `config.py` (+ `.env.example`): `VERIFY_ENABLED=True`,
  `VERIFY_MIN_IMPORTANCE=3`, `VERIFY_PRICE_TOL_PCT=2.0`, `VERIFY_PCT_TOL_PP=1.5`,
  `VERIFY_VOL_TOL=0.5`, `VERIFY_FLOOR_CONVICTION_ON_CONTRADICTION=True`.
- `tests/test_verify.py` (use the `conftest.py` in-memory DB fixture; inject
  `Claim`s directly): supported/contradicted at each tolerance edge; %-move
  absolute + relative edges; vol-multiple edge; direction sign match/mismatch;
  unknown metric → unverifiable; stale context → unverifiable; ticker absent →
  unverifiable; empty claims → `grounded=True, ok=True`.

### Phase 2 — extraction (light LLM, fail-open)

- Register an `extract_claims` prompt in `prompts.py`: inputs `{text}` and
  `{tickers}`; output a strict JSON array of `{ticker, metric, value,
  direction_word, raw}` with `metric` constrained to the closed enum; instruct it
  to extract **only hard, checkable, ticker-bound numeric claims** and to ignore
  forward-looking / qualitative / macro statements. Escape literal `$` as `$$`.
- `extract_claims(text, tickers) -> list[Claim]` in `verify.py`: `light` tier,
  `json_mode=True` (reasoning off), parse with `llm.parse_json_response`; drop
  claims whose ticker isn't in `tickers` or whose metric isn't in the enum;
  fail-open → `[]` on any error or `[LLM_ERROR]`.
- `verify_text(text, tickers, *, surface, source, session=None) -> VerifyResult`:
  orchestrate extract → `check_claims`; set `ok=False` when extraction was
  unavailable. Sync-callable.
- Manual smoke: a one-off snippet calling `verify_text` on a sample string.

### Phase 3 — persistence + telemetry

- `models.py`: add a `ClaimCheck` table (`id`, `ts` indexed, `surface`
  ("call"|"post"), `source`, `ticker` nullable, `n_claims`, `n_contradicted`,
  `grounded` bool, `note` ≤500, `sample` ≤500). Add nullable `grounded` (bool) and
  `verify_note` (str ≤400) to `TradingCall`.
- `db.py`: add the two `TradingCall` columns to `_migrate_add_columns` (idempotent
  `ADD COLUMN`). The new table auto-creates.
- In `verify_text`, after checking: insert a `ClaimCheck` row and
  `events.publish("claim_check", {...})` (best-effort, never fatal).
- `health.py`: add a grounding section to both `health_report()` (structured) and
  `health_text()` (Discord) — last 24h/7d checked count, contradiction rate, worst
  sample; `⚠` verdict above a threshold (e.g. >10% contradiction rate with a
  minimum sample). Add a test for the detector and for migration idempotency.

### Phase 4 — hooks + docs

- `scorecard.record_call`: after validation, before persisting the `TradingCall`,
  if `settings.VERIFY_ENABLED` call `verify_text(thesis, [ticker],
  surface="call", source=source)`; set `tc.grounded` / `tc.verify_note`; if not
  grounded and `VERIFY_FLOOR_CONVICTION_ON_CONTRADICTION`, floor conviction to 1
  and append to the note; post a one-line `#meta` alert (best-effort). Always
  record the call. Wrap in try/except → unverified on error.
- `discord_client.post_embed`: add `verify: bool | None = None`; default to verify
  when `settings.VERIFY_ENABLED and importance >= settings.VERIFY_MIN_IMPORTANCE`.
  Flatten the embed to text (reuse/extend the `interactions.extract_post_text`
  style), collect candidate tickers (cashtags + title), and run `verify_text` via
  `asyncio.to_thread` (do not block the loop). On contradiction append a
  `⚠ Unverified figures` field with the note. Fail-open.
- Add a `record_call` integration test (monkeypatch `verify.verify_text` to a
  canned contradicted `VerifyResult`) asserting `grounded=False` persisted and
  conviction floored — model it on `tests/test_autofade.py`.
- Update docs to reflect the new behavior: `CLAUDE.md` (add verification to the
  chokepoint notes for `record_call`/`post_embed` + a gotcha), `docs/ARCHITECTURE.md`
  (§6 accountability spine + §7 surface), `docs/HANDBOOK.md` (the spine + ops
  sections, and the "never fabricate" line now backed by a real check).

## Acceptance criteria

- Full `uv run pytest -q` green (existing ~365 + new), `uv run ruff check src tests`
  clean, `uv run python -m sentinel.main --preflight` green (new table initializes).
- With `VERIFY_ENABLED=False`, behavior is byte-identical to before (no calls, no
  fields, no rows).
- Deliberately forcing a contradiction (seed a wrong figure or set a tolerance to
  0) produces: a `⚠ Unverified figures` field on the post, `grounded=False` +
  floored conviction on the `TradingCall`, a `ClaimCheck` row, a `#meta` line, and
  a non-zero contradiction rate in the health/System grounding section — and
  nothing is blocked or dropped.
- A forced extractor failure (raise inside `extract_claims`) leaves posts and
  calls flowing, unverified (`grounded=None`).

## Notes

- Keep it to the one module + the two hooks + the table/columns/prompt/settings/
  health detector. No new scheduled job; verification is inline at the chokepoints.
- Don't touch wallet math or the `materiality` prompt.
- Commit per phase with a clear message; do not commit to `main` directly (branch
  first, per the repo workflow).
