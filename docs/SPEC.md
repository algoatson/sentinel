# Sentinel — Spec

A fully autonomous Discord-based trading intelligence system. Watches SEC filings, Reddit, Hacker News, and price action across a self-managed watchlist. Scores material events, posts contextualized summaries to Discord, and writes a daily digest. Learns from 👍/👎 reactions over time. No web UI, no slash commands beyond debugging. The user reads Discord; the system does the rest.

---

## 1. Architecture

Three independent layers, sharing a single SQLite database:

**Ingestion layer** — Passive, scheduled, zero LLM. Each source runs on its own cadence and writes to its own tables. One source failing never blocks another.

**Intelligence layer** — LLM-driven processors that read from the ingestion tables and produce derived data (summaries, scores, sentiment, digests). Every LLM call has exactly one job, bounded input, and deterministic routing of its output. No agent loop, no recursive thinking, no autonomy beyond what the scheduler grants.

**Surface layer** — Discord. Six channels, all bot-driven. The user reacts 👍/👎 on posts; that's the only interaction the system asks of them.

The agentic feel comes from scheduling, routing, and accumulation — not from giving any single LLM call autonomy.

---

## 2. Tech stack

- Python 3.12, managed with `uv`
- `discord.py` for the Discord bot
- `apscheduler` for the heartbeat
- `httpx` for HTTP
- `sqlmodel` over SQLite (file: `./data/radar.db`)
- `ollama` Python client pointed at a local Ollama server running Gemma 4
- `praw` for Reddit
- `yfinance` for prices
- `beautifulsoup4` + `lxml` for HTML parsing
- `pandas_market_calendars` for trading-day awareness
- `pyyaml` for config files
- `python-dotenv` for env loading
- `loguru` for logging
- `tenacity` for retry/backoff

**LLM endpoint**: Ollama running locally with Gemma 4. Default `http://localhost:11434`. The system runs `gemma4:e4b` for high-volume light tasks and `gemma4:31b` for heavy reasoning (or `gemma4:26b-a4b` MoE if VRAM is constrained — see operational notes).

---

## 3. Directory layout

```
sentinel/
  pyproject.toml
  .env.example
  README.md
  config/
    subreddits.yaml
    tracked_entities.yaml
    indices.yaml
  data/                         # gitignored
    radar.db
  src/sentinel/
    __init__.py
    main.py
    config.py
    db.py
    models.py
    llm.py
    prompts.py
    discord_client.py
    scheduler.py
    feedback.py
    edgar/
      __init__.py
      client.py
      watchlist_builder.py
    ingesters/
      __init__.py
      reddit.py
      hackernews.py
      prices.py
    pipelines/
      __init__.py
      filings.py
      enrich.py
      sentiment.py
      social_pulse.py
      digest.py
      tuning.py
  tests/
    test_edgar.py
    test_ticker_extraction.py
    test_prompts.py
    test_materiality.py
```

---

## 4. Environment variables

`.env.example`:

```
# Discord
DISCORD_TOKEN=
DISCORD_GUILD_ID=
DISCORD_USER_ID=                     # for @mentions on priority posts
DISCORD_PRIORITY_CHANNEL_ID=
DISCORD_FILINGS_CHANNEL_ID=
DISCORD_INSIDERS_CHANNEL_ID=
DISCORD_PULSE_CHANNEL_ID=
DISCORD_DIGEST_CHANNEL_ID=
DISCORD_META_CHANNEL_ID=

# LLM (Ollama running locally with Gemma 4)
OLLAMA_BASE_URL=http://localhost:11434
LLM_MODEL_LIGHT=gemma4:e4b
LLM_MODEL_HEAVY=gemma4:31b

# Reddit (free script-type app)
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=sentinel/0.1 by <yourname>

# SEC (free, just requires a UA string)
EDGAR_USER_AGENT=YourName your@email.com

# Cadence (minutes)
POLL_FILINGS_MINUTES=10
POLL_REDDIT_MINUTES=15
POLL_HN_MINUTES=30
POLL_PRICES_MINUTES=5
DIGEST_HOUR_ET=16
DIGEST_MINUTE_ET=30
```

---

## 5. Schema

All tables in SQLite via `sqlmodel`. Define every table below.

**`Watchlist`** — auto-managed, never user-edited
- `id` int PK
- `cik` str(10) indexed — zero-padded
- `ticker` str nullable indexed
- `source` enum(`index`, `tracked_entity`, `activity`)
- `added_at` datetime
- `expires_at` datetime nullable — non-null only for `activity` source (60d TTL)

**`TrackedEntity`** — mirrored from `config/tracked_entities.yaml` on startup
- `id` int PK
- `name` str
- `cik` str(10) indexed
- `type` enum(`fund`, `insider`)
- `notes` str nullable

**`Filing`**
- `id` int PK
- `cik` str(10)
- `ticker` str nullable
- `form_type` str
- `accession_number` str unique indexed
- `filed_at` datetime
- `primary_doc_url` str
- `summary` text nullable
- `materiality_score` int nullable — 0..3
- `materiality_reason` str nullable
- `posted_at` datetime nullable
- `message_id` str nullable indexed — Discord message id for feedback join
- `channel` str nullable — which Discord channel it was posted to

**`SeenFiling`** — dedupe table, written before any processing
- `accession_number` str PK
- `seen_at` datetime

**`RedditMention`**
- `id` int PK
- `subreddit` str
- `post_id` str indexed
- `comment_id` str nullable
- `ticker` str indexed
- `author` str
- `score` int
- `num_comments` int
- `created_at` datetime indexed
- `title` str
- `body_excerpt` str(500)
- `permalink` str
- `sentiment` int nullable — -1, 0, 1
- `is_thesis` bool nullable

**`HnMention`**
- `id` int PK
- `ticker` str indexed
- `hn_id` str unique
- `title` str
- `url` str
- `points` int
- `num_comments` int
- `author` str
- `created_at` datetime indexed

**`PriceBar`**
- `id` int PK
- `ticker` str indexed
- `ts` datetime indexed
- `open`, `high`, `low`, `close` float
- `volume` int
- unique constraint on (`ticker`, `ts`)

**`PriceContext`** — one row per ticker, updated in place
- `ticker` str PK
- `last_price` float
- `change_1d_pct` float
- `change_5d_pct` float
- `volume_vs_20d_avg` float
- `last_updated` datetime

**`SocialPulse`**
- `id` int PK
- `ticker` str
- `mention_count` int
- `baseline` float
- `ratio` float
- `summary` str
- `created_at` datetime
- `message_id` str nullable

**`Feedback`**
- `id` int PK
- `message_id` str indexed
- `emoji` str
- `user_id` str
- `created_at` datetime

**`PromptVersion`** — append-only history; one row marked `active=True` per `prompt_name`
- `id` int PK
- `prompt_name` str — e.g. `materiality`, `summarize_8k`
- `content` text
- `created_at` datetime
- `active` bool

---

## 6. Config files (committed to repo)

**`config/indices.yaml`** — which indices to auto-include in the watchlist:
```yaml
indices:
  - sp500
  - nasdaq100
```

**`config/subreddits.yaml`**:
```yaml
subreddits:
  - wallstreetbets
  - stocks
  - investing
  - SecurityAnalysis
  - ValueInvesting
  - options
```

**`config/tracked_entities.yaml`** — seed with a default set:
```yaml
entities:
  - name: Berkshire Hathaway
    cik: "0001067983"
    type: fund
  - name: Scion Asset Management
    cik: "0001649339"
    type: fund
  - name: Pershing Square Capital Management
    cik: "0001336528"
    type: fund
  - name: Greenlight Capital
    cik: "0001079114"
    type: fund
  - name: Appaloosa LP
    cik: "0001656456"
    type: fund
  - name: Baupost Group
    cik: "0001061165"
    type: fund
  # (add more — these are illustrative; verify CIKs at runtime)
```

`WatchlistBuilder` should validate every CIK in `tracked_entities.yaml` on startup and log a warning (don't crash) on any that 404 against EDGAR.

---

## 7. Module specs

### `config.py`
Pydantic-settings `Settings` class loading from `.env`. One exported singleton `settings`. All other modules import from here. No `os.getenv` calls anywhere else in the codebase.

### `db.py`
`engine = create_engine("sqlite:///./data/radar.db", ...)`. Single helper `session_scope()` context manager. `init_db()` creates all tables and runs the prompt seeding (insert default prompt versions from `prompts.py` if `PromptVersion` is empty).

### `models.py`
All SQLModel table definitions per the schema above.

### `llm.py`

```python
class LLM:
    def complete(self, prompt: str, *, model: Literal["light", "heavy"], 
                 json_mode: bool = False, max_tokens: int = 800) -> str: ...
```

- Uses the `ollama` Python client (`ollama.Client(host=settings.OLLAMA_BASE_URL)`)
- Picks the model string from `settings.LLM_MODEL_LIGHT` / `LLM_MODEL_HEAVY`
- Calls `client.generate(model=..., prompt=..., options={"num_predict": max_tokens, "temperature": 0.2}, format="json" if json_mode else "")`
- On startup, the LLM class checks `client.list()` and pulls any missing models with `client.pull()` — log progress, block startup until models are available
- `tenacity` retry: 3 attempts, exponential backoff 1s/4s/16s, retry on `httpx.ConnectError`, `httpx.ReadTimeout`, `ollama.ResponseError` with HTTP 5xx
- On final failure, returns the sentinel string `"[LLM_ERROR]"` — callers handle gracefully
- If `json_mode=True`, requests JSON format from Ollama natively; still strip ``` fences defensively
- Request timeout: 120s for light, 300s for heavy (local inference on big models is slow)

### `prompts.py`

All prompts live here as module constants. The active prompt for any name is read from `PromptVersion` at runtime, falling back to the constant if no DB row exists. This is what enables the tuning system to override prompts without code changes.

Helper: `get_prompt(name: str) -> str` — returns active DB version or module constant.

Prompt names and full content are defined in **Section 8** below.

### `edgar/client.py`

```python
class EdgarClient:
    def get_company_submissions(cik: str) -> dict: ...        # /submissions/CIK{cik}.json
    def list_recent_filings(cik: str, since: datetime) -> list[FilingMeta]: ...
    def fetch_primary_document(accession_number: str, primary_doc: str) -> str: ...
    def get_ticker_to_cik_map() -> dict[str, str]: ...        # /files/company_tickers.json, cached 24h
    def full_text_search(query: str) -> list[dict]: ...        # efts.sec.gov endpoint
```

- All requests use the `EDGAR_USER_AGENT` header (SEC requires this)
- Internal rate limiter: 8 req/sec ceiling (SEC limit is 10, leave headroom)
- HTML documents stripped to text via BeautifulSoup, truncated to 100k chars
- iXBRL documents: extract visible text only, drop XBRL tags

### `edgar/watchlist_builder.py`

```python
def build_watchlist() -> None: ...
```

On startup and weekly:
1. Fetch S&P 500 constituents from Wikipedia (`https://en.wikipedia.org/wiki/List_of_S%26P_500_companies`) — parse the first table
2. Fetch Nasdaq 100 constituents from Wikipedia (`https://en.wikipedia.org/wiki/Nasdaq-100`)
3. Resolve all tickers to CIKs via `get_ticker_to_cik_map()`
4. Upsert each as `Watchlist(source="index", expires_at=None)`
5. Load `config/tracked_entities.yaml`, upsert to `TrackedEntity` table and to `Watchlist(source="tracked_entity")`
6. Run activity promotion: any non-watchlisted CIK with ≥3 filings in the last 30 days OR any 8-K in the last 7 days → upsert with `source="activity", expires_at=now+60d`
7. Remove expired `activity` rows where `expires_at < now`

Activity promotion: run a single query against `Filing` table grouped by CIK. Skip on the very first run (table is empty).

### `ingesters/reddit.py`

```python
def poll_reddit() -> None: ...
```

- Initialize `praw.Reddit` from env credentials
- Iterate subreddits from `config/subreddits.yaml`
- For each sub: fetch `subreddit.new(limit=100)` and `subreddit.comments(limit=200)`
- For each post/comment, extract tickers using `extract_tickers(text, watchlist_tickers)` — see below
- Insert into `RedditMention` with `sentiment=None`, `is_thesis=None` (filled later by sentiment pipeline)
- Dedupe by `(post_id, comment_id, ticker)` — a single post mentioning 3 tickers produces 3 rows

**Ticker extraction (`extract_tickers`)** — this is where naive implementations fail. Rules:

1. Cashtag form (`$AAPL`) — always accept if cashtag value is in `watchlist_tickers`
2. Bare-ticker form (`AAPL`) — accept only if:
   - Token is in `watchlist_tickers` AND
   - Token appears ≥2 times in the text, OR the post's flair matches the ticker, OR the post title contains the cashtag form
3. Reject if ticker is a common English word from a hardcoded blocklist: `{"A", "ARE", "IT", "ALL", "ON", "BE", "OR", "AND", "FOR", "BY", "AT", "TO", "AS", "IS", "GO", "ANY", "CAN", "DO", "HAS", "HE", "I", "IF", "IN", "MY", "NO", "NOW", "OF", "OUT", "SO", "UP", "WE", "WHO", "YOU", "ONE", "TWO", "DD", "CEO", "USA", "USD", "EOD", "EPS", "PE", "PR", "IPO", "ATH", "ATL", "IV", "OTM", "ITM"}` regardless of cashtag

Test coverage in `tests/test_ticker_extraction.py` is mandatory — this is the single highest-noise source.

### `ingesters/hackernews.py`

```python
def poll_hackernews() -> None: ...
```

- For each ticker in `Watchlist`, build two queries: the ticker itself and the company name (look up from `EdgarClient.get_ticker_to_cik_map()` reverse mapping — actually company names come from `submissions.json`; cache per-cik)
- Endpoint: `https://hn.algolia.com/api/v1/search_by_date?tags=story&query={query}&numericFilters=created_at_i>{unix_ts_6h_ago}`
- No auth, no key
- Dedupe by `hn_id`
- Skip stories whose title contains neither the ticker nor the company name in a meaningful position (filter false positives — Algolia matches loosely)

### `ingesters/prices.py`

```python
def poll_prices() -> None: ...
```

- Only run during market hours (9:30–16:00 ET on trading days per `pandas_market_calendars`)
- Fetch all watchlist tickers in batches of 50 via `yfinance.download(tickers, period="1d", interval="1m", group_by="ticker", threads=True)`
- Best-effort: any single ticker failure is logged and skipped, never raised
- Insert new `PriceBar` rows (skip if ts already exists)
- Recompute `PriceContext` for each ticker: 
  - `last_price` from the latest bar
  - `change_1d_pct` = (last_close - prev_close) / prev_close
  - `change_5d_pct` = (last_close - close 5d ago) / close 5d ago
  - `volume_vs_20d_avg` = today's cumulative volume / 20d avg daily volume

A separate slower job (daily, 17:00 ET) fetches `period="30d", interval="1d"` for all tickers to keep daily bars current — needed for the 5d and 20d computations.

### `pipelines/filings.py`

```python
def run_filings_cycle() -> None: ...
```

For every CIK in `Watchlist`:
1. `client.list_recent_filings(cik, since=now-2h)` (2h overlap window absorbs poll jitter)
2. Filter out `accession_number` in `SeenFiling`
3. For each new filing, in order:
   - Insert `SeenFiling` row immediately (crash-safe dedupe)
   - Fetch primary document text
   - Call `summarize(form_type, text)` from `prompts.py` → `Filing.summary`
   - If `summary == "[LLM_ERROR]"`: insert Filing row with `posted_at=None`, log error, continue
   - Call `enrich(filing)` from `pipelines/enrich.py` → `EnrichmentContext`
   - Call `score_materiality(filing, enrichment)` → `(score, reason)`
   - Update Filing row with `summary`, `materiality_score`, `materiality_reason`
   - Route per **Section 9: Discord routing** rules
   - On post success, store `message_id` and `channel` on Filing row

Form-type → prompt mapping:
- `8-K` → `summarize_8k`
- `8-K/A` → `summarize_8k` (amendments still relevant)
- `4`, `4/A` → `summarize_form4`
- `10-Q`, `10-Q/A` → `summarize_10q`
- `10-K`, `10-K/A` → `summarize_10k`
- `13F-HR`, `13F-HR/A` → `summarize_13f`
- `S-1`, `S-1/A`, `424B*` → `summarize_offering`
- `DEF 14A`, `PRE 14A` → `summarize_proxy`
- Any other → `summarize_generic`

Model selection:
- `10-Q`, `10-K`, `13F`, `DEF 14A`, `S-1` → `heavy`
- All others → `light`

### `pipelines/enrich.py`

```python
@dataclass
class EnrichmentContext:
    reddit_mentions_24h: int
    reddit_mentions_baseline: float
    reddit_top_titles: list[str]
    reddit_avg_sentiment: float | None
    hn_mentions_24h: int
    hn_top_title: str | None
    price_change_1d_pct: float | None
    volume_ratio: float | None

def enrich(filing: Filing) -> EnrichmentContext: ...
```

Pure DB query, no LLM call. Renders into the Discord embed footer and is also passed into the materiality scorer.

### `pipelines/sentiment.py`

```python
def tag_recent_mentions() -> None: ...
```

Hourly. Selects `RedditMention` rows where `sentiment IS NULL` and `created_at > now - 24h`, in batches of 25. For each batch, builds a numbered list of `{title}\n{body_excerpt}` and calls the light model with the `tag_sentiment` prompt. Parses returned JSON array and updates rows. On parse error, mark `sentiment=0, is_thesis=False` so we don't keep retrying the same row.

### `pipelines/social_pulse.py`

```python
def run_social_pulse() -> None: ...
```

Hourly during market hours:
1. For each ticker in `Watchlist`, count `RedditMention` rows where `created_at > now-1h`
2. Compute 7-day rolling baseline (mentions per hour) — exclude the current hour
3. Find tickers where `current_hour_count > 3 * baseline AND baseline >= 1.0` (avoid divide-by-zero noise on dead tickers)
4. Exclude tickers that have a `Filing` row in the last 6 hours (those are already in #filings)
5. For each remaining spike ticker: gather top 5 Reddit posts by score in the last 1h
6. Call heavy model with `social_pulse` prompt
7. Insert `SocialPulse` rows and post a single embed to `DISCORD_PULSE_CHANNEL_ID` with all spikes

### `pipelines/digest.py`

```python
def write_daily_digest() -> None: ...
```

Runs at `DIGEST_HOUR_ET:DIGEST_MINUTE_ET` on trading days only:
1. Pull all `Filing` rows from today where `materiality_score >= 2`, sorted desc
2. Pull all `Filing` rows from today with `form_type IN ("4", "13F-HR")` and `materiality_score >= 2`
3. Pull all `SocialPulse` rows from today
4. Compose a structured input JSON for the heavy model
5. Call heavy model with `daily_digest` prompt
6. Post as embed to `DISCORD_DIGEST_CHANNEL_ID`

### `pipelines/tuning.py`

```python
def run_monthly_tuning() -> None: ...
```

Runs on the 1st of each month at 12:00 UTC:
1. Sample 20 `Filing` rows with 👍 feedback in the last 30 days
2. Sample 20 with 👎 feedback in the last 30 days
3. If either sample < 5, skip (insufficient signal)
4. Call heavy model with `tuning_suggest` prompt
5. Post the suggestion JSON to `DISCORD_META_CHANNEL_ID` with reaction handlers ✅ / ❌
6. On ✅: append the `proposed_prompt_delta` to the materiality prompt content, mark old `PromptVersion` inactive, insert new active version
7. On ❌: log and continue

### `feedback.py`

Discord event handler `on_reaction_add`:
- If reaction is on a message in any filing/pulse/digest channel, log a `Feedback` row
- If reaction is on a tuning proposal in #meta, dispatch to tuning apply/reject logic

### `discord_client.py`

Wraps `discord.py` bot setup. Exposes:
- `post_filing(filing: Filing, enrichment: EnrichmentContext, channel: str) -> str` returns message_id
- `post_digest(content: str) -> str`
- `post_pulse(content: str) -> str`
- `post_meta(content: str, expect_reaction: bool) -> str`
- All posts use rich embeds with color-coding (see Section 9)

### `scheduler.py`

`apscheduler` jobs:

| Job | Trigger | Function |
|---|---|---|
| `filings_cycle` | every `POLL_FILINGS_MINUTES` | `pipelines.filings.run_filings_cycle` |
| `reddit_poll` | every `POLL_REDDIT_MINUTES` | `ingesters.reddit.poll_reddit` |
| `hn_poll` | every `POLL_HN_MINUTES` | `ingesters.hackernews.poll_hackernews` |
| `prices_poll` | every `POLL_PRICES_MINUTES` (market hours only) | `ingesters.prices.poll_prices` |
| `prices_daily` | 17:00 ET trading days | `ingesters.prices.poll_daily_bars` |
| `sentiment_tag` | hourly | `pipelines.sentiment.tag_recent_mentions` |
| `social_pulse` | hourly (market hours only) | `pipelines.social_pulse.run_social_pulse` |
| `daily_digest` | DIGEST_HOUR_ET:DIGEST_MINUTE_ET trading days | `pipelines.digest.write_daily_digest` |
| `watchlist_rebuild` | weekly Sunday 06:00 UTC | `edgar.watchlist_builder.build_watchlist` |
| `monthly_tuning` | 1st of month 12:00 UTC | `pipelines.tuning.run_monthly_tuning` |

All jobs: `misfire_grace_time=120`, `max_instances=1`, `coalesce=True`.

### `main.py`

1. Load `.env`
2. `init_db()`
3. Run `build_watchlist()` once synchronously (block startup until watchlist exists)
4. Start `scheduler`
5. Start Discord bot (this blocks)
6. Graceful shutdown on SIGTERM: stop scheduler, close bot, close DB

---

## 8. Prompts

All prompts are committed as module constants in `prompts.py`. Each is also seeded into `PromptVersion` on first run so the tuning system can override.

### `summarize_8k`

```
You are reading an 8-K SEC filing. Identify the material event in plain English.

Rules:
- Lead with what changed. No preamble.
- One paragraph, max 150 words.
- Include key numbers and dates if present in the filing.
- Never invent numbers or facts not in the filing.
- If the filing is purely procedural (amendment with no material change, late filing notice, routine compensation), output exactly: "PROCEDURAL: <one-line reason>"

Filing text follows.
---
{text}
```

### `summarize_form4`

```
This is a Form 4 insider transaction filing. Report concisely:

- Insider name and role
- Transaction type: buy / sell / grant / option exercise / disposition
- Total shares transacted and approximate dollar value
- Approximate percentage of the insider's reported holdings this represents (compute from the post-transaction holdings shown)
- Whether this is a 10b5-1 plan trade or a discretionary trade
- Any unusual feature (first trade in months, large size, cluster with other recent insider trades on same issuer)

Max 90 words. End with a one-line read: is this a meaningful bullish or
bearish insider signal, or routine? (cluster, size vs. stake, and 10b5-1
vs. discretionary all inform it.)

Filing text follows.
---
{text}
```

### `summarize_10q`

```
This is a 10-Q quarterly filing. Identify the top 3 things that changed vs. the prior quarter.

For each change:
- What changed (revenue, margin, segment, guidance, language tone, balance sheet item)
- Magnitude with numbers
- Likely implication in one phrase

Rules:
- Max 250 words total.
- Lead with the most material change.
- Flag explicitly if any of these appear: going-concern language, material weakness, restatement, auditor change, guidance withdrawal.
- No boilerplate. No "the company reported revenue of..." — assume the reader knows it's an earnings report.

Filing text follows (may be truncated).
---
{text}
```

### `summarize_10k`

Same as `summarize_10q` but vs. prior year, max 300 words, and additionally surface any new risk factors or any items removed from the prior year's risk factors section.

### `summarize_13f`

```
This is a 13F-HR filing showing fund holdings as of quarter end.

Report:
- Top 5 new positions: ticker, size in USD, % of portfolio
- Top 5 exits: ticker, prior size
- Top 5 size increases: ticker, change %
- Top 5 size decreases: ticker, change %
- One sentence on any concentration shift (e.g., "increased financials exposure from 12% to 18%")

Max 200 words. If the previous 13F is not provided in context, note "no prior period available — initial holdings only" and list top 10 positions by size.

Filing text follows.
---
{text}
```

### `summarize_offering`

```
This is a securities offering filing (S-1, S-1/A, 424B). Report:
- Type (IPO, secondary, shelf takedown, ATM)
- Size (shares and approximate USD)
- Use of proceeds in one sentence
- Dilution to existing holders if computable
- Underwriters

Max 120 words.

Filing text follows.
---
{text}
```

### `summarize_proxy`

```
This is a proxy statement (DEF 14A / PRE 14A). Identify only the items shareholders are being asked to vote on that are non-routine:
- M&A votes
- Significant compensation changes
- Bylaw amendments
- Activist proposals
- Board changes beyond routine re-election

Skip: routine director re-election, routine auditor ratification, say-on-pay if unchanged from prior year.

Max 150 words. If nothing non-routine appears, output: "ROUTINE PROXY: <one-line confirmation>"

Filing text follows.
---
{text}
```

### `summarize_generic`

```
Summarize this SEC filing in plain English. Lead with the material content if any.
Max 100 words.
If the filing is purely administrative or procedural, output: "PROCEDURAL: <one-line reason>"

Filing text follows.
---
{text}
```

### `materiality`

```
You are scoring an SEC filing's materiality for the trader who runs this desk.

Score 0, 1, 2, or 3:

- 3 (HIGH): Material surprise that would meaningfully affect a thesis. Includes: guidance changes, M&A announcements, executive departures or unexpected appointments, large insider purchases (>$1M or >10% of insider's holdings), large new 13F positions from tracked entities, restatements, going-concern language, surprise earnings beats/misses, FDA decisions, settlement of major litigation, dividend cuts/initiations, share buyback authorizations >5% of float.

- 2 (NOTABLE): Material but expected. Includes: scheduled earnings without surprises, routine guidance reaffirmation with subtle tone shifts, smaller insider activity, 13F changes that move the portfolio but aren't dramatic, new contracts of meaningful size.

- 1 (ROUTINE): Standard quarterly content without surprises, scheduled compensation, routine S-8 employee plans, run-of-the-mill ATM takedowns.

- 0 (PROCEDURAL): Amendments with no material change, late filing notifications, routine prospectus supplements, administrative cleanup. Anything where the summary begins with "PROCEDURAL:" or "ROUTINE PROXY:".

Context to weight:
- If the filing summary explicitly says "PROCEDURAL" or "ROUTINE", score 0 regardless of other context.
- If the filing is genuinely material AND social attention is elevated (reddit_mentions_24h > 3x baseline), push borderline 2 to 3.
- If social is elevated but filing is procedural, score 0 — do not promote noise.
- For Form 4: weight by percentage of holdings, not just dollar amount. A $500k purchase that's 50% of an insider's reported stake is more material than a $5M sale that's 2% of stake.
- For 13F from a tracked entity: any new position >2% of portfolio is at least a 2.

Inputs:
- form_type: {form_type}
- ticker: {ticker}
- summary: {summary}
- enrichment: {enrichment_json}

Output strict JSON only:
{"score": <0|1|2|3>, "reason": "<one sentence, max 25 words>"}
```

### `tag_sentiment`

```
For each numbered Reddit item below, output one JSON object per item, in order, as a JSON array.

Each object:
- "sentiment": -1 (bearish on the ticker), 0 (neutral / unrelated to direction), or 1 (bullish on the ticker)
- "is_thesis": true if the item argues a specific position with reasoning, false if it's reaction, joke, question, or pure speculation

Output the JSON array only. No prose, no explanation.

Items:
{numbered_items}
```

### `social_pulse`

```
The following tickers are seeing unusually high Reddit activity right now (>3x their 7-day hourly baseline) with no corresponding SEC filing in the last 6 hours.

For each ticker, you have:
- ticker
- current_hour_mentions
- baseline_mentions
- top_5_posts (title and score)

Write one sentence per ticker describing:
- What people are discussing
- Whether it appears to be substance (product news, leak, sector rotation, real catalyst) or noise (memes, FOMO, momentum chasing, copycat from another sub)

Skip any ticker that is clearly noise. Better to output 2 substantive lines than 6 mediocre ones.

Output format, one ticker per line:
{ticker}: {one sentence}

Inputs:
{spike_data_json}
```

### `daily_digest`

```
You are writing the end-of-day brief for the trader whose personal book this is.

Inputs (JSON):
- filings_materiality_3: array of {ticker, form_type, summary, reason}
- filings_materiality_2: array of same shape
- insider_activity: array of {ticker, summary} from Form 4 / 13F
- social_pulses: array of {ticker, summary}
- date: today's date

Write a 380-480 word brief structured as:
1. One opening sentence on the day's biggest theme if one exists. If no clear theme, skip — go straight to substance.
2. 3-5 short paragraphs grouped by natural theme (earnings surprises, M&A, insider clusters, sector moves, etc.). Reference tickers with $TICKER form.
3. A "Watch for tomorrow" paragraph noting any pending earnings or scheduled events you can infer from today's filings.
4. **The read** — 2-3 sentences: your actual take. What you'd lean toward, trim, add to, or sit on into tomorrow, with a confidence. Commit — don't just recap the day.

Rules:
- No bullet points. No hedging language ("could potentially", "may possibly"). Be direct.
- Lead each paragraph with the most material item in that theme.
- Don't repeat ticker summaries verbatim — synthesize.
- If the day was genuinely quiet, say so in 150 words and stop.

Inputs:
{input_json}
```

### `tuning_suggest`

```
Below are 20 filings the user reacted 👍 to and 20 they reacted 👎 to over the last 30 days.

Each row includes: form_type, materiality_score, materiality_reason, summary excerpt, ticker.

The user's reactions reveal what they actually find valuable vs. what the current scorer overvalues or undervalues.

Analyze the pattern. Look for:
- Form types they consistently like or dislike
- Topics within filings (insider activity, guidance, M&A, etc.)
- Score levels that don't match their reactions

Output strict JSON only:
{
  "current_issue": "<what the scorer is currently getting wrong, one sentence>",
  "proposed_prompt_delta": "<text to append to the materiality prompt as an additional rule>",
  "rationale": "<why this delta should help, one sentence>"
}

The proposed_prompt_delta should be a single sentence or short paragraph that gets appended to the existing materiality prompt's "Context to weight" section.

Inputs:
{feedback_data_json}
```

---

## 9. Discord routing rules

Score-based routing applied after the materiality scorer returns:

| Form type | Score | Channel | Mention |
|---|---|---|---|
| Form 4, 13F | ≥ 2 | `#insiders` | none |
| Form 4, 13F | 0–1 | (not posted) | — |
| Any other | 3 | `#priority` | `@user_id` |
| Any other | 2 | `#filings` | none |
| Any other | 0–1 | (not posted) | — |

All non-posted filings still get a `Filing` row with `posted_at=NULL` and `message_id=NULL`.

**Embed format:**
- Title: `[{ticker}] {form_type}` — color-coded:
  - 8-K → red (0xD64545)
  - Form 4 → blue (0x4B7BEC)
  - 10-Q / 10-K → green (0x2ECC71)
  - 13F → purple (0x8E44AD)
  - S-1 / 424B → orange (0xE67E22)
  - DEF 14A → yellow (0xF1C40F)
  - default → gray (0x95A5A6)
- Description: `summary`
- Fields:
  - "Filed" — `filed_at` ET
  - "Materiality" — `{score}/3 — {reason}`
- Footer: enrichment context, e.g. `📊 Price: -3.2% on 2.4x volume · 💬 Reddit: 47 mentions (↑ from 3 yesterday) · HN: 1 story`
- URL: link to EDGAR filing

---

## 10. Phased build order

Build in this order. Each phase is its own session and must end with the exit criterion satisfied.

**Phase 1 — Skeleton + filings pipeline (no enrichment, no scoring)**
- All schema, `config.py`, `db.py`, `models.py`, `llm.py`, `prompts.py`, `discord_client.py`, `scheduler.py`, `main.py`
- `edgar/client.py`, `edgar/watchlist_builder.py`
- `pipelines/filings.py` with summarization only — no enrichment, no scoring. Every filing posts to `#filings`.
- Exit: with default Groq Gemma config, run for 1 hour, see real summaries appear in `#filings` for at least one watchlist filing.

**Phase 2 — Enrichment**
- `ingesters/reddit.py`, `ingesters/hackernews.py`, `ingesters/prices.py`
- `pipelines/enrich.py`
- Filings pipeline now calls `enrich()` and renders the footer
- Exit: filings posts include populated footer with at least one of the enrichment data points present.

**Phase 3 — Materiality scoring + routing**
- `materiality` prompt
- Scoring step added to filings pipeline
- Channel routing per Section 9
- Exit: over 24h, `#priority` has 1–5 posts, `#filings` has 10–50, `#insiders` has at least one Form 4 if any tracked entity filed.

**Phase 4 — Sentiment + social pulse**
- `pipelines/sentiment.py`
- `pipelines/social_pulse.py`
- Exit: `RedditMention.sentiment` populated for >50% of last-24h rows; if a spike occurs, `#pulse` gets a post.

**Phase 5 — Daily digest**
- `pipelines/digest.py`
- Exit: at 4:30 ET on a trading day, exactly one digest post appears in `#digest`.

**Phase 6 — Feedback + tuning**
- `feedback.py`
- `pipelines/tuning.py`
- Exit: react 👍 on 3 posts and 👎 on 3 posts; verify `Feedback` rows exist. (Tuning itself only fires monthly, so test it manually with a `--run-tuning-now` CLI flag.)

---

## 11. Exit criteria (full system)

Run for 24 hours on a trading day with default config:

1. `#filings` populated with 10–50 posts, all with non-empty summaries, materiality scores, and enrichment footers
2. `#priority` populated with 1–5 posts, all materiality 3
3. `#insiders` has at least one Form 4 or 13F post
4. `#pulse` has at least one post if any social spike occurred
5. `#digest` has exactly one post at 4:30 ET
6. No duplicate posts (restart the bot mid-run to confirm dedupe works)
7. No crashes; `#meta` empty or contains only informational logs
8. React 👍 and 👎 on assorted posts; verify `Feedback` rows
9. Force `run_monthly_tuning()` via debug command; verify a suggestion appears in `#meta` with `✅`/`❌` reactions handled correctly

---

## 12. Non-goals (do not build these even if it seems easy)

- X/Twitter integration (paywall, not worth it)
- Paid news APIs (Bloomberg, Reuters, etc.)
- Auto-trading or live broker integration (paper-only, by design)
- Backtesting framework
- Multi-user support, permissions, ACLs
- Web dashboard, mobile app, or any UI outside Discord
- Slash commands for watchlist management (the watchlist is autonomous)
- Image / chart generation
- Multi-language support

Keep the surface area small. Every feature past the spec adds maintenance burden against zero revenue.

### Stance on opinions, calls, and advice

This is a **single-user, personal, paper-only** trading copilot — not a
regulated product and not a neutral newswire. It is *expected* to think:
form conclusions, take directional views, size conviction, give concrete
buy / sell / exit / positioning advice, and own the outcome. The scorecard
and the three autonomous funds exist precisely to keep it honest about those
calls — accountability, not abstention, is the discipline here.

There is no compliance surface on a private paper tool, so:

- **No disclaimers.** No "not financial advice" / "informational only" /
  "not a recommendation" anywhere — posts, embeds, threads, footers.
- **No refusing a take.** A hedge-free call with the risk stated in a clause
  is the product; a disclaimer or a punt is a bug.
- **Predictions, signals, and directional calls are core, not non-goals.**
  synthesis / why_moved / convergence → `TradingCall` → scorecard + funds is
  the spine of the whole system.

The one rule that is *not* relaxed: **never fabricate a number, price, or
fact.** Ground every claim in the data or in real market knowledge, and
separate fact from inference from bet — but still bet. (Earlier drafts of
this spec carried generic template restrictions — "no prediction", "nothing
that suggests buy or sell". Those were boilerplate and are intentionally
removed; the call *is* the value.)

---

## 13. Operational notes

- **SQLite is fine.** Don't reach for Postgres. WAL mode on, that's all you need for this load.
- **Logging:** `loguru` to stdout + rotating file in `./data/logs/`. INFO level by default, DEBUG for ingester modules.
- **Error policy:** every pipeline catches `Exception` at the top level, logs with traceback, posts a one-line alert to `#meta`, and continues. The system must never crash on bad data.
- **Time zones:** store everything in UTC. Convert to ET only at display time (digest, scheduler triggers tied to market hours).
- **Reddit credentials:** create a script-type app at `https://www.reddit.com/prefs/apps`. Free, takes 5 minutes.
- **Ollama setup:** install Ollama (`https://ollama.com/download`), then `ollama pull gemma4:e4b` and `ollama pull gemma4:31b`. The bot calls `ollama list` on startup and pulls anything missing — but it's faster to do it ahead of time. Keep Ollama running as a service: macOS launches it automatically after install; on Linux use `systemctl --user enable --now ollama`.
- **Model sizing:** the 31B dense model wants ~24GB VRAM at Q4 quant, ~40GB at Q8. If you don't have that, swap `LLM_MODEL_HEAVY` to `gemma4:26b-a4b` (the MoE variant — only 4B active params per token, runs comfortably on 16GB VRAM with good quality). The E4B model needs ~4-6GB and runs anywhere. **Set `OLLAMA_KEEP_ALIVE=30m`** in the Ollama server env so models stay loaded between pipeline cycles — otherwise every filing cycle pays a cold-load tax.
- **Concurrency:** Ollama serializes requests per model by default. Filings pipeline is fine (sequential). The sentiment tagger and pulse analyzer also call the LLM — schedule them to never overlap with the filings cycle if you're on a single-GPU box. The scheduler config already enforces `max_instances=1` per job; just be aware that two jobs hitting the same model at the same time will queue, not parallelize.
- **Local-only means no API costs and no rate limits, but you pay in latency.** Heavy-model calls on the 31B will take 10-60s depending on hardware. The filings cycle's 10-minute cadence has plenty of headroom; the digest's once-daily call has all the time it needs. The hourly sentiment tagger is the one to watch — keep batches small (25 items) so any single call completes in <2 min.
- **First-run cost:** the initial watchlist build hits EDGAR ~600 times. Respect the rate limiter. Subsequent runs only touch new filings.