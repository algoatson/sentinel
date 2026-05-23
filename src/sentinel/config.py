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


settings = Settings()
