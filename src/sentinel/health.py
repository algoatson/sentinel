"""Self-status heartbeat + daily diagnostic.

An APScheduler listener records every job execution into JobRun. `!health`
and the daily #meta post don't just summarize the last 24h — they actively
hunt the *silent* failure modes that bite an unattended bot:

- a job the scheduler dropped or that hangs (it simply stops appearing —
  detected by comparing each job's recent gap to its own 7d cadence, so
  weekly/monthly jobs don't false-alarm);
- a core ingest stream that went to zero (dead feed / blocked / stalled);
- crypto marks going stale (24/7 feed — staleness is unambiguous there);
- the LLM quietly failing a large share of completions;
- which sources auto-fade is currently dampening (the control loop, visible).

The report LEADS with a ✅ / ⚠️ / 🔴 verdict and the specific problems, so
"is it actually working?" is answered at a glance.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

import discord
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from loguru import logger
from sqlmodel import func, select

from . import discord_client
from .config import settings
from .db import session_scope
from .llm import llm_stats
from .models import (
    Filing,
    JobRun,
    NewsItem,
    PriceBar,
    PriceContext,
    RedditMention,
    TradingCall,
    Watchlist,
)

_PRUNE_DAYS = 7


def attach_listener(sched) -> None:
    """Wire JobRun recording into the scheduler. Best-effort: a failed
    insert must never take down a job."""

    def _on_event(event) -> None:
        try:
            with session_scope() as s:
                s.add(
                    JobRun(
                        job_id=event.job_id,
                        ran_at=datetime.now(timezone.utc),
                        ok=event.exception is None,
                        error=(
                            f"{type(event.exception).__name__}: {event.exception}"[:500]
                            if event.exception
                            else None
                        ),
                    )
                )
        except Exception as e:
            logger.debug("JobRun record failed for {}: {}", event.job_id, e)

    sched.add_listener(_on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    logger.info("health listener attached")


def _count(session, model, col, since) -> int:
    return session.exec(
        select(func.count()).select_from(model).where(col >= since)
    ).one()


# ── silent-rot detectors (pure; take session + now) ─────────────────────────


def _silent_jobs(session, now: datetime) -> list[dict]:
    """Jobs that went quiet or are chronically failing.

    Silent = a job whose gap since its last run exceeds 3× its own median
    inter-run gap over the last 7d (self-calibrating: a 3-min poller flags at
    ~10min, a weekly rebuild only after ~3 weeks — so low-cadence jobs never
    false-alarm). Failing = ran in the last 26h but every run errored.
    """
    week = now - timedelta(days=7)
    runs = session.exec(
        select(JobRun).where(JobRun.ran_at >= week).order_by(JobRun.ran_at)
    ).all()
    by_job: dict[str, list[JobRun]] = {}
    for r in runs:
        by_job.setdefault(r.job_id, []).append(r)

    out: list[dict] = []
    for job, rs in by_job.items():
        pairs = [
            (
                r,
                r.ran_at if r.ran_at.tzinfo
                else r.ran_at.replace(tzinfo=timezone.utc),
            )
            for r in rs
        ]
        ts = [t for _, t in pairs]
        last = max(ts)
        gap_since = (now - last).total_seconds()
        recent = [r for r, t in pairs if (now - t).total_seconds() <= 93600]
        if recent and all(not r.ok for r in recent):
            out.append({
                "job": job, "kind": "failing",
                "since_h": round(gap_since / 3600, 1),
                "err": next((r.error for r in reversed(recent) if r.error), ""),
            })
            continue
        if len(ts) < 3:
            continue  # too few runs to judge a cadence (weekly/monthly) — skip
        gaps = [
            (b - a).total_seconds()
            for a, b in zip(sorted(ts), sorted(ts)[1:])
        ]
        med = statistics.median(gaps)
        if gap_since > max(3 * med, 5400):  # 90-min floor for fast pollers
            out.append({
                "job": job, "kind": "silent",
                "since_h": round(gap_since / 3600, 1), "err": "",
            })
    return sorted(out, key=lambda d: -d["since_h"])


def _bot_age_hours(session, now: datetime) -> float:
    """Hours since the earliest JobRun on this DB — proxy for "how long
    has the bot been alive here." Returns 0.0 when nothing has run yet
    (fresh DB / just after `--reset`), so callers can suppress alerts
    that would otherwise false-alarm before any data has had a chance
    to land."""
    earliest = session.exec(select(func.min(JobRun.ran_at))).first()
    if earliest is None:
        return 0.0
    earliest = (
        earliest if earliest.tzinfo
        else earliest.replace(tzinfo=timezone.utc)
    )
    return max(0.0, (now - earliest).total_seconds() / 3600)


# Per-stream minimum bot age (hours) before "zero in 24h" can fire.
# Below this, a zero just means the bot hasn't been running long enough,
# not that the feed is dead. Tuned to each stream's natural cadence:
# bars/news poll fast; reddit batches every ~20m; filings cluster during
# business hours, so we wait longer.
_STREAM_MIN_AGE_H = {"bars": 0.5, "news": 0.5, "reddit": 1.0, "filings": 3.0}


def _stream_health(session, now: datetime) -> list[dict]:
    """Core ingest streams over 24h. Zero bars = CRITICAL (24/7 crypto
    means the price feed is dead, which corrupts everything downstream).
    Zero filings/news/reddit = WARNING (often just a quiet window/weekend —
    surface it, don't cry wolf). Gated by bot age so a fresh `--reset`
    doesn't get four red flags at boot."""
    since = now - timedelta(hours=24)
    naive = since.replace(tzinfo=None)
    streams = {
        "filings": _count(session, Filing, Filing.filed_at, since),
        "news": _count(session, NewsItem, NewsItem.fetched_at, since),
        "reddit": _count(session, RedditMention, RedditMention.created_at, naive),
        "bars": _count(session, PriceBar, PriceBar.ts, naive),
    }
    age_h = _bot_age_hours(session, now)
    flags = []
    for name, n in streams.items():
        if n == 0 and age_h >= _STREAM_MIN_AGE_H.get(name, 1.0):
            flags.append({
                "stream": name, "count": 0,
                "sev": "critical" if name == "bars" else "warn",
            })
    return flags, streams


def _crypto_staleness(session, now: datetime) -> dict | None:
    """Stalest crypto mark. Crypto is 24/7 so staleness is unambiguous
    (equities are correctly market-hours-gated, so we don't flag those)."""
    cryptos = {
        r.ticker for r in session.exec(
            select(Watchlist).where(Watchlist.asset_class == "crypto")
        ).all() if r.ticker
    }
    if not cryptos:
        return None
    worst = None
    for p in session.exec(select(PriceContext)).all():
        if p.ticker not in cryptos:
            continue
        u = p.last_updated
        u = u if u.tzinfo else u.replace(tzinfo=timezone.utc)
        age_m = (now - u).total_seconds() / 60
        if worst is None or age_m > worst[1]:
            worst = (p.ticker, age_m)
    if worst is None:
        return None
    ticker, age_m = worst
    sev = "critical" if age_m > 180 else "warn" if age_m > 60 else None
    return {"ticker": ticker, "age_m": round(age_m, 1), "sev": sev}


def _fade_status() -> list[str]:
    """Which call sources auto-fade is currently dampening — the control
    loop, made visible (informational, a sign it's working)."""
    from .scorecard import _fade_conviction, track_record_summary

    by_source = track_record_summary()["by_source"]
    out = []
    for src in sorted(by_source):
        _, note = _fade_conviction(src, 5, by_source)
        if note:
            out.append(note)
    return out


def health_text() -> str:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    with session_scope() as s:
        runs = s.exec(select(JobRun).where(JobRun.ran_at >= since)).all()
        by_job: dict[str, dict] = {}
        for r in runs:
            d = by_job.setdefault(
                r.job_id,
                {"n": 0, "fail": 0, "last": None, "last_ok": True, "err": None},
            )
            d["n"] += 1
            if not r.ok:
                d["fail"] += 1
            if d["last"] is None or r.ran_at > d["last"]:
                d["last"], d["last_ok"], d["err"] = r.ran_at, r.ok, r.error

        silent = _silent_jobs(s, now)
        stream_flags, streams = _stream_health(s, now)
        crypto = _crypto_staleness(s, now)
        wl = s.exec(select(func.count()).select_from(Watchlist)).one()
        calls_open = s.exec(
            select(func.count()).select_from(TradingCall)
            .where(TradingCall.settled == False)  # noqa: E712
        ).one()

    faded = _fade_status()
    ls = llm_stats()
    llm_rate = (ls["errors"] / ls["calls"]) if ls["calls"] else 0.0

    # ── verdict ──────────────────────────────────────────────────────────
    crit, warn = [], []
    for j in silent:
        (warn if j["kind"] == "silent" else crit).append(
            f"job `{j['job']}` {j['kind']} ({j['since_h']}h)"
            + (f" — _{j['err'][:70]}_" if j["err"] else "")
        )
    for f in stream_flags:
        msg = f"ingest `{f['stream']}` = 0 in 24h"
        (crit if f["sev"] == "critical" else warn).append(msg)
    if crypto and crypto["sev"]:
        msg = f"crypto marks stale ({crypto['ticker']} {crypto['age_m']:.0f}m)"
        (crit if crypto["sev"] == "critical" else warn).append(msg)
    if ls["calls"] >= 20 and llm_rate > 0.25:
        warn.append(
            f"LLM error rate {llm_rate * 100:.0f}% ({ls['errors']}/{ls['calls']})"
        )

    if crit:
        verdict = f"🔴 **{len(crit)} critical**" + (
            f", {len(warn)} warning" if warn else ""
        )
    elif warn:
        verdict = f"⚠️ **{len(warn)} warning(s)**"
    else:
        verdict = "✅ **all systems nominal**"

    lines = [
        f"**🩺 Health & diagnostics** — {now:%Y-%m-%d %H:%M}Z",
        verdict,
    ]
    for c in crit:
        lines.append(f"🔴 {c}")
    for w in warn:
        lines.append(f"⚠️ {w}")

    total = sum(d["n"] for d in by_job.values())
    fails = sum(d["fail"] for d in by_job.values())
    lines += [
        "",
        f"Jobs 24h: {total} runs / {len(by_job)} jobs · **{fails} failures**",
    ]
    for job in sorted(by_job):
        d = by_job[job]
        mark = "🟢" if d["last_ok"] else "🔴"
        line = f"{mark} `{job}` ×{d['n']}"
        if d["fail"]:
            line += f" · {d['fail']} fail"
        if not d["last_ok"] and d["err"]:
            line += f" · _{d['err'][:80]}_"
        lines.append(line)

    lines += [
        "",
        f"Ingest 24h: {streams['filings']} filings · {streams['news']} news · "
        f"{streams['reddit']} reddit · {streams['bars']} bars",
        f"LLM: {ls['calls']} calls / {ls['errors']} failed (since boot) · "
        f"watchlist {wl} · open calls {calls_open}",
    ]
    if faded:
        lines.append("Auto-fade active: " + " · ".join(faded))
    return "\n".join(lines)[:4000]


def health_report() -> dict:
    """Structured twin of `health_text()` for the cockpit.

    Same detectors, same verdict logic — but returns data, not a Discord
    markdown blob, so the UI can render proper components (status dots,
    alert rows, stat tiles) instead of dumping a string. `health_text()`
    is intentionally left untouched (its exact output is pinned by tests
    and shipped to Discord); this is an additive sibling, not a refactor.

    Plain strings only (no backticks/underscores) — presentation is the
    caller's job. Never raises: a failure degrades to an `error` verdict
    so the header chip and panel still render.
    """
    try:
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=24)

        with session_scope() as s:
            runs = s.exec(select(JobRun).where(JobRun.ran_at >= since)).all()
            by_job: dict[str, dict] = {}
            for r in runs:
                d = by_job.setdefault(
                    r.job_id,
                    {"n": 0, "fail": 0, "last": None, "ok": True, "err": None},
                )
                d["n"] += 1
                if not r.ok:
                    d["fail"] += 1
                if d["last"] is None or r.ran_at > d["last"]:
                    d["last"], d["ok"], d["err"] = r.ran_at, r.ok, r.error

            silent = _silent_jobs(s, now)
            stream_flags, streams = _stream_health(s, now)
            crypto = _crypto_staleness(s, now)
            wl = s.exec(select(func.count()).select_from(Watchlist)).one()
            calls_open = s.exec(
                select(func.count()).select_from(TradingCall)
                .where(TradingCall.settled == False)  # noqa: E712
            ).one()

        faded = _fade_status()
        ls = llm_stats()
        llm_rate = (ls["errors"] / ls["calls"]) if ls["calls"] else 0.0

        crit: list[str] = []
        warn: list[str] = []
        for j in silent:
            tail = f" — {j['err'][:80]}" if j["err"] else ""
            (warn if j["kind"] == "silent" else crit).append(
                f"job {j['job']} {j['kind']} ({j['since_h']}h){tail}"
            )
        for f in stream_flags:
            msg = f"no {f['stream']} ingested in 24h"
            (crit if f["sev"] == "critical" else warn).append(msg)
        if crypto and crypto["sev"]:
            msg = (
                f"crypto marks stale ({crypto['ticker']} "
                f"{crypto['age_m']:.0f}m old)"
            )
            (crit if crypto["sev"] == "critical" else warn).append(msg)
        if ls["calls"] >= 20 and llm_rate > 0.25:
            warn.append(
                f"LLM error rate {llm_rate * 100:.0f}% "
                f"({ls['errors']}/{ls['calls']})"
            )

        if crit:
            verdict, marker = "crit", "🔴"
            headline = f"{len(crit)} critical" + (
                f", {len(warn)} warning" if warn else ""
            )
        elif warn:
            verdict, marker = "warn", "⚠️"
            headline = f"{len(warn)} warning(s)"
        else:
            verdict, marker = "ok", "✅"
            headline = "all systems nominal"

        jobs = [
            {
                "id": jid,
                "runs": d["n"],
                "fail": d["fail"],
                "ok": d["ok"],
                "err": d["err"],
            }
            for jid, d in sorted(by_job.items())
        ]
        return {
            "as_of": now,
            "verdict": verdict,
            "marker": marker,
            "headline": headline,
            "critical": crit,
            "warnings": warn,
            "jobs": jobs,
            "jobs_runs": sum(d["n"] for d in by_job.values()),
            "jobs_fail": sum(d["fail"] for d in by_job.values()),
            "jobs_n": len(by_job),
            "streams": streams,
            "llm": {
                "calls": ls["calls"],
                "errors": ls["errors"],
                "rate": round(llm_rate * 100, 1),
            },
            "watchlist": int(wl),
            "open_calls": int(calls_open),
            "faded": faded,
        }
    except Exception as e:  # never break the header chip / panel
        logger.debug("health_report failed: {}", e)
        return {
            "as_of": datetime.now(timezone.utc),
            "verdict": "error",
            "marker": "•",
            "headline": f"diagnostics unavailable ({type(e).__name__})",
            "critical": [],
            "warnings": [],
            "jobs": [],
            "jobs_runs": 0,
            "jobs_fail": 0,
            "jobs_n": 0,
            "streams": {},
            "llm": {"calls": 0, "errors": 0, "rate": 0.0},
            "watchlist": 0,
            "open_calls": 0,
            "faded": [],
        }


def _prune() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=_PRUNE_DAYS)
    with session_scope() as s:
        for r in s.exec(select(JobRun).where(JobRun.ran_at < cutoff)).all():
            s.delete(r)


async def run_health_post() -> None:
    try:
        import asyncio

        await asyncio.to_thread(_prune)
        text = await asyncio.to_thread(health_text)
        embed = discord.Embed(
            title="🩺 Daily health & diagnostics",
            description=text,
            color=0x95A5A6,
        )
        await discord_client.post_embed(
            settings.DISCORD_META_CHANNEL_ID, embed,
            with_actions=False, importance=2,
        )
    except Exception as e:
        logger.exception("run_health_post failure: {}", e)
