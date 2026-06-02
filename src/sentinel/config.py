from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root — the parent of `src/`. Used by code that loads YAML configs
# so the bot can be launched from anywhere (the previous code assumed CWD
# was the repo root, which silently failed under systemd or cron).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    DISCORD_TOKEN: str = ""
    DISCORD_GUILD_ID: int = 0
    DISCORD_PRIORITY_CHANNEL_ID: int = 0
    DISCORD_FILINGS_CHANNEL_ID: int = 0
    DISCORD_INSIDERS_CHANNEL_ID: int = 0
    DISCORD_PULSE_CHANNEL_ID: int = 0
    DISCORD_DIGEST_CHANNEL_ID: int = 0
    DISCORD_META_CHANNEL_ID: int = 0
    # Dedicated news channel. Breaking-news + geopolitical/relational analysis
    # posts here. Falls back to the pulse channel when unset (0).
    DISCORD_NEWS_CHANNEL_ID: int = 0
    # Dedicated crypto channel. ALL per-asset crypto content (trending, crypto
    # convergence, crypto why-moved, crypto news alerts) routes here so it
    # stops flooding #priority / #news. Falls back to #news when unset (0).
    DISCORD_CRYPTO_CHANNEL_ID: int = 0
    # General/lounge channel — relaxed, non-signal: grounded geopolitics↔market
    # musings + a curated community highlight, twice a day, gated. Falls back
    # to the digest channel when unset (0).
    DISCORD_GENERAL_CHANNEL_ID: int = 0
    # Dedicated Reddit-stream channel — individual notable r/ posts (ticker is
    # moving today and/or the community is surging on it). It is intentionally
    # opt-in: when unset (0) the pipeline skips entirely rather than firehose
    # raw Reddit into another channel.
    DISCORD_REDDIT_CHANNEL_ID: int = 0
    # Call-resolution ("📒 Called It") verdicts — the visible half of the
    # accountability loop. Falls back to the digest channel, then #meta.
    DISCORD_CALLS_CHANNEL_ID: int = 0
    # Proactive book-risk alerts on your OPEN paper positions (adverse
    # drawdown, earnings imminent, fresh material filing/news on the name).
    # The bot's most urgent voice — falls back to the priority channel,
    # then #meta. It only ever speaks when a position is actually in trouble.
    DISCORD_RISK_CHANNEL_ID: int = 0
    # The autonomous wallets narrating *why* they took/closed each position
    # (the triggering thesis + the mechanical exit reason). Falls back to the
    # digest channel, then #meta. Only posts on cycles where something traded.
    DISCORD_FUNDS_CHANNEL_ID: int = 0
    # Hot movers — gainers/losers from the watchlist with |1d %| above a
    # threshold AND volume above 20d-avg. Posts a compact embed listing the
    # top movers, with a per-ticker cooldown so a sustained mover doesn't
    # respam. Unset (0) → pipeline skips entirely (opt-in).
    DISCORD_HOT_CHANNEL_ID: int = 0
    # Multi-source convergence — when filings + news + social/HN all line up
    # on the same direction for a name within a window. Falls back to the
    # priority channel when unset; gives the highest-conviction signals
    # their own visible track instead of mixing into other #priority posts.
    DISCORD_CONVERGENCE_CHANNEL_ID: int = 0
    # Macro/geopolitical news ONLY (NewsItem.is_macro=True). Splits this
    # stream off #news so per-ticker news stays focused. Falls back to the
    # news channel, then the pulse channel.
    DISCORD_MACRO_CHANNEL_ID: int = 0
    # Forward catalyst calendar (earnings + scheduled macro). Splits off
    # #digest so the catalyst radar lives somewhere persistent and findable.
    # Falls back to the digest channel, then news, then pulse.
    DISCORD_CATALYSTS_CHANNEL_ID: int = 0

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    LLM_MODEL_LIGHT: str = "gemma4:e4b"
    LLM_MODEL_HEAVY: str = "qwen3:30b-a3b"

    # ── Serverless LLM (OpenAI-compatible: OpenRouter, Novita, Together,
    # Groq, DeepInfra, vLLM, OpenAI …). Set BASE + KEY once, then point
    # either or both tiers at a hosted model id. Leave a tier's model
    # blank to keep that tier on local Ollama. Routing BOTH tiers means
    # Ollama isn't needed at all (so the bot runs on a cheap CPU VPS).
    #   OpenRouter → https://openrouter.ai/api/v1
    #   Novita     → https://api.novita.ai/v3/openai
    LLM_API_BASE: str = ""
    LLM_API_KEY: str = ""
    LLM_API_MODEL_LIGHT: str = ""   # e.g. meta-llama/llama-3.1-8b-instruct
    LLM_API_MODEL_HEAVY: str = ""   # e.g. qwen/qwen3-30b-a3b

    # OpenRouter-only: pin which upstream provider (and optionally
    # quantization) serves each tier — "provider" or "provider/quant",
    # e.g. "deepinfra/fp8", "io-net/fp8". Ignored by direct providers
    # (DeepInfra/Novita) since the base URL already picks the host.
    LLM_API_PROVIDER_LIGHT: str = ""
    LLM_API_PROVIDER_HEAVY: str = ""

    # Reasoning-model control for the OpenRouter chat payload.
    #   "low"/"medium"/"high" → keep chain-of-thought ON for analytical
    #     calls (the trade/data brain + tool-selection).
    #   "off" → disable thinking entirely (loses the reasoning edge).
    # Default "medium" — and that's the empirically-measured sweet spot
    # on deepseek-v4-flash, not a guess. Across repeated live runs:
    #   low    → ~582 completion tok avg (noisy, spiked to 865)
    #   medium → ~446 tok avg (CHEAPEST + most stable + full answers)
    #   high   → ~523 tok avg
    # On this model "low" paradoxically rambles more, so medium is
    # cheaper AND a better reasoner — best on every axis.
    # Economy is enforced structurally, not via this knob: JSON
    # classifiers (sentiment/materiality/triage — the high-frequency
    # calls) are ALWAYS reasoning-off since CoT does nothing for a
    # mechanical tag, and a max_tokens floor (llm._REASONING_MIN_TOKENS)
    # guarantees think+answer fit so a reasoning call never truncates
    # (a truncated reasoning call is the worst waste — you pay for the
    # thinking and get nothing back).
    LLM_REASONING: str = "medium"

    # Per-million-token prices for the $ estimate on /system. Token COUNTS
    # are exact (from each response's usage block); these just turn them
    # into dollars. Set to deepseek-v4-flash's published rates. (Cache-read
    # is $0.022/M but we don't model the cache-hit fraction, so the input
    # figure is a conservative upper bound — real spend is a bit lower.)
    # 0 disables the $ figure (tokens still shown).
    LLM_PRICE_IN_PER_M: float = 0.112
    LLM_PRICE_OUT_PER_M: float = 0.224

    # Per-tier provider overrides — set these to put a tier on a DIFFERENT
    # provider than the shared one above (e.g. light on a free Google AI
    # Studio endpoint, heavy on Novita). An override wins over the shared
    # config for that tier. HEAVY_* also preserves the original heavy-only
    # API config, so existing setups keep working untouched.
    LIGHT_LLM_API_BASE: str = ""
    LIGHT_LLM_API_KEY: str = ""
    LIGHT_LLM_API_MODEL: str = ""
    HEAVY_LLM_API_BASE: str = ""
    HEAVY_LLM_API_KEY: str = ""
    HEAVY_LLM_API_MODEL: str = ""

    # Reddit ingestion is public-RSS only (no OAuth) — see ingesters/reddit.py.
    # This UA overrides the rotating browser pool when set.
    REDDIT_USER_AGENT: str = ""

    EDGAR_USER_AGENT: str = "sentinel/0.1 dev@example.com"

    # Cheap HTTP ingestion runs near-continuously — the bottleneck is the
    # local CPU LLM, not these polls, so they stay tight. Reasoning pipelines
    # (digest/macro/convergence) keep their long cadences in scheduler.py.
    POLL_FILINGS_MINUTES: int = 3
    POLL_REDDIT_MINUTES: int = 15
    POLL_HN_MINUTES: int = 30
    POLL_PRICES_MINUTES: int = 3
    POLL_NEWS_MINUTES: int = 5
    NEWS_ALERTS_MINUTES: int = 10
    DIGEST_HOUR_ET: int = 16
    DIGEST_MINUTE_ET: int = 30

    # Crypto-trending discovery (CoinGecko, free, no key).
    POLL_CRYPTO_TRENDING_MINUTES: int = 30

    # Synthesis "brain" cadence (hours). Heavy LLM — keep it well above the
    # ingestion cadences. 6h ≈ 4 connected reads a day.
    SYNTHESIS_HOURS: int = 6

    # Why-did-it-move scan + user-defined watch evaluation cadences (min).
    WHY_MOVED_MINUTES: int = 30
    WATCHES_MINUTES: int = 15
    # Catalyst radar — daily post hour (ET), just before the briefing.
    CATALYSTS_HOUR_ET: int = 7

    # Autonomous paper funds: per-fund starting cash + trade-cycle cadence.
    FUND_STARTING_CASH: float = 10_000.0
    FUNDS_CYCLE_MINUTES: int = 60

    # ── In-process cockpit (NiceGUI). Localhost-only by default: it exposes a
    # control surface (pause jobs / log calls), so it must not be reachable
    # off-box without a deliberate host change. ────────────────────────────
    DASHBOARD_ENABLED: bool = True
    DASHBOARD_HOST: str = "127.0.0.1"
    DASHBOARD_PORT: int = 8730

    # ── Fact-verification layer (verify.py). The one inviolable rule —
    # never fabricate a number — backed by a deterministic check: at the
    # call + post chokepoints, the hard ticker-bound figures the LLM emits
    # (price / 1d / 5d move / volume multiple / direction) are extracted and
    # compared against PriceContext ground truth. It ANNOTATES and FLAGS,
    # never blocks: a contradicted post still ships (with a ⚠ field), a
    # contradicted call is still recorded (grounded=False, conviction
    # floored). Fail-open everywhere — extraction unavailable or any error
    # leaves the item unverified, never dropped. ─────────────────────────
    VERIFY_ENABLED: bool = True
    # Only verify posts at/above this importance — low-importance noise isn't
    # worth a light-LLM extraction call. Calls are always verified when enabled.
    VERIFY_MIN_IMPORTANCE: int = 3
    # A stated price within this % of last_price is supported.
    VERIFY_PRICE_TOL_PCT: float = 2.0
    # A stated %-move within this many percentage points (OR 25% relative,
    # whichever is looser) of the actual move is supported.
    VERIFY_PCT_TOL_PP: float = 1.5
    # A stated volume-vs-20d multiple within this absolute band is supported.
    VERIFY_VOL_TOL: float = 0.5
    # Ground truth older than this many hours can't fairly verify a figure →
    # the claim is unverifiable (never contradicted). Generous enough to absorb
    # a weekend + holiday so we don't false-flag a Friday-close figure on Monday.
    VERIFY_CONTEXT_STALE_HOURS: float = 80.0
    # On a contradiction, floor the recorded call's conviction to 1 (it still
    # records — we just stop trusting a call built on a wrong number).
    VERIFY_FLOOR_CONVICTION_ON_CONTRADICTION: bool = True


settings = Settings()
