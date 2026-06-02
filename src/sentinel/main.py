"""Entry point.

Two modes:

- Live: `python -m sentinel.main`
  Boots LLM, builds watchlist, starts scheduler, runs Discord bot long-lived.

- Debug single-cycle: `python -m sentinel.main --run-once <job>`
  Boots LLM, builds watchlist, briefly connects Discord, runs one named cycle,
  exits.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from loguru import logger

from . import (
    chat,
    dashboard,
    discord_client,
    feedback,
    funds,
    health,
    interactions,
    scorecard,
    thesis,
)
from .config import settings
from .db import archive_database, init_db
from .edgar import watchlist_builder
from .ingesters import (
    crypto_micro,
    crypto_trending,
    hackernews,
    news,
    prices,
    reddit,
)
from .llm import get_llm
from .pipelines import (
    auto_exits,
    auto_research_pre_earnings,
    auto_thesis,
    book_risk,
    briefing,
    call_review,
    catalysts,
    convergence,
    digest,
    filings,
    game_plan,
    lounge,
    macro_themes,
    hot_movers,
    movers,
    news_alerts,
    news_impact,
    reddit_feed,
    risk_circuit,
    sentiment,
    social_pulse,
    synthesis,
    tuning,
    watches,
    why_moved,
)
from .scheduler import make_scheduler


_RUN_ONCE_REGISTRY = {
    "filings": filings.run_filings_cycle,
    "reddit": reddit.poll_reddit,
    "hackernews": hackernews.poll_hackernews,
    "prices": prices.poll_prices,
    "prices_daily": prices.poll_daily_bars,
    "prices_backfill": prices.backfill_history,
    "sentiment": sentiment.tag_recent_mentions,
    "social_pulse": social_pulse.run_social_pulse,
    "digest": digest.write_daily_digest,
    "tuning": tuning.run_monthly_tuning,
    "convergence": convergence.run_convergence_cycle,
    "movers": movers.run_movers_cycle,
    "hot_movers": hot_movers.run_hot_movers,
    "thesis_generate": thesis.run_generate_cycle,
    "thesis_review": thesis.run_review_cycle,
    "auto_thesis": auto_thesis.run_auto_thesis,
    "auto_exits": auto_exits.run_auto_exits,
    "risk_circuit": risk_circuit.run_risk_circuit,
    "auto_research_pre_earnings": auto_research_pre_earnings.run_auto_research_pre_earnings,
    "briefing": briefing.run_premarket_briefing,
    "news": news.poll_news,
    "macro_themes": macro_themes.run_macro_themes,
    "news_impact": news_impact.run_news_impact_tagging,
    "mark_calls": scorecard.run_mark_calls,
    "call_review": call_review.run_call_review,
    "book_risk": book_risk.run_book_risk,
    "health": health.run_health_post,
    "funds_cycle": funds.run_funds_cycle,
    "funds_digest": funds.run_funds_digest,
    "funds_meta": funds.run_funds_meta,
    "news_alerts": news_alerts.run_news_alerts,
    "crypto_trending": crypto_trending.poll_crypto_trending,
    "crypto_micro": crypto_micro.poll_crypto_micro,
    "synthesis": synthesis.run_synthesis_cycle,
    "why_moved": why_moved.run_why_moved_cycle,
    "watches": watches.run_watch_cycle,
    "catalysts": catalysts.run_catalyst_radar,
    "lounge": lounge.run_lounge_cycle,
    "game_plan": game_plan.run_game_plan_job,
    "reddit_feed": reddit_feed.run_reddit_feed,
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sentinel")
    p.add_argument(
        "--run-once",
        choices=sorted(_RUN_ONCE_REGISTRY.keys()),
        help="run a single named cycle then exit (debug)",
    )
    p.add_argument(
        "--skip-watchlist",
        action="store_true",
        help="skip watchlist build on startup (for testing only)",
    )
    p.add_argument(
        "--skip-llm",
        action="store_true",
        help="skip LLM startup verification (for testing only)",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="archive the current DB to data/backups/ and recreate an "
        "empty schema, then exit — run this (with the bot stopped) for a "
        "fresh start. Reversible: nothing is deleted.",
    )
    p.add_argument(
        "--preflight",
        action="store_true",
        help="run boot self-checks (DB schema, YAML configs, channel ids, "
        "LLM tiers reachable, dashboard port free, watchlist seeded) then "
        "exit. Exit code 0 if nothing critical failed; warnings never "
        "block. Wire this into your systemd unit's ExecStartPre to catch "
        "stale-config restarts before the scheduler arms.",
    )
    return p.parse_args()


async def _run_once(job_name: str) -> None:
    fn = _RUN_ONCE_REGISTRY[job_name]

    async def _do() -> None:
        try:
            await discord_client.post_meta(f"🔧 --run-once {job_name} starting")
        except Exception as e:
            logger.warning("could not post startup meta: {}", e)
        await fn()
        try:
            await discord_client.post_meta(f"✅ --run-once {job_name} done")
        except Exception:
            pass

    await discord_client.run_with_bot(_do)


async def _run_live() -> None:
    sched = make_scheduler()
    sched.start()

    bot = discord_client.get_bot()
    feedback.register_feedback_handlers(bot)
    chat.register_chat_handler(bot)
    interactions.register_actions(bot)

    # In-process cockpit on this same loop. Isolated: a mount failure logs
    # and returns None, the bot runs on regardless.
    dash_task = dashboard.mount(sched)

    # One-shot history backfill at boot so why_moved / movers / PriceContext
    # are correct immediately, not after weeks of bar accumulation. Off the
    # main path (to_thread internally) so it never blocks the bot.
    asyncio.create_task(prices.backfill_history())

    async def _on_ready_hook() -> None:
        try:
            await discord_client.post_meta(
                "🚀 sentinel online. scheduler started, jobs registered."
            )
        except Exception as e:
            logger.warning("could not post startup meta: {}", e)

    @bot.event
    async def on_ready() -> None:
        logger.info("Discord connected as {}", bot.user)
        await _on_ready_hook()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    bot_task = asyncio.create_task(bot.start(settings.DISCORD_TOKEN))
    # If the bot task crashes or exits early, unblock the main wait. Without
    # this, an early Discord shutdown leaves the process hanging.
    bot_task.add_done_callback(lambda _t: stop_event.set())

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down…")
        sched.shutdown(wait=False)
        if dash_task is not None:
            dash_task.cancel()
            try:
                await dash_task
            except (asyncio.CancelledError, Exception):
                pass
        await bot.close()
        try:
            await bot_task
        except Exception as e:
            logger.debug("bot_task ended with: {}", e)
        from .edgar.client import EdgarClient
        EdgarClient.close()


def main() -> int:
    args = _parse_args()

    if args.preflight:
        # Lazy import — preflight pulls a lot of the world (DB, LLM, httpx)
        # and we don't want every other code path paying for that import.
        from . import preflight
        results, code = preflight.run_all()
        preflight.print_report(results)
        return code

    if args.reset:
        backup = archive_database()
        init_db()  # recreates an empty schema + reseeds prompts/wallets
        if backup:
            logger.info("reset: previous DB archived → {}", backup)
            print(
                "✅ reset complete — previous data archived to:\n"
                f"   {backup}\n"
                "   (move it back to data/radar.db to restore.)\n"
                "   start the bot normally for a fresh run."
            )
        else:
            print(
                "✅ reset complete — no previous DB found; "
                "fresh schema created."
            )
        return 0

    init_db()

    if not args.skip_llm:
        try:
            get_llm()
        except Exception as e:
            logger.error("LLM init failed: {}", e)
            return 2

    if not args.skip_watchlist:
        watchlist_builder.build_watchlist()

    if args.run_once:
        asyncio.run(_run_once(args.run_once))
        return 0

    asyncio.run(_run_live())
    return 0


if __name__ == "__main__":
    sys.exit(main())
