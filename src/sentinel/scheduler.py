"""APScheduler wiring — the orchestration spine.

Every ingester/pipeline `run_*` entrypoint registers here exactly once, and
each is mirrored in main._RUN_ONCE_REGISTRY for single-cycle debugging. The
sole exception is the weekly watchlist rebuild: it's a sync bootstrap step
that main() already runs ahead of every cycle (gated by --skip-watchlist),
not an async job dispatched through the run-once harness.
Interval jobs share `_COMMON` (misfire grace, single-instance, coalesce);
time-of-day jobs use ET via stdlib zoneinfo. Job IDs are stable so a restart
re-registers cleanly.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from . import funds, health, scorecard, thesis
from .config import settings
from .edgar import watchlist_builder
from .ingesters import (
    crypto_micro,
    crypto_trending,
    hackernews,
    news,
    prices,
    reddit,
)
from .pipelines import (
    book_risk,
    briefing,
    call_review,
    catalysts,
    convergence,
    digest,
    filings,
    hot_movers,
    lounge,
    macro_themes,
    movers,
    news_alerts,
    news_impact,
    reddit_feed,
    sentiment,
    social_pulse,
    synthesis,
    tuning,
    watches,
    why_moved,
)


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

_COMMON = {"misfire_grace_time": 120, "max_instances": 1, "coalesce": True}

# Spread interval pollers by ±_JITTER seconds so they don't all fire on the
# same aligned tick and thunder-herd the single SQLite writer — which on a
# slow disk (e.g. a Raspberry Pi SD card) blows past busy_timeout and raises
# "database is locked". Cron (time-of-day) jobs keep their exact times.
_JITTER = 45


def _every(**kwargs) -> IntervalTrigger:
    return IntervalTrigger(jitter=_JITTER, **kwargs)


def make_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=UTC)

    sched.add_job(
        filings.run_filings_cycle,
        _every(minutes=settings.POLL_FILINGS_MINUTES),
        id="filings_cycle",
        **_COMMON,
    )
    sched.add_job(
        reddit.poll_reddit,
        _every(minutes=settings.POLL_REDDIT_MINUTES),
        id="reddit_poll",
        **_COMMON,
    )
    sched.add_job(
        hackernews.poll_hackernews,
        _every(minutes=settings.POLL_HN_MINUTES),
        id="hn_poll",
        **_COMMON,
    )
    sched.add_job(
        prices.poll_prices,
        _every(minutes=settings.POLL_PRICES_MINUTES),
        id="prices_poll",
        **_COMMON,
    )
    sched.add_job(
        prices.poll_daily_bars,
        CronTrigger(hour=17, minute=0, timezone=ET),
        id="prices_daily",
        **_COMMON,
    )
    sched.add_job(
        prices.backfill_history,
        _every(hours=6),
        id="prices_backfill",
        **_COMMON,
    )
    sched.add_job(
        sentiment.tag_recent_mentions,
        _every(hours=1),
        id="sentiment_tag",
        **_COMMON,
    )
    sched.add_job(
        social_pulse.run_social_pulse,
        _every(hours=1),
        id="social_pulse",
        **_COMMON,
    )
    sched.add_job(
        digest.write_daily_digest,
        CronTrigger(
            hour=settings.DIGEST_HOUR_ET,
            minute=settings.DIGEST_MINUTE_ET,
            timezone=ET,
        ),
        id="daily_digest",
        **_COMMON,
    )
    sched.add_job(
        watchlist_builder.build_watchlist,
        CronTrigger(day_of_week="sun", hour=6, minute=0, timezone=UTC),
        id="watchlist_rebuild",
        **_COMMON,
    )
    sched.add_job(
        tuning.run_monthly_tuning,
        CronTrigger(day=1, hour=12, minute=0, timezone=UTC),
        id="monthly_tuning",
        **_COMMON,
    )
    sched.add_job(
        convergence.run_convergence_cycle,
        _every(minutes=30),
        id="convergence",
        **_COMMON,
    )
    sched.add_job(
        movers.run_movers_cycle,
        CronTrigger(hour=16, minute=15, timezone=ET),
        id="movers_daily",
        **_COMMON,
    )
    # Hot-movers — terse "what's moving NOW on the watchlist" feed for #hot.
    # 15 min cadence + 4h per-ticker cooldown keeps the channel readable.
    # The pipeline itself no-ops when DISCORD_HOT_CHANNEL_ID isn't set and
    # gates on US market hours (crypto names exempted, 24/7).
    sched.add_job(
        hot_movers.run_hot_movers,
        _every(minutes=15),
        id="hot_movers",
        **_COMMON,
    )
    sched.add_job(
        briefing.run_premarket_briefing,
        CronTrigger(hour=8, minute=30, timezone=ET),
        id="premarket_briefing",
        **_COMMON,
    )
    sched.add_job(
        news.poll_news,
        _every(minutes=settings.POLL_NEWS_MINUTES),
        id="news_poll",
        **_COMMON,
    )
    sched.add_job(
        crypto_trending.poll_crypto_trending,
        _every(minutes=settings.POLL_CRYPTO_TRENDING_MINUTES),
        id="crypto_trending",
        **_COMMON,
    )
    sched.add_job(
        crypto_micro.poll_crypto_micro,
        _every(minutes=20),
        id="crypto_micro",
        **_COMMON,
    )
    sched.add_job(
        macro_themes.run_macro_themes,
        _every(hours=4),
        id="macro_themes",
        **_COMMON,
    )
    sched.add_job(
        news_impact.run_news_impact_tagging,
        _every(hours=1),
        id="news_impact_tag",
        **_COMMON,
    )
    sched.add_job(
        scorecard.run_mark_calls,
        _every(hours=2),
        id="mark_calls",
        **_COMMON,
    )
    # Visible accountability — posts the verdict on each notable call once its
    # 5d horizon matures (runs just behind mark_calls; self-gating + idempotent
    # via resolved_posted_at, so cadence/ordering aren't load-bearing).
    sched.add_job(
        call_review.run_call_review,
        _every(hours=2),
        id="call_review",
        **_COMMON,
    )
    sched.add_job(
        news_alerts.run_news_alerts,
        _every(minutes=settings.NEWS_ALERTS_MINUTES),
        id="news_alerts",
        **_COMMON,
    )
    sched.add_job(
        synthesis.run_synthesis_cycle,
        _every(hours=settings.SYNTHESIS_HOURS),
        id="synthesis",
        **_COMMON,
    )
    sched.add_job(
        why_moved.run_why_moved_cycle,
        _every(minutes=settings.WHY_MOVED_MINUTES),
        id="why_moved",
        **_COMMON,
    )
    sched.add_job(
        watches.run_watch_cycle,
        _every(minutes=settings.WATCHES_MINUTES),
        id="watches",
        **_COMMON,
    )
    # Proactive book-risk — watches open paper positions, speaks only when
    # one is in trouble (self-gating + cooldown, so cadence just bounds
    # latency, not post frequency).
    sched.add_job(
        book_risk.run_book_risk,
        _every(minutes=30),
        id="book_risk",
        **_COMMON,
    )
    sched.add_job(
        catalysts.run_catalyst_radar,
        CronTrigger(hour=settings.CATALYSTS_HOUR_ET, minute=0, timezone=ET),
        id="catalyst_radar",
        **_COMMON,
    )
    sched.add_job(
        health.run_health_post,
        CronTrigger(hour=8, minute=0, timezone=ET),
        id="health_post",
        **_COMMON,
    )
    sched.add_job(
        funds.run_funds_cycle,
        _every(minutes=settings.FUNDS_CYCLE_MINUTES),
        id="funds_cycle",
        **_COMMON,
    )
    sched.add_job(
        funds.run_funds_digest,
        CronTrigger(hour=16, minute=45, timezone=ET),
        id="funds_digest",
        **_COMMON,
    )
    # Weekly edge readout — needs accumulated closed trades to mean anything,
    # so it runs once a week (Sunday), not daily.
    sched.add_job(
        funds.run_funds_meta,
        CronTrigger(day_of_week="sun", hour=12, minute=0, timezone=ET),
        id="funds_meta",
        **_COMMON,
    )
    # Thesis engine.
    # `generate_cycle`: heavy LLM proposes new running theses (+ closes
    # no-longer-valid ones) once a day, just before market open ET so
    # the day's headlines feed into yesterday's mental model. Bounded
    # to 12 active theses; quality > quantity.
    sched.add_job(
        thesis.run_generate_cycle,
        CronTrigger(hour=8, minute=15, timezone=ET),
        id="thesis_generate",
        **_COMMON,
    )
    # `review_cycle`: pure-rules sweep that closes theses on target hit,
    # decisive challenge accumulation, or horizon-elapsed. Cheap; runs
    # after market close so end-of-day price action is reflected.
    sched.add_job(
        thesis.run_review_cycle,
        CronTrigger(hour=17, minute=10, timezone=ET),
        id="thesis_review",
        **_COMMON,
    )
    # Dedicated Reddit-stream channel — notable r/ posts (moving/surging
    # tickers only). Self-gating (skips when channel unset or nothing notable).
    sched.add_job(
        reddit_feed.run_reddit_feed,
        _every(minutes=20),
        id="reddit_feed",
        **_COMMON,
    )
    # The Lounge — relaxed #general posts, mid-morning + late-afternoon ET.
    # Self-gating (prompt returns SKIP when there's nothing worth saying).
    sched.add_job(
        lounge.run_lounge_cycle,
        CronTrigger(hour=11, minute=20, timezone=ET),
        id="lounge_am",
        **_COMMON,
    )
    sched.add_job(
        lounge.run_lounge_cycle,
        CronTrigger(hour=17, minute=20, timezone=ET),
        id="lounge_pm",
        **_COMMON,
    )

    health.attach_listener(sched)

    for job in sched.get_jobs():
        logger.info("scheduled job: {} (trigger: {})", job.id, job.trigger)

    return sched
