# Sentinel

Discord-resident autonomous trading-intelligence system. Watches SEC filings, Reddit, Hacker News, and prices across a self-managed watchlist. Scores material events with a local LLM (Gemma 4 via Ollama) and posts contextualized summaries to Discord.

Full specification in `docs/SPEC.md`.

## Setup

```bash
uv sync
cp .env.example .env
# fill .env with Discord token, channel IDs, Reddit creds, EDGAR user-agent
```

Make sure Ollama is running locally and both models are pulled:

```bash
ollama pull gemma4:e4b
ollama pull gemma4:31b
```

(The bot also calls `ollama.list()` on startup and pulls anything missing.)

## Running

Live mode (long-running bot + scheduler):

```bash
uv run python -m sentinel.main
```

Debug single-cycle mode:

```bash
uv run python -m sentinel.main --run-once filings
```

Run tests:

```bash
uv run pytest
```

## Status

**Phase 1**: filings pipeline with summarization only. Every filing posts to `#filings`. No enrichment, no scoring, no routing. Other ingesters and pipelines exist as stubs.
