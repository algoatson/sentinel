# Plan — More robust news ticker resolution (structured candidates + smarter LLM)

## Context

Ticker attribution is the input classification the whole system inherits
(`NewsItem.ticker` drives trading/scoring; `tickers_csv` drives thesis-linking,
`news_impact`, convergence, search, the verifier). Today
`news_tickers.resolve_article_tickers` derives the ticker set almost entirely
from the title/summary — a heuristic `extract_tickers_ranked` pass plus a light
LLM — and the only structured signal it uses is a single `feed_ticker` hint.

**We want the LLM reasoning to stay and get *better*, not be removed.** The goal:
feed the resolver a high-recall, structured candidate set so the model reasons
from an anchored shortlist (which names is this about / what would it materially
affect) instead of guessing the whole set from prose. The model should still be
free to **add** an affected name the candidates missed ("AI capex story →
$AMAT") and to **drop** a name that's only present because of the feed/search
query.

### Live payload findings (pulled from the real sources, June 2026)

These corrected my initial assumptions — build against *these*, not the old plan:

- **`yfinance.Ticker(t).news`** (what the ingester calls today): items are just
  `{content, id}`; `content.finance` has **no** ticker list, and legacy
  `relatedTickers` is `None`. **There is nothing to capture on the current path.**
- **Yahoo v1 search API** — `https://query2.finance.yahoo.com/v1/finance/search?q={T}&newsCount=10&quotesCount=0`
  (and `yfinance.Search`) — returns `news[]` where each item has
  **`relatedTickers`**, e.g. an NVDA chips story →
  `['NVDA','SOXX','QCOM','MRVL','DELL','HPQ','ARM','INTC','AMD','AAPL','MSFT']`.
  This is the "might affect X" association set. **Caveat:** it injects the
  *query* ticker — searching `NVDA` stamps `NVDA` onto a Walmart article
  (`['WMT','NVDA','COST','TGT']`) and an Alphabet one (`['GOOG','NVDA']`). The
  real subject is typically `relatedTickers[0]`; the query ticker is appended.
- **RSS feeds** (Yahoo per-ticker `?s=`, CNBC, Google News): `entry.tags` is
  `None` for all of them. **No structured tickers in RSS at ingest.** The feed's
  own ticker (Yahoo `?s=NVDA`) is just the query ticker again.
- **Yahoo article *page*** (the URL most items point to): carries a curated,
  tight ticker set in the HTML (the `stockTickers` JSON / `ticker-tag-module`
  anchors / meta `$bnb-usd;…` hashtag — e.g. BNB/H/BTC). High precision, but only
  available if we fetch the page.

### Deployment note

The live process + real `data/radar.db` run on a **Raspberry Pi** (dashboard at
`http://10.0.0.69:8730/app/`); this repo checkout has stale/empty data. Keep the
change **network-light and CPU-cheap**: no increase in per-poll network calls,
LLM stays a single bounded light call per article within the existing budget, and
any page fetch reuses the `article_fetch` cache. Branches aren't used — commit
straight to the working tree.

## Design — anchored union, LLM still reasons

`resolve_article_tickers` keeps its role but gains a structured candidate set.
For each article gather, normalize to watchlist convention, and `∩ watchlist`:

1. **`source_cand`** — structured, high recall:
   - yfinance path: `relatedTickers` from the Yahoo **search API** for that poll
     ticker's articles (subject-first; query ticker flagged as `feed_ticker`).
   - when the article page is fetched (phase 2): curated tickers via
     `from_html` (high precision).
   - RSS path: usually empty (kept for the rare feed that tags).
2. **`heuristic`** — `extract_tickers_ranked(title+summary)` (today).
3. **LLM resolve (kept + improved)** — one light JSON call given the title,
   summary, the **union of source+heuristic candidates as strong hints**, and the
   watchlist universe. The prompt asks it to return: the set of tickers the story
   is **about or would materially affect** (it MAY add names not in the hints; it
   SHOULD drop a hinted ticker that's only there because it was the feed/search
   query and the content doesn't support it), plus the single **primary** subject.
   Output validated against the watchlist (allowlist) — drops hallucinations and
   anything untracked.

**Final set** = LLM output ∩ watchlist (the LLM has already merged the structured
hints with its own reasoning). If the LLM is unavailable/over budget →
deterministic fallback = `source_cand ∪ heuristic` with the feed/query ticker
demoted (today's `_fallback`, now seeded with the richer `source_cand`). So even
with no LLM, tagging is strictly better than today.

**Primary** = LLM primary if valid; else title-mention rank; else
`relatedTickers[0]`; never the bare feed/query ticker unless content/title backs
it.

This is *anchored* reasoning: the model gets a curated shortlist, so it (a) needs
fewer tokens, (b) false-positives less (it's choosing/confirming, not free-
guessing), and (c) still does the value-add — reasoning in genuinely affected
names and discarding query contamination.

## Other ticker-quality improvements (do these too)

- **Demote the query/feed ticker properly** using the structured order: if the
  poll ticker is `relatedTickers[k>0]` and isn't in the title, it's a related
  mention, not the subject.
- **Order as a prior:** pass `relatedTickers` order to the resolver; `[0]` is a
  strong primary prior the LLM can override only with reason.
- **Confidence / auditability:** record per-item how tags were derived
  (`NewsItem.tag_source`: e.g. `search+ai`, `html+ai`, `heuristic`, `ai`) so
  accuracy is measurable on the Pi dashboard / `health`.
- **Cap + rank** `tickers_csv` to the top-N most-supported (avoid stamping a
  story with 11 tangential names); keep all that the LLM affirms, primary-first.
- **Normalization hardening** in one place: class shares (`BRK.B`↔`BRK-B`),
  crypto (`$btc-usd`/`BTC`→`BTC-USD`), futures (`ES`→`ES=F`), drop foreign/
  non-watchlist (`2454.TW`, `ANTH.PVT`, `^GSPC`) cleanly.

## New / changed files

- **new** `src/sentinel/source_tags.py` — `related_tickers_for(query)` (Yahoo v1
  search API via `httpx`, returns `[(title,url,relatedTickers)]`), `from_html(html)`
  (Yahoo page curated tickers), `normalize(sym)`. Pure/deterministic except the
  search fetch; fully unit-testable with fixtures.
- `src/sentinel/news_tickers.py` — add `source_tickers: list[str] | None` +
  improved prompt usage; keep `ResolvedTickers`; add `tag_source`.
- `src/sentinel/prompts.py` — upgrade `tag_article_tickers` to the
  "about-or-affects + drop-the-query-ticker, candidates-as-hints" instruction.
- `src/sentinel/ingesters/news.py` — yfinance path obtains `relatedTickers` (via
  the search API for each poll ticker, replacing/augmenting the `.news` fetch
  that yields no tickers) and passes `source_tickers` + `feed_ticker`.
- `src/sentinel/models.py` (+`NewsItem.tag_source` nullable) & `db.py`
  (`_migrate_add_columns`).
- **phase 2:** `src/sentinel/article_fetch.py` (return/cache `from_html` tags) +
  a budgeted `news_retag` ingest job (scheduler + `--run-once` parity) that fetches
  tag-poor RSS items' pages and upgrades their tags.
- `config.py` + `.env.example`: `NEWS_SEARCH_TAGS_ENABLED` (bool, default True),
  optional `NEWS_RETAG_*` (phase 2). No removal of the LLM path or its budget.
- docs: `docs/ARCHITECTURE.md` §4 + `docs/HANDBOOK.md` §5 note.

## Tests (deterministic, no network/LLM)

- `tests/test_source_tags.py`: parse a **captured v1-search JSON fixture** →
  per-article `relatedTickers`; normalization edges; the **Yahoo page HTML
  snippet** → curated `{BNB-USD,H-USD,BTC-USD}`; junk/foreign symbols dropped.
- `tests/test_news_tickers.py` additions: query-ticker contamination demoted
  (WMT article searched under NVDA → primary `WMT`, NVDA only if title-backed);
  LLM (monkeypatched) **adds** an affected name beyond the hints and it survives
  watchlist validation; LLM unavailable → fallback = `source_cand ∪ heuristic`
  with feed ticker demoted; non-watchlist hints dropped; the BNB/H/BTC page case
  → primary `BNB-USD`, all three associated.
- ingest-level: a fake search payload → `NewsItem.tickers_csv` multi, correct
  primary, `tag_source` records `search`/`ai`.

## Verification (end-to-end)

- `uv run pytest -q` (existing ~365 + new) green; `ruff` clean; `--preflight`
  green (new column inits).
- `uv run python -m sentinel.main --run-once news` (weekday): yfinance items get
  multi-ticker `tickers_csv` with the query ticker correctly demoted, `tag_source`
  set, LLM budget respected.
- Because the live DB is on the Pi: after deploying, eyeball recent items on
  `http://10.0.0.69:8730/app/` (Intel/News + Symbol pages) and confirm articles
  now associate to the full, correct set (and the $H-USD-on-a-BNB-article class of
  bug is gone).

## Guardrails / non-goals

- **Keep the LLM.** It still reads each article and reasons in affected names;
  structured candidates only anchor it (better + cheaper), never replace it.
- **Watchlist is the gate;** never store untracked tickers in `tickers_csv`.
- **Fail-open & cheap:** search-fetch or extractor errors fall back to today's
  heuristic+LLM; no extra per-poll network calls; page fetch (phase 2) reuses the
  article cache and is budget-bounded — important on the Pi.
- Ingestion only; no Discord/surface changes; don't touch wallet math,
  `record_call`, or `verify.py`. No branches.
