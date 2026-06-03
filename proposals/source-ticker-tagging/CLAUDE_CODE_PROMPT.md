# Claude Code prompt — more robust news ticker resolution (structured candidates + smarter LLM)

> Paste below the line into Claude Code at the repo root
> (`/home/algoatson/Work/tradingbot`). Read `CLAUDE.md`, `docs/ARCHITECTURE.md`,
> `docs/HANDBOOK.md` first. Full rationale + the live payload findings:
> `proposals/source-ticker-tagging/PLAN.md`.

---

You are making **news ticker resolution** more robust in Sentinel. Today
`news_tickers.resolve_article_tickers` derives tickers mostly from the
title/summary (heuristic `extract_tickers_ranked` + a light LLM) with only a
single `feed_ticker` hint. Give it a **structured, high-recall candidate set** so
the LLM reasons from an anchored shortlist — **keep the LLM reasoning, make it
better, do not remove it.** Follow `CLAUDE.md` conventions (chokepoints, additive
migrations, `config.py` settings, prompts in `prompts.py` with `$$` escaping,
fail-open, deterministic tests). **Do not create git branches** — this is a
solo project; commit to the working tree.

## Ground truth (verified live — build against THIS, not assumptions)

- `yfinance.Ticker(t).news` returns items shaped `{content, id}` with **no
  tickers** (`content.finance` lacks them; legacy `relatedTickers` is `None`).
- The **Yahoo v1 search API** carries the structured tickers:
  `GET https://query2.finance.yahoo.com/v1/finance/search?q={T}&newsCount=10&quotesCount=0`
  → `news[]` items with fields `title, link, publisher, providerPublishTime,
  uuid, relatedTickers`. `relatedTickers` is the "might affect" set, **subject
  first**, but it **injects the query ticker** (search `NVDA` → a Walmart story
  comes back `['WMT','NVDA','COST','TGT']`). `yfinance.Search(q).news` exposes the
  same `relatedTickers`; prefer the direct API via the project's `httpx` for
  stability.
- RSS feeds (Yahoo `?s=`, CNBC, Google News) have **no** `entry.tags` — no
  structured tickers at ingest.
- The Yahoo article **page** HTML has a curated tight set (`stockTickers` JSON /
  `ticker-tag-module` anchors / `$bnb-usd;…` meta hashtag) — high precision, only
  if the page is fetched.

The live bot + real DB run on a **Raspberry Pi** (`http://10.0.0.69:8730/app/`);
this checkout has stale data. Keep it **network-light and CPU-cheap**: no extra
per-poll network calls, the LLM stays one bounded light call per article within
the existing `_AI_TAG_BUDGET`, page fetches reuse the `article_fetch` cache.

## Hard requirements

- **Keep + improve the LLM resolver.** Structured candidates only *anchor* it —
  the model still reads title+summary and returns the tickers the story is
  **about or materially affects**, MAY add an affected name not in the candidates,
  and SHOULD drop a candidate that's only present because it was the feed/search
  query. Never replace the LLM with pure structured tags.
- **Union, watchlist-gated.** Final set = LLM output ∩ watchlist. Never store an
  untracked ticker in `tickers_csv`.
- **Fail-open.** Search-fetch / parse / LLM errors → fall back to today's path
  (now seeded with `source_cand ∪ heuristic`, feed ticker demoted). Never crash a
  poll or drop an item.
- **Demote query contamination.** The poll/search ticker must not become primary
  unless the title/content backs it; use `relatedTickers[0]` + title rank as the
  primary prior.
- **Normalize in one place:** `BRK.B`↔`BRK-B`, `$btc-usd`/`BTC`→`BTC-USD`,
  `ES`→`ES=F`, strip `$`/upper-case, drop foreign/junk (`2454.TW`, `ANTH.PVT`,
  `^GSPC`). Reuse the crypto/futures alias logic in `utils.py`.

## Phase 1 — search-API related tickers + smarter resolver

1. New `src/sentinel/source_tags.py` (deterministic except the one fetch):
   - `related_tickers_for(query: str) -> list[dict]` — hit the v1 search API with
     `httpx`, return `[{title, url, uuid, related: [normalized tickers]}]`
     (preserve order; query ticker stays in `related` but the caller passes the
     query as `feed_ticker` so it can be demoted). Fail-open → `[]`.
   - `normalize(sym) -> str | None`.
   - (`from_html(html) -> list[str]` may land here now or in phase 2.)
2. Rework `news_tickers.resolve_article_tickers` to accept
   `source_tickers: list[str] | None` (already normalized, subject-first) and
   `feed_ticker`. Build `source_cand = [t for t in source_tickers if t in watch]`;
   `heuristic = extract_tickers_ranked(...)`; pass the **union as hints** to the
   LLM. Final set = `_ai_resolve(...) ∩ watch`; fallback (no AI) =
   `source_cand ∪ heuristic` with `feed_ticker` demoted from primary if unbacked.
   Primary = LLM primary | title rank | `source_cand[0]`. Keep `ResolvedTickers`;
   add a `tag_source` string (`search+ai`/`html+ai`/`heuristic`/`ai`).
3. Upgrade the `tag_article_tickers` prompt in `prompts.py`: inputs = title,
   summary, `candidates` (the union hints), and a short watchlist context; output
   strict JSON `{"primary": "SYM"|null, "tickers": ["..."]}`; instruction =
   "return tickers the article is about OR would materially affect; you may add
   affected names beyond the candidates; DROP a candidate that's only there
   because it was the search/feed query and the text doesn't support it." Reasoning
   off; `$$`-escape literals.
4. `ingesters/news.py` yfinance path: obtain `relatedTickers` via
   `source_tags.related_tickers_for(poll_ticker)` (this replaces the tickerless
   `.news` call as the article source, or augments it — keep one network call per
   poll ticker), and call `resolve_article_tickers(..., source_tickers=related,
   feed_ticker=poll_ticker)`. RSS path: `source_tickers=None` (unchanged behavior,
   still LLM+heuristic).
5. `models.py`: add nullable `NewsItem.tag_source`; `db.py` `_migrate_add_columns`.
   Set it where resolution is decided.
6. `config.py` + `.env.example`: `NEWS_SEARCH_TAGS_ENABLED` (bool, default True).
7. Tests: `tests/test_source_tags.py` (a **captured v1-search JSON fixture** →
   per-article `related`; normalization edges; junk/foreign dropped) and additions
   to `tests/test_news_tickers.py` (WMT-under-NVDA contamination → primary `WMT`;
   monkeypatched LLM **adds** an affected name that passes watchlist validation;
   no-AI fallback = `source_cand ∪ heuristic` with feed demoted; non-watchlist
   hints dropped). Capture the fixture by running the documented search URL once
   and saving a trimmed `news[]` array under `tests/fixtures/`.
   End: `uv run pytest -q` green, `ruff` clean, `--preflight` green.

## Phase 2 — page HTML tags for RSS items (optional, budgeted)

8. Extend `article_fetch` to also return/cache `source_tags.from_html(html)`
   (parse Yahoo page `stockTickers`/anchors/meta hashtag) alongside the body.
9. New `news_retag` ingest job (+ scheduler + `--run-once news_retag` parity):
   pick recent tag-poor `NewsItem`s whose `url` is a Yahoo page, fetch via
   `article_fetch`, run `from_html`, and **upgrade** `ticker`/`tickers_csv`/
   `tag_source='html+ai'` when it yields a better watchlist-validated set; re-link
   theses + re-publish on change. Budget-bounded; fail-open per item. Tests with
   HTML fixtures (the BNB/H/BTC snippet from PLAN.md → curated set).

## Acceptance criteria

- Full `uv run pytest -q` green, `ruff` clean, `--preflight` green.
- `--run-once news` (weekday): yfinance items show multi-ticker `tickers_csv`, the
  query ticker is demoted unless title-backed, `tag_source` is populated, and LLM
  budget is respected (one light call per article, max).
- The LLM still adds reasoned affected names (verify with a monkeypatched test and
  by eyeballing a few live items on the Pi dashboard after deploy).
- Forcing a search-fetch exception still produces tagged items via the old path.

## Notes

- Ingestion only — no Discord/surface changes; don't touch wallet math,
  `record_call`, or `verify.py`. No branches; commit to the working tree.
- Defensive: log once if the search API returns no `relatedTickers` for a ticker
  (so coverage stays observable on the Pi).
