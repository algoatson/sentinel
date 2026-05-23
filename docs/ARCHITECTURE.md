# Sentinel — Architecture

Visual reference of how the bot is wired together as of the news-alerts addition.

Three-layer architecture sharing a single SQLite database:

1. **Ingestion** — passive, scheduled, zero LLM. Each source writes to its own tables.
2. **Intelligence** — LLM-driven pipelines that read ingestion tables and produce derived data.
3. **Surface** — Discord, six channels. User interacts with reactions and chat.

The agentic feel is a function of scheduling + accumulation, not any single LLM having autonomy.

---

## 1. Layered overview

```mermaid
flowchart TB
    classDef src fill:#1f5582,stroke:#0a2540,color:#fff,stroke-width:1px
    classDef ing fill:#2e7d8f,stroke:#0a2540,color:#fff,stroke-width:1px
    classDef db fill:#37474f,stroke:#0a2540,color:#fff,stroke-width:1px
    classDef light fill:#2e7d32,stroke:#0a2540,color:#fff,stroke-width:1px
    classDef heavy fill:#c0392b,stroke:#0a2540,color:#fff,stroke-width:1px
    classDef none fill:#6c5ce7,stroke:#0a2540,color:#fff,stroke-width:1px
    classDef disc fill:#5c3a92,stroke:#0a2540,color:#fff,stroke-width:1px
    classDef user fill:#d35400,stroke:#0a2540,color:#fff,stroke-width:1px

    subgraph SRC["📡 External sources"]
      direction LR
      SEC[SEC EDGAR]:::src
      HNAPI[Hacker News Algolia]:::src
      YFIN[yfinance / Yahoo]:::src
      RSS[13 RSS feeds]:::src
      WIKI[Wikipedia indices]:::src
    end

    subgraph ING["🔄 Ingesters — passive, no LLM"]
      direction LR
      I1["watchlist_builder<br/>Sun 06:00 UTC + on boot"]:::ing
      I2["filings ingest<br/>every 10 min"]:::ing
      I3["hn_poll<br/>every 30 min"]:::ing
      I4["prices_poll<br/>5 min, market hrs"]:::ing
      I5["prices_daily<br/>17:00 ET"]:::ing
      I6["news_poll<br/>every 20 min"]:::ing
      I7["reddit_poll<br/>STUB — PRAW pending"]:::ing
    end

    DB[("💾 SQLite · ./data/radar.db<br/>13 tables<br/><br/>Watchlist · TrackedEntity · Filing · SeenFiling<br/>RedditMention · HnMention · NewsItem<br/>PriceBar · PriceContext<br/>SocialPulse · Feedback · PromptVersion")]:::db

    subgraph INTEL["🧠 Intelligence — LLM-driven"]
      direction TB
      subgraph FAST["⚡ Light tier (gemma4:e4b)"]
        direction LR
        L1["filings + materiality<br/>10 min · per filing"]:::light
        L2["sentiment_tag<br/>1h batches of 25"]:::light
        L3["news_alerts<br/>15 min · LLM triage"]:::light
        L4["chat handler<br/>on demand · RAG"]:::light
      end
      subgraph SLOW["🐢 Heavy tier (qwen2.5:14b-instruct)"]
        direction LR
        H1["macro_themes<br/>every 4h"]:::heavy
        H2["social_pulse<br/>1h · market hrs"]:::heavy
        H3["convergence<br/>every 30 min"]:::heavy
        H4["movers_daily<br/>16:15 ET"]:::heavy
        H5["premarket_briefing<br/>08:30 ET"]:::heavy
        H6["daily_digest<br/>16:30 ET"]:::heavy
        H7["monthly_tuning<br/>1st 12:00 UTC"]:::heavy
      end
      subgraph PURE["📊 Pure-SQL (no LLM)"]
        direction LR
        N1["enrich<br/>per-filing"]:::none
        N2["news_impact_tag<br/>hourly"]:::none
      end
    end

    subgraph DISC["💬 Discord"]
      direction LR
      C1["#priority"]:::disc
      C2["#filings"]:::disc
      C3["#insiders"]:::disc
      C4["#pulse"]:::disc
      C5["#digest"]:::disc
      C6["#meta"]:::disc
    end

    subgraph U["👤 You"]
      direction LR
      UC["Chat<br/>!status · !ticker · !news ·<br/>!recent · !filing · @mention"]:::user
      UR["Reactions<br/>👍 👎 · ✅ ❌"]:::user
    end

    SRC --> ING
    ING --> DB
    DB --> INTEL
    INTEL --> DB
    INTEL --> DISC
    UC --> L4
    L4 --> DISC
    DISC --> UR
    UR --> H7
    UR --> DB
```

---

## 2. Detailed data flow

What reads what, what writes what. Solid arrows = primary data flow; dashed = LLM call; dotted = enrichment context.

```mermaid
flowchart LR
    classDef tab fill:#37474f,stroke:#0a2540,color:#fff
    classDef ing fill:#2e7d8f,stroke:#0a2540,color:#fff
    classDef light fill:#2e7d32,stroke:#0a2540,color:#fff
    classDef heavy fill:#c0392b,stroke:#0a2540,color:#fff
    classDef none fill:#6c5ce7,stroke:#0a2540,color:#fff
    classDef ch fill:#5c3a92,stroke:#0a2540,color:#fff

    %% Ingesters
    WB[watchlist_builder]:::ing
    FI[filings ingest]:::ing
    HN[hn_poll]:::ing
    PP[prices_poll]:::ing
    NP[news_poll]:::ing

    %% Tables
    WL[(Watchlist)]:::tab
    F[(Filing)]:::tab
    HM[(HnMention)]:::tab
    NI[(NewsItem)]:::tab
    PB[(PriceBar)]:::tab
    PC[(PriceContext)]:::tab
    SP[(SocialPulse)]:::tab
    FB[(Feedback)]:::tab
    PV[(PromptVersion)]:::tab
    RM[(RedditMention)]:::tab

    %% Pipelines
    ENR{{enrich}}:::none
    MAT{{materiality}}:::light
    SENT{{sentiment_tag}}:::light
    NA{{news_alerts}}:::light
    CHAT{{chat handler}}:::light

    SOC{{social_pulse}}:::heavy
    MT{{macro_themes}}:::heavy
    CONV{{convergence}}:::heavy
    MOV{{movers}}:::heavy
    BR{{briefing}}:::heavy
    DIG{{digest}}:::heavy
    TUN{{tuning}}:::heavy
    NIMP{{news_impact}}:::none

    %% Discord channels
    CPRIO>#priority]:::ch
    CFIL>#filings]:::ch
    CINS>#insiders]:::ch
    CPULSE>#pulse]:::ch
    CDIG>#digest]:::ch
    CMETA>#meta]:::ch

    %% Ingestion writes
    WB --> WL
    FI --> F
    HN --> HM
    PP --> PB
    PP --> PC
    NP --> NI

    %% enrich reads many, writes nothing (returns a dataclass)
    F -.-> ENR
    HM -.-> ENR
    NI -.-> ENR
    PC -.-> ENR
    RM -.-> ENR

    %% Materiality (filings pipeline)
    F --> MAT
    ENR -.-> MAT
    MAT --> F
    MAT --> CPRIO
    MAT --> CFIL
    MAT --> CINS

    %% Sentiment tagger
    RM --> SENT
    SENT --> RM

    %% News pipelines
    NI --> NA
    NA --> NI
    NA --> CPULSE

    NI --> MT
    WL --> MT
    MT --> CPULSE

    NI --> NIMP
    PB --> NIMP
    NIMP --> NI

    %% Social pulse
    RM --> SOC
    PC --> SOC
    F --> SOC
    SOC --> SP
    SOC --> CPULSE

    %% Convergence
    F --> CONV
    PC --> CONV
    RM --> CONV
    HM --> CONV
    NI --> CONV
    CONV --> CPRIO

    %% Movers
    PC --> MOV
    HM --> MOV
    RM --> MOV
    NI --> MOV
    MOV --> WL
    MOV --> CPULSE

    %% Briefing
    F --> BR
    NI --> BR
    HM --> BR
    SP --> BR
    PC --> BR
    BR --> CDIG

    %% Daily digest
    F --> DIG
    SP --> DIG
    DIG --> CDIG

    %% Tuning loop
    FB --> TUN
    F --> TUN
    TUN --> PV
    TUN --> CMETA

    %% Chat + RAG
    F -.-> CHAT
    NI -.-> CHAT
    PC -.-> CHAT
    HM -.-> CHAT
    SP -.-> CHAT
    CHAT --> CPRIO
    CHAT --> CFIL
    CHAT --> CINS
    CHAT --> CPULSE
    CHAT --> CDIG
    CHAT --> CMETA

    %% Feedback collection
    CPRIO --> FB
    CFIL --> FB
    CINS --> FB
    CPULSE --> FB
    CDIG --> FB
    CMETA --> FB
```

---

## 3. Discord channel routing

Where each post type lands and what reactions it accepts:

| Producer | Channel | Mention | Reactions used |
|---|---|---|---|
| filings ⟶ score 3 (non-insider) | `#priority` | `@you` | 👍/👎 → feedback |
| filings ⟶ score 2 (non-insider) | `#filings` | — | 👍/👎 → feedback |
| filings ⟶ Form 4 / 13F score≥2 | `#insiders` | — | 👍/👎 → feedback |
| filings ⟶ score 0-1 | (DB only, no post) | — | — |
| `social_pulse` spike | `#pulse` | — | 👍/👎 |
| `macro_themes` themes | `#pulse` | — | 👍/👎 |
| `news_alerts` 🚨 alert | `#pulse` | — | 👍/👎 |
| `movers` daily | `#pulse` | — | 👍/👎 |
| `convergence` 🎯 | `#priority` | `@you` | 👍/👎 |
| `premarket_briefing` 🌅 | `#digest` | — | 👍/👎 |
| `daily_digest` 📊 | `#digest` | — | 👍/👎 |
| `monthly_tuning` proposal | `#meta` | — | ✅ apply / ❌ reject |
| Pipeline errors | `#meta` | — | — |
| Chat replies (you ⟶ bot) | same channel as your message | — | — |

---

## 4. Scheduler cadence

17 jobs total. UTC unless noted.

| Job | Trigger | Frequency | Tier | What it does |
|---|---|---|---|---|
| `filings_cycle` | interval | 10 min | light | New filings → summarize + score + route |
| `reddit_poll` | interval | 15 min | none | **STUB** — PRAW pending |
| `hn_poll` | interval | 30 min | none | Algolia search per ticker/company |
| `prices_poll` | interval | 5 min | none | 1-min bars during market hours |
| `prices_daily` | cron | 17:00 ET | none | 30d daily bar refresh |
| `news_poll` | interval | 20 min | none | RSS + yfinance per-ticker |
| `news_alerts` | interval | 15 min | light | LLM triage tier-1 fresh news |
| `news_impact_tag` | interval | 1h | none | Measure realized 1h/1d return per news item |
| `sentiment_tag` | interval | 1h | light | Tag RedditMention rows |
| `social_pulse` | interval | 1h (mkt) | heavy | Spike detection + LLM synthesis |
| `convergence` | interval | 30 min | heavy | 2+ signal alignment per ticker |
| `macro_themes` | interval | 4h | heavy | Cluster macro headlines into themes |
| `movers_daily` | cron | 16:15 ET | heavy | Top % movers without filing trigger |
| `premarket_briefing` | cron | 08:30 ET | heavy | Overnight synthesis |
| `daily_digest` | cron | 16:30 ET | heavy | End-of-day digest |
| `watchlist_rebuild` | cron | Sun 06:00 UTC | none | S&P 500 + Nasdaq 100 + ETFs + activity |
| `monthly_tuning` | cron | 1st 12:00 UTC | heavy | Propose materiality prompt delta |

---

## 5. LLM tier assignment

```mermaid
flowchart LR
    classDef light fill:#2e7d32,stroke:#0a2540,color:#fff
    classDef heavy fill:#c0392b,stroke:#0a2540,color:#fff
    classDef none fill:#6c5ce7,stroke:#0a2540,color:#fff

    subgraph "⚡ LIGHT — gemma4:e4b (fast)"
      F1[filings summaries]:::light
      F2[materiality scoring]:::light
      F3[sentiment_tag]:::light
      F4[news_alerts triage]:::light
      F5[chat / @mention RAG]:::light
    end

    subgraph "🐢 HEAVY — qwen2.5:14b-instruct (quality)"
      H1[macro_themes]:::heavy
      H2[social_pulse synthesis]:::heavy
      H3[convergence synthesis]:::heavy
      H4[movers hypothesis]:::heavy
      H5[premarket_briefing]:::heavy
      H6[daily_digest]:::heavy
      H7[monthly_tuning]:::heavy
    end

    subgraph "📊 NO LLM — pure DB/code"
      N1[enrich]:::none
      N2[news_impact_tag]:::none
      N3[ingesters — all of them]:::none
      N4[scheduler]:::none
      N5[discord routing]:::none
    end
```

Sampling defaults (auto-selected on model tag prefix):
- Gemma: `temperature=1.0, top_p=0.95, top_k=64`
- Qwen3: `temperature=0.7, top_p=0.8, top_k=20, min_p=0` (plus `/no_think` injection for JSON mode)

---

## 6. Discovery loop

How the watchlist self-expands beyond the seed indices:

```mermaid
flowchart LR
    classDef seed fill:#1f5582,stroke:#0a2540,color:#fff
    classDef promote fill:#d35400,stroke:#0a2540,color:#fff
    classDef table fill:#37474f,stroke:#0a2540,color:#fff

    S1[S&P 500 · Nasdaq 100<br/>from Wikipedia]:::seed
    S2[config/etfs.yaml<br/>~46 sector + leveraged ETFs]:::seed
    S3[config/tracked_entities.yaml<br/>tracked funds]:::seed

    P1["activity promotion<br/>3+ filings in 30d<br/>OR any 8-K in 7d"]:::promote
    P2["movers discovery<br/>yfinance day_gainers<br/>not on watchlist"]:::promote

    WL[(Watchlist)]:::table

    S1 --> WL
    S2 --> WL
    S3 --> WL
    P1 --> WL
    P2 --> WL

    WL -.->|expired 'activity' rows<br/>cleaned weekly| WL
```

Future: social-mention discovery when Reddit is wired (any unseen cashtag crossing a mention threshold → promote).

---

## 7. Feedback loop

How 👍 / 👎 close back into the prompt:

```mermaid
flowchart LR
    classDef ch fill:#5c3a92,stroke:#0a2540,color:#fff
    classDef table fill:#37474f,stroke:#0a2540,color:#fff
    classDef heavy fill:#c0392b,stroke:#0a2540,color:#fff
    classDef light fill:#2e7d32,stroke:#0a2540,color:#fff

    P>posts]:::ch
    R["👍 / 👎 react<br/>(you)"]
    FB[(Feedback)]:::table
    F[(Filing)]:::table
    TUN{{monthly_tuning<br/>1st of month}}:::heavy
    META>#meta proposal<br/>with ✅ / ❌]:::ch
    PV[(PromptVersion)]:::table
    MAT{{materiality scorer<br/>future cycles}}:::light

    P --> R
    R --> FB
    FB --> TUN
    F --> TUN
    TUN --> META
    META -- "you ✅" --> PV
    META -- "you ❌" --> R
    PV --> MAT
```

---

## 8. File map

```
sentinel/
├── config/
│   ├── indices.yaml          # which indices the watchlist pulls
│   ├── etfs.yaml             # curated ETF list
│   ├── tracked_entities.yaml # funds/insiders by CIK
│   ├── subreddits.yaml       # for when Reddit comes online
│   └── news_feeds.yaml       # 13 RSS feeds (macro + tier-1 + Google News topics)
├── data/
│   └── radar.db              # SQLite, WAL mode
├── docs/
│   ├── SPEC.md               # original spec
│   └── ARCHITECTURE.md       # this file
├── src/sentinel/
│   ├── main.py               # entry point, --run-once registry
│   ├── config.py             # pydantic-settings Settings
│   ├── db.py                 # engine + session_scope + inline migrations
│   ├── models.py             # all 13 SQLModel tables
│   ├── llm.py                # Ollama wrapper, family-aware sampling, parse_json_response
│   ├── prompts.py            # all SPEC §8 prompts + seed_prompts
│   ├── discord_client.py     # bot, post_filing, post_meta, post_digest, post_pulse, run_with_bot
│   ├── chat.py               # !commands + @mention RAG
│   ├── feedback.py           # on_raw_reaction_add (filings feedback + tuning apply/reject)
│   ├── scheduler.py          # 17-job AsyncIOScheduler
│   ├── utils.py              # extract_tickers (cashtag/bare/blocklist rules)
│   ├── edgar/
│   │   ├── client.py         # EDGAR HTTP w/ 8 req/s limiter, company_tickers, submissions
│   │   └── watchlist_builder.py
│   ├── ingesters/
│   │   ├── reddit.py         # STUB — PRAW pending
│   │   ├── hackernews.py     # Algolia search per ticker/company
│   │   ├── news.py           # RSS + yfinance per-ticker
│   │   └── prices.py         # yfinance + market-hours guard
│   └── pipelines/
│       ├── filings.py        # summarize + materiality + route
│       ├── enrich.py         # pure DB → EnrichmentContext (filings + chat + materiality)
│       ├── sentiment.py      # batched LLM tagging of RedditMention
│       ├── social_pulse.py   # spike detection + heavy synthesis
│       ├── convergence.py    # 2+ signal stacking → #priority
│       ├── movers.py         # PriceContext outliers + day_gainers discovery
│       ├── macro_themes.py   # cluster macro headlines → themes (with validator)
│       ├── news_alerts.py    # LLM-triaged tier-1 breaking news
│       ├── news_impact.py    # measure realized 1h/1d return per news item
│       ├── briefing.py       # pre-market briefing
│       ├── digest.py         # end-of-day digest
│       └── tuning.py         # monthly feedback-driven prompt delta
└── tests/
    ├── test_ticker_extraction.py   # 12 cases — passing
    ├── test_prompts.py             # 8 cases — passing
    ├── test_edgar.py               # placeholder
    └── test_materiality.py         # placeholder
```

---

## 9. Non-goals (worth restating)

Per SPEC §12, the bot does **not** do any of the following, even though they might seem tempting:

- Auto-trading, broker integration, position sizing
- Price prediction or signal generation
- Backtesting framework
- Multi-user, ACLs, web dashboard
- X/Twitter integration, Discord-scraping of other servers
- Paid news APIs (Bloomberg, Reuters paid)
- LLM-speculation discovery of "related" tickers
- Slash commands for portfolio management

The bot's role is **information surfacing + correlation + RAG**, not decision-making. The edge comes from:

1. **Breadth** — ~600 names + ETFs continuously monitored.
2. **Speed** — sub-10-min latency on SEC filings.
3. **Cross-source synthesis** — filings × price × HN × news × Reddit (when wired).
4. **Measurement** — every news item gets tagged with realized price reaction.
5. **Adaptation** — 👍/👎 feedback → monthly prompt-tuning loop.

You bring the trading judgment; the bot keeps you informed without you having to refresh anything.
