"""Self-diagnostic contract.

The whole point is catching SILENT rot, so the detectors are pinned here:
silent-job (self-calibrating, no weekly false-alarms), dead-stream severity,
crypto staleness, and the ✅/⚠️/🔴 verdict assembly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel import health
from sentinel.db import session_scope
from sentinel.models import (
    Filing,
    JobRun,
    NewsItem,
    PriceBar,
    PriceContext,
    RedditMention,
    Watchlist,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 18, 18, 0, tzinfo=UTC)


def _job(s, job_id, ago_s, ok=True, err=None):
    s.add(JobRun(job_id=job_id, ran_at=NOW - timedelta(seconds=ago_s),
                 ok=ok, error=err))


# ── _silent_jobs ────────────────────────────────────────────────────────────


def test_silent_job_detection_is_self_calibrating():
    with session_scope() as s:
        # fast poller: 4 runs ~3min apart, but last was 3h ago → SILENT
        for k in range(4):
            _job(s, "fast", 10800 + k * 180)
        # healthy: 4 runs, last 2 min ago → fine
        for k in range(4):
            _job(s, "healthy", 120 + k * 180)
        # weekly: only 2 runs in 7d (one 7d ago) → too few to judge → skipped
        _job(s, "weekly", 600000)
        _job(s, "weekly", 60)
        # broken: 3 runs in last hour, all failed → FAILING
        for k in range(3):
            _job(s, "broken", 600 + k * 200, ok=False, err="boom")

    with session_scope() as s:
        flags = {f["job"]: f["kind"] for f in health._silent_jobs(s, NOW)}
    assert flags.get("fast") == "silent"
    assert flags.get("broken") == "failing"
    assert "healthy" not in flags
    assert "weekly" not in flags  # low-cadence must NOT false-alarm


# ── _stream_health ──────────────────────────────────────────────────────────


def test_dead_stream_severity():
    # Seed an old JobRun so _stream_health's bot-age gate is satisfied —
    # otherwise on a fresh DB we'd correctly suppress every flag (the
    # "no false-alarm immediately after reset" contract below).
    with session_scope() as s:
        _job(s, "prices_poll", 3600 * 4)  # 4h ago
        flags, streams = health._stream_health(s, NOW)
    sev = {f["stream"]: f["sev"] for f in flags}
    assert sev["bars"] == "critical"          # 24/7 feed dead = critical
    assert sev["filings"] == "warn"           # could be a quiet window
    assert streams == {"filings": 0, "news": 0, "reddit": 0, "bars": 0}

    with session_scope() as s:
        s.add(PriceBar(ticker="BTC-USD", ts=NOW - timedelta(hours=1),
                        open=1, high=1, low=1, close=1, volume=1))
    with session_scope() as s:
        flags, streams = health._stream_health(s, NOW)
    assert streams["bars"] == 1
    assert "bars" not in {f["stream"] for f in flags}


# ── _crypto_staleness ───────────────────────────────────────────────────────


def test_crypto_staleness_thresholds():
    with session_scope() as s:
        assert health._crypto_staleness(s, NOW) is None  # no crypto tracked
        s.add(Watchlist(cik="x", ticker="ETH-USD", source="crypto",
                         asset_class="crypto", added_at=NOW))
        s.add(PriceContext(ticker="ETH-USD", last_price=1.0, change_1d_pct=0.0,
                            change_5d_pct=0.0, volume_vs_20d_avg=1.0,
                            last_updated=NOW - timedelta(minutes=90)))
    with session_scope() as s:
        r = health._crypto_staleness(s, NOW)
    assert r["ticker"] == "ETH-USD" and r["sev"] == "warn"

    with session_scope() as s:
        pc = s.get(PriceContext, "ETH-USD")
        pc.last_updated = NOW - timedelta(minutes=240)
        s.add(pc)
    with session_scope() as s:
        assert health._crypto_staleness(s, NOW)["sev"] == "critical"

    with session_scope() as s:
        pc = s.get(PriceContext, "ETH-USD")
        pc.last_updated = NOW - timedelta(minutes=5)
        s.add(pc)
    with session_scope() as s:
        assert health._crypto_staleness(s, NOW)["sev"] is None


# ── verdict assembly ────────────────────────────────────────────────────────


def test_no_false_alarm_immediately_after_reset():
    # fresh DB → no JobRuns yet → bot age = 0 → stream-zero must be
    # suppressed (otherwise every `--reset` boots into a wall of red).
    txt = health.health_text()
    assert "ingest `bars` = 0" not in txt
    assert "🔴" not in txt


def test_verdict_critical_when_price_feed_dead_and_bot_is_old_enough():
    # seed a JobRun from over an hour ago so the bot has been "alive"
    # long enough to justify flagging — *now* zero bars must shout 🔴.
    with session_scope() as s:
        _job(s, "prices_poll", 3600 * 2)  # 2h ago
    txt = health.health_text()
    assert "🔴" in txt and "ingest `bars` = 0" in txt
    assert "all systems nominal" not in txt


def test_verdict_nominal_when_everything_flows():
    n = datetime.now(UTC)
    with session_scope() as s:
        _job(s, "prices_poll", 60)
        s.add(Filing(cik="1", ticker="AAA", form_type="8-K",
                      accession_number="acc1", filed_at=n,
                      primary_doc_url="u"))
        s.add(NewsItem(source="rss:x", external_id="e1", title="t", url="u",
                        published_at=n, fetched_at=n, is_macro=True))
        s.add(RedditMention(subreddit="s", post_id="p", ticker="AAA",
                             author="a", score=0, num_comments=0,
                             created_at=n, title="t", body_excerpt="",
                             permalink="u"))
        s.add(PriceBar(ticker="BTC-USD", ts=n, open=1, high=1, low=1,
                        close=1, volume=1))
        s.add(Watchlist(cik="x", ticker="BTC-USD", source="crypto",
                         asset_class="crypto", added_at=n))
        s.add(PriceContext(ticker="BTC-USD", last_price=1.0,
                            change_1d_pct=0.0, change_5d_pct=0.0,
                            volume_vs_20d_avg=1.0, last_updated=n))
    txt = health.health_text()
    assert "✅" in txt and "all systems nominal" in txt
    assert "🔴" not in txt and "⚠️" not in txt


def test_health_report_is_structured_twin_of_text():
    # bot has run for >3h on a still-empty DB → no bars/news/reddit/filings
    # in 24h → critical, same verdict shape as health_text(). The age seed
    # is what differentiates this from a fresh-reset boot.
    with session_scope() as s:
        _job(s, "prices_poll", 3600 * 4)  # 4h ago
    rep = health.health_report()
    assert rep["verdict"] == "crit" and rep["marker"] == "🔴"
    assert any("bars" in c for c in rep["critical"])
    # contract: every key the cockpit reads is present and the right type
    for k in ("as_of", "headline", "critical", "warnings", "jobs",
              "streams", "llm", "watchlist", "open_calls", "faded"):
        assert k in rep
    assert isinstance(rep["critical"], list)
    assert isinstance(rep["jobs"], list)
    assert set(rep["llm"]) >= {"calls", "errors", "rate"}
    # plain strings only — no Discord markdown leaks into the structured API
    blob = " ".join(rep["critical"] + rep["warnings"])
    assert "`" not in blob and "**" not in blob


def test_health_report_nominal_when_everything_flows():
    n = datetime.now(UTC)
    with session_scope() as s:
        _job(s, "prices_poll", 60)
        s.add(Filing(cik="1", ticker="AAA", form_type="8-K",
                      accession_number="acc1", filed_at=n,
                      primary_doc_url="u"))
        s.add(NewsItem(source="rss:x", external_id="e1", title="t", url="u",
                        published_at=n, fetched_at=n, is_macro=True))
        s.add(RedditMention(subreddit="s", post_id="p", ticker="AAA",
                             author="a", score=0, num_comments=0,
                             created_at=n, title="t", body_excerpt="",
                             permalink="u"))
        s.add(PriceBar(ticker="BTC-USD", ts=n, open=1, high=1, low=1,
                        close=1, volume=1))
        s.add(Watchlist(cik="x", ticker="BTC-USD", source="crypto",
                         asset_class="crypto", added_at=n))
        s.add(PriceContext(ticker="BTC-USD", last_price=1.0,
                            change_1d_pct=0.0, change_5d_pct=0.0,
                            volume_vs_20d_avg=1.0, last_updated=n))
    rep = health.health_report()
    assert rep["verdict"] == "ok" and rep["marker"] == "✅"
    assert not rep["critical"] and not rep["warnings"]
    # streams use a real-now 24h window, so the just-inserted rows count
    # (the job table keys off the fixture's fixed NOW and is clock-relative,
    # exactly as the sibling health_text() nominal test treats it)
    assert rep["streams"]["bars"] >= 1 and rep["streams"]["filings"] >= 1


def test_llm_stats_shape():
    from sentinel.llm import llm_stats

    s = llm_stats()
    assert {"calls", "errors"} <= set(s)
    assert isinstance(s["calls"], int) and isinstance(s["errors"], int)
