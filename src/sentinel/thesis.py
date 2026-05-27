"""Cross-pollination thesis engine.

Bot maintains running theses (hypotheses) about tickers it cares about.
Each thesis has a direction, a target, a horizon, and an invalidation
condition. As news/filings arrive, the linker tags them against active
theses — `supports`, `challenges`, or `neutral`. The aggregates feed a
periodic review: persistent challenges invalidate; the target/horizon
mark validated or matured.

Three moving parts:

- **`generate_cycle()`** — daily-ish: heavy LLM reads the open paper
  positions, last 14d of calls, recent material filings + breaking
  news, plus the currently-active theses; outputs new theses to open
  AND existing theses to close. Bounded so we don't accumulate noise.
- **`link_news(news_id)` / `link_filing(filing_id)`** — called by
  the news / filings pipelines after each new insert. Pure-rule
  judgement on impact (sentiment-aligned with thesis direction =
  supports; opposite = challenges; missing sentiment = neutral). Fast,
  no LLM. ThesisEvent persisted, thesis aggregate counters bumped.
- **`review_cycle()`** — daily: walks active theses, checks state
  transitions (target hit, horizon elapsed, challenge count exceeds
  threshold) and closes accordingly.

Why this is the cornerstone feature: the bot graduates from "stream of
disconnected reads" to "ongoing analysis with memory". The user's
exact request — "the bot should make links between data and come up
with theories that it can try and verify" — is what this delivers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from string import Template

from loguru import logger
from sqlmodel import select

from .db import session_scope
from .llm import LLM_ERROR_SENTINEL, get_llm
from .models import (
    Filing,
    FundTrade,
    NewsItem,
    PaperTrade,
    PriceBar,
    PriceContext,
    Thesis,
    ThesisEvent,
    TradingCall,
)


# ── tuning constants ──────────────────────────────────────────────────────

# Hard cap on simultaneously-active theses. Past this the generator is
# told to prune before adding more — we don't want a wall of stale
# hypotheses competing for the user's attention. 12 = enough to cover
# the major positions + a few sector themes.
_MAX_ACTIVE = 12

# Challenge ratio at which a thesis auto-invalidates in `review_cycle`.
# 3 challenges per 1 support = "the data has decisively turned against
# this read". Tuned to need at least a handful of events before tripping
# so a single bad headline doesn't kill an otherwise-warm thesis.
_INVALIDATE_CHALLENGE_RATIO = 3.0
_INVALIDATE_MIN_EVENTS = 4
# Percent-move-against-direction that auto-invalidates a thesis on the
# next review_cycle. Matches the default `invalidation_criteria` text
# auto_thesis writes (">5% against the thesis for 2 sessions") with
# some slack for intraday noise — we don't track session-by-session
# closes, so a single ≥10% adverse mark is treated as definitive.
_INVALIDATE_ADVERSE_PCT = 0.10
# How far back to look for the price-at-creation bar. Theses generated
# midweek with a Friday review on a fresh ticker should still find a
# pre-creation bar within this window.
_PRICE_LOOKUP_WINDOW_DAYS = 14

# Below this many days from target, we ask the LLM to consider whether
# the thesis is on track — used by the review prompt context.
_HORIZON_REVIEW_WINDOW_DAYS = 5


# ── LLM prompt ────────────────────────────────────────────────────────────

_GENERATE_PROMPT = Template("""\
You are the thesis desk. You maintain a small set of RUNNING THESES
about specific tickers — the bot's mental model of "what's actually
happening here, and what would prove me wrong".

Inputs (factual, recent):
$context_json

Currently-active theses (do NOT duplicate these; you may propose
CLOSING any that no longer hold):
$active_theses_json

OUTPUT — strict JSON, no markdown fences, no prose before/after:

{
  "open": [
    {
      "ticker": "NVDA",                 // single ticker; "MACRO" for cross-asset
      "direction": "long|short|neutral",
      "title": "1 line — the thesis in plain English",
      "body": "2-4 sentences — the reasoning, drawing on specific data points from above",
      "invalidation_criteria": "1-2 sentences — what would prove this wrong (specific, not generic)",
      "conviction": 1-5,
      "target_price": null | 250.0,    // realistic, defended
      "horizon_days": null | 30        // expected hold horizon
    }
  ],
  "close": [
    { "id": 7, "reason": "specific 1-line rationale" }
  ]
}

Rules:
- Limit `open` to 1-5 NEW theses per cycle. Quality > quantity.
- Don't propose a thesis on a ticker that already has an active one
  unless the existing one needs replacing — in that case put it in
  `close` AND propose the new one.
- "MACRO" theses (Fed regime, tariff escalation, etc.) are fine but
  should be rare and substantial.
- Invalidation criteria must name a specific observable trigger.
  "Macro shifts" is too vague; "10y yield breaks above 4.7%" is fine.
- If you have nothing material to add, return empty arrays — the bot
  prefers a quiet desk to a noisy one.
""")


# ── helpers ───────────────────────────────────────────────────────────────


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso(t: datetime | None) -> str | None:
    a = _aware(t)
    return a.isoformat() if a else None


def _model_name() -> str:
    from .config import settings
    return (
        settings.LLM_API_MODEL_HEAVY
        or settings.HEAVY_LLM_API_MODEL
        or settings.LLM_MODEL_HEAVY
        or "unknown"
    )[:120]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── accessors ─────────────────────────────────────────────────────────────


def list_active() -> list[dict]:
    """Active theses, newest first. The dashboard Theses tab reads
    this every refresh — kept structural so the renderer doesn't have
    to know about SQLModel."""
    with session_scope() as s:
        rows = s.exec(
            select(Thesis)
            .where(Thesis.state == "active")
            .order_by(Thesis.created_at.desc())
        ).all()
        return [_thesis_dict(t) for t in rows]


def list_recent_closed(days: int = 30) -> list[dict]:
    """Closed/validated/invalidated/matured in the window — for the
    "graveyard" / "wins" sections of the dashboard."""
    cutoff = (_now() - timedelta(days=days)).replace(tzinfo=None)
    with session_scope() as s:
        rows = s.exec(
            select(Thesis)
            .where(Thesis.state != "active")
            .where(Thesis.closed_at >= cutoff)
            .order_by(Thesis.closed_at.desc())
        ).all()
        return [_thesis_dict(t) for t in rows]


def get_thesis(thesis_id: int) -> dict | None:
    with session_scope() as s:
        t = s.get(Thesis, thesis_id)
        if t is None:
            return None
        d = _thesis_dict(t)
        events = s.exec(
            select(ThesisEvent)
            .where(ThesisEvent.thesis_id == thesis_id)
            .order_by(ThesisEvent.created_at.desc())
            .limit(80)
        ).all()
        d["events"] = [_event_dict(e) for e in events]
        return d


def close_thesis(thesis_id: int, *, state: str, reason: str) -> bool:
    """User/system action: close the thesis with a final state and
    reason. `state` must be one of validated/invalidated/matured/closed.
    Returns False if the thesis doesn't exist or is already non-active."""
    if state not in ("validated", "invalidated", "matured", "closed"):
        return False
    with session_scope() as s:
        t = s.get(Thesis, thesis_id)
        if t is None or t.state != "active":
            return False
        now = _now()
        t.state = state
        t.closed_at = now
        t.close_reason = (reason or "")[:400]
        t.updated_at = now
        s.add(t)
    return True


def _thesis_dict(t: Thesis) -> dict:
    return {
        "id": t.id,
        "ticker": t.ticker,
        "direction": t.direction,
        "title": t.title,
        "body": t.body,
        "invalidation_criteria": t.invalidation_criteria,
        "conviction": t.conviction,
        "target_price": t.target_price,
        "horizon_days": t.horizon_days,
        "state": t.state,
        "source_event": t.source_event,
        "model": t.model,
        "created_at": _iso(t.created_at),
        "updated_at": _iso(t.updated_at),
        "closed_at": _iso(t.closed_at),
        "close_reason": t.close_reason,
        "supporting_events": t.supporting_events,
        "challenging_events": t.challenging_events,
        "last_event_at": _iso(t.last_event_at),
    }


def _event_dict(e: ThesisEvent) -> dict:
    return {
        "id": e.id,
        "thesis_id": e.thesis_id,
        "kind": e.kind,
        "ref_table": e.ref_table,
        "ref_id": e.ref_id,
        "description": e.description,
        "impact": e.impact,
        "rationale": e.rationale,
        "created_at": _iso(e.created_at),
    }


# ── linker (cheap, rule-based; called from ingestion paths) ───────────────


def _impact_for_news(news: NewsItem, thesis: Thesis) -> tuple[str, str]:
    """Sentiment-aligned heuristic: if news sentiment matches the
    thesis direction, the impact is `supports`; opposite → challenges;
    untagged / neutral sentiment → neutral.

    Returns (impact, rationale). The rationale is a one-line human
    explanation that surfaces in the timeline modal."""
    sent = news.sentiment or 0
    if sent == 0:
        return ("neutral", "covers ticker but sentiment untagged or neutral")
    bullish = sent > 0
    if thesis.direction == "long":
        return (
            ("supports", "bullish news on the long thesis")
            if bullish
            else ("challenges", "bearish news against the long thesis")
        )
    if thesis.direction == "short":
        return (
            ("challenges", "bullish news against the short thesis")
            if bullish
            else ("supports", "bearish news on the short thesis")
        )
    return ("neutral", "tagged news on a neutral-direction thesis")


def _impact_for_filing(filing: Filing, thesis: Thesis) -> tuple[str, str]:
    """Materiality + filing-type heuristic on the actual 0-3
    materiality scale (was 0-10 before a scorer refactor; the old
    >=7 / >=4 branches were unreachable and every filing tagged
    'neutral' → filings never moved the supports/challenges ratio).

    Directional inference (cheap, no LLM):
      * Form 4 BUY / "purchase" / "acquired" → bullish (supports long,
        challenges short).
      * Form 4 SELL / "disposed" → bearish (challenges long, supports
        short). Routine 10b5-1 plan sales are tagged in the summary
        and treated as neutral (the prefix is the existing convention
        the summariser uses).
      * 10-K / 10-Q / 8-K with score >= 2 and a clear "beat" / "raised
        guidance" tone → bullish; "miss" / "cut guidance" / "going
        concern" → bearish.
      * Everything else stays neutral with a tiered rationale —
        materiality 3 = "material", 2 = "notable", else "routine"."""
    form = (filing.form_type or "").upper()
    score = filing.materiality_score or 0
    summary = (filing.summary or "").lower()

    # ── Form 4 directional inference ─────────────────────────────────
    if form.startswith("4"):
        is_buy = any(t in summary for t in (
            "purchase", "purchased", "buy ", "acquired", "open market buy",
        ))
        is_sell = any(t in summary for t in (
            "sale", "sold", "disposed", "sell ",
        ))
        is_planned = "10b5-1" in summary or "rule 10b5" in summary
        if is_planned:
            return ("neutral", "Form 4 10b5-1 planned trade — scheduled, not directional")
        if is_buy and not is_sell:
            if thesis.direction == "long":
                return ("supports", "insider purchase aligns with the long thesis")
            if thesis.direction == "short":
                return ("challenges", "insider purchase against the short thesis")
        if is_sell and not is_buy:
            if thesis.direction == "long":
                return ("challenges", "insider sale against the long thesis")
            if thesis.direction == "short":
                return ("supports", "insider sale aligns with the short thesis")
        return ("neutral", "Form 4 — direction unclear from summary")

    # ── 8-K / 10-Q / 10-K directional sniff ──────────────────────────
    if score >= 2 and form in ("8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A"):
        bullish_hits = sum(
            1 for t in (
                "beat", "raised guidance", "raise guidance",
                "exceeded", "above consensus", "record revenue", "buyback",
                "dividend increase", "acquisition target",
            )
            if t in summary
        )
        bearish_hits = sum(
            1 for t in (
                "miss", "missed", "cut guidance", "below consensus",
                "going concern", "restatement", "restated", "fraud",
                "investigation", "subpoena", "dilut", "secondary offering",
            )
            if t in summary
        )
        if bullish_hits > bearish_hits:
            if thesis.direction == "long":
                return ("supports", f"{form} reads bullish — supports the long thesis")
            if thesis.direction == "short":
                return ("challenges", f"{form} reads bullish — challenges the short thesis")
        elif bearish_hits > bullish_hits:
            if thesis.direction == "long":
                return ("challenges", f"{form} reads bearish — challenges the long thesis")
            if thesis.direction == "short":
                return ("supports", f"{form} reads bearish — supports the short thesis")

    # ── Tiered neutral fallback ─────────────────────────────────────
    if score >= 3:
        return ("neutral", f"material {form} (score 3/3) — direction unclear")
    if score >= 2:
        return ("neutral", f"notable {form} (score 2/3) — direction unclear")
    return ("neutral", f"{form} (routine)")


def link_news(news_id: int) -> int:
    """Match a new NewsItem against active theses for EVERY ticker it
    tags. Persists one ThesisEvent per (thesis, ticker) match + bumps
    the thesis aggregates. Returns the total number of theses linked.

    Multi-ticker aware: a story tagging both NVDA and AMD will
    populate the timeline of any active thesis on either ticker. The
    `tickers_csv` column carries the full set; falls back to the
    single `ticker` for legacy rows that pre-date the migration.

    Cheap; safe to call repeatedly — duplicate (thesis_id, ref_table,
    ref_id) tuples are de-duped at insert time."""
    if news_id is None:
        return 0
    try:
        from .utils import parse_tickers_csv
        with session_scope() as s:
            news = s.get(NewsItem, news_id)
            if news is None:
                return 0
            tickers = parse_tickers_csv(news.tickers_csv) or (
                [news.ticker] if news.ticker else []
            )
            if not tickers:
                return 0
            total = 0
            for t in tickers:
                total += _link_inner(
                    s, kind="news", ref_table="newsitem", ref_id=news.id,
                    ticker=t,
                    description=(news.title or "")[:500],
                    impact_for=lambda thesis: _impact_for_news(news, thesis),
                )
            return total
    except Exception as e:
        logger.warning("link_news({}) failed: {}", news_id, e)
        return 0


def link_filing(filing_id: int) -> int:
    """Same shape as `link_news` for SEC filings."""
    if filing_id is None:
        return 0
    try:
        with session_scope() as s:
            filing = s.get(Filing, filing_id)
            if filing is None or not filing.ticker:
                return 0
            description = (
                (filing.summary or filing.form_type or "")[:500]
            )
            return _link_inner(
                s, kind="filing", ref_table="filing", ref_id=filing.id,
                ticker=filing.ticker, description=description,
                impact_for=lambda thesis: _impact_for_filing(filing, thesis),
            )
    except Exception as e:
        logger.warning("link_filing({}) failed: {}", filing_id, e)
        return 0


def _link_inner(
    session, *, kind: str, ref_table: str, ref_id: int, ticker: str,
    description: str, impact_for,
) -> int:
    """Shared linker implementation. Caller pre-loads the row and
    passes an `impact_for(thesis)` callback that returns the per-thesis
    (impact, rationale) judgement."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return 0
    active = session.exec(
        select(Thesis)
        .where(Thesis.state == "active")
        .where(Thesis.ticker == ticker)
    ).all()
    if not active:
        return 0
    now = _now()
    linked = 0
    for t in active:
        # Dedup: don't double-link the same source row to the same thesis
        existing = session.exec(
            select(ThesisEvent)
            .where(ThesisEvent.thesis_id == t.id)
            .where(ThesisEvent.ref_table == ref_table)
            .where(ThesisEvent.ref_id == ref_id)
        ).first()
        if existing is not None:
            continue
        impact, rationale = impact_for(t)
        session.add(ThesisEvent(
            thesis_id=t.id,
            kind=kind, ref_table=ref_table, ref_id=ref_id,
            description=description, impact=impact,
            rationale=rationale, created_at=now,
        ))
        # Update aggregates
        if impact == "supports":
            t.supporting_events += 1
        elif impact == "challenges":
            t.challenging_events += 1
        t.last_event_at = now
        t.updated_at = now
        session.add(t)
        linked += 1
    return linked


# ── generator (LLM-driven, daily-ish) ─────────────────────────────────────


def _build_generator_context() -> dict:
    """Snapshot the bot's recent state for the generator prompt. Caps
    everything so we don't blow the token budget."""
    now = _now()
    cutoff_calls = (now - timedelta(days=14)).replace(tzinfo=None)
    cutoff_filings = (now - timedelta(days=7)).replace(tzinfo=None)
    cutoff_news = (now - timedelta(hours=24)).replace(tzinfo=None)
    with session_scope() as s:
        # Open paper positions across all wallets
        paper_opens = s.exec(
            select(PaperTrade).where(PaperTrade.status == "open")
        ).all()
        fund_opens = s.exec(
            select(FundTrade).where(FundTrade.status == "open")
        ).all()
        # Recent calls (high-conviction only — conv ≥ 4)
        calls = s.exec(
            select(TradingCall)
            .where(TradingCall.created_at >= cutoff_calls)
            .where(TradingCall.conviction >= 4)
            .order_by(TradingCall.created_at.desc())
            .limit(12)
        ).all()
        # Material filings
        filings = s.exec(
            select(Filing)
            .where(Filing.filed_at >= cutoff_filings)
            .where(Filing.materiality_score.is_not(None))
            .order_by(Filing.materiality_score.desc())
            .limit(10)
        ).all()
        # Breaking news (with sentiment tagged)
        news = s.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff_news)
            .where(NewsItem.sentiment.is_not(None))
            .order_by(NewsItem.published_at.desc())
            .limit(15)
        ).all()
        # Price context for tickers we already trade
        tickers_of_interest = {
            *(p.ticker for p in paper_opens),
            *(f.ticker for f in fund_opens),
            *(c.ticker for c in calls),
        }
        contexts = [
            {"ticker": pc.ticker, "last": pc.last_price,
             "change_1d_pct": pc.change_1d_pct,
             "change_5d_pct": pc.change_5d_pct}
            for pc in (s.get(PriceContext, t) for t in tickers_of_interest)
            if pc is not None
        ]

    return {
        "open_paper_positions": [
            {"ticker": p.ticker, "side": p.side, "qty": p.qty,
             "entry": p.entry_price}
            for p in paper_opens
        ],
        "open_fund_positions": [
            {"ticker": p.ticker, "side": p.side, "fund_id": p.fund_id,
             "entry": p.entry_price}
            for p in fund_opens
        ],
        "recent_high_conv_calls": [
            {"ticker": c.ticker, "direction": c.direction,
             "conviction": c.conviction, "source": c.source,
             "thesis": (c.thesis or "")[:200],
             "ret_1d_pct": c.ret_1d_pct, "ret_5d_pct": c.ret_5d_pct}
            for c in calls
        ],
        "material_filings_7d": [
            {"ticker": f.ticker, "form": f.form_type,
             "summary": (f.summary or "")[:200],
             "materiality_score": f.materiality_score}
            for f in filings
        ],
        "breaking_news_24h": [
            {"ticker": n.ticker, "title": (n.title or "")[:140],
             "sentiment": n.sentiment, "source": n.source}
            for n in news
        ],
        "price_contexts": contexts,
    }


def _validate_open(d: dict) -> dict | None:
    """Coerce + clamp one `open` entry from the generator's output.
    Returns None when too malformed to keep."""
    try:
        ticker = str(d.get("ticker") or "").upper().strip()
        if not ticker or len(ticker) > 12:
            return None
        direction = str(d.get("direction") or "").lower()
        if direction not in ("long", "short", "neutral"):
            return None
        title = str(d.get("title") or "").strip()
        if not title:
            return None
        body = str(d.get("body") or "").strip()
        invalidation = str(d.get("invalidation_criteria") or "").strip()
        if not invalidation:
            return None
        try:
            conviction = int(d.get("conviction") or 3)
        except (TypeError, ValueError):
            conviction = 3
        conviction = max(1, min(5, conviction))
        target_price = d.get("target_price")
        if target_price is not None:
            try:
                target_price = float(target_price)
                if target_price <= 0:
                    target_price = None
            except (TypeError, ValueError):
                target_price = None
        horizon_days = d.get("horizon_days")
        if horizon_days is not None:
            try:
                horizon_days = int(horizon_days)
                if horizon_days <= 0:
                    horizon_days = None
            except (TypeError, ValueError):
                horizon_days = None
        return {
            "ticker": ticker, "direction": direction,
            "title": title[:200], "body": body,
            "invalidation_criteria": invalidation[:500],
            "conviction": conviction,
            "target_price": target_price,
            "horizon_days": horizon_days,
        }
    except Exception:
        return None


def generate_cycle() -> dict:
    """Daily-ish: ask heavy LLM to propose new theses (and close any
    no-longer-valid ones). Returns counts.

    Cheap-on-quiet-days: when the LLM returns empty arrays, nothing
    is written. Hard cap on simultaneously-active theses prevents
    accumulation across days when the generator is "feeling
    productive"."""
    active = list_active()
    if len(active) >= _MAX_ACTIVE:
        logger.info(
            "thesis: {} active theses already (cap {}); generator focuses on "
            "closing rather than opening this cycle",
            len(active), _MAX_ACTIVE,
        )

    context = _build_generator_context()
    active_short = [
        {"id": a["id"], "ticker": a["ticker"],
         "direction": a["direction"], "title": a["title"],
         "conviction": a["conviction"],
         "supporting": a["supporting_events"],
         "challenging": a["challenging_events"]}
        for a in active
    ]
    prompt = _GENERATE_PROMPT.safe_substitute(
        context_json=json.dumps(context, default=str)[:9000],
        active_theses_json=json.dumps(active_short, default=str)[:2000],
    )

    try:
        body = get_llm().complete(
            prompt, model="heavy", max_tokens=2000, fallback_light=True,
        )
    except Exception as e:
        logger.exception("thesis generate LLM call failed: {}", e)
        return {"opened": 0, "closed": 0, "error": str(e)}

    if not body or body == LLM_ERROR_SENTINEL:
        return {"opened": 0, "closed": 0, "error": "LLM returned empty"}

    data = _parse_generator_output(body)
    if data is None:
        return {"opened": 0, "closed": 0, "error": "unparseable LLM output"}

    now = _now()
    model = _model_name()
    opened = 0
    closed = 0

    with session_scope() as s:
        for entry in (data.get("close") or []):
            try:
                tid = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            reason = str(entry.get("reason") or "")[:400] or "closed by generator"
            t = s.get(Thesis, tid)
            if t is None or t.state != "active":
                continue
            t.state = "closed"
            t.closed_at = now
            t.close_reason = reason
            t.updated_at = now
            s.add(t)
            closed += 1

        # Enforce cap before opening — if we're at/over, skip new opens
        if len(active) + opened - closed >= _MAX_ACTIVE:
            logger.info("thesis: cap respected, not opening new theses")
        else:
            slots = max(0, _MAX_ACTIVE - (len(active) - closed))
            for d in (data.get("open") or [])[:slots]:
                clean = _validate_open(d)
                if clean is None:
                    continue
                # Dedup: don't double-open on the same ticker if active
                same = s.exec(
                    select(Thesis)
                    .where(Thesis.state == "active")
                    .where(Thesis.ticker == clean["ticker"])
                ).first()
                if same is not None:
                    continue
                s.add(Thesis(
                    ticker=clean["ticker"],
                    direction=clean["direction"],
                    title=clean["title"],
                    body=clean["body"],
                    invalidation_criteria=clean["invalidation_criteria"],
                    conviction=clean["conviction"],
                    target_price=clean["target_price"],
                    horizon_days=clean["horizon_days"],
                    state="active",
                    source_event=f"generator:{now.isoformat()[:19]}",
                    model=model,
                    created_at=now,
                    updated_at=now,
                ))
                opened += 1

    logger.info(
        "thesis.generate_cycle: opened={} closed={} active_after={}",
        opened, closed, len(active) + opened - closed,
    )
    return {"opened": opened, "closed": closed}


def _parse_generator_output(raw: str) -> dict | None:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    first = s.find("{")
    last = s.rfind("}")
    if first >= 0 and last >= first:
        s = s[first:last + 1]
    try:
        d = json.loads(s)
    except Exception as e:
        logger.warning("thesis.generate: JSON parse failed: {} — raw: {}",
                       e, s[:300])
        return None
    if not isinstance(d, dict):
        return None
    return d


# ── review (rule-based, daily-ish) ────────────────────────────────────────


def _price_at_or_before(session, ticker: str, when: datetime) -> float | None:
    """Closest PriceBar at or before `when` within
    ``_PRICE_LOOKUP_WINDOW_DAYS``. Used to anchor the price-based
    invalidation against the actual price when the thesis was opened,
    not against the live mark at review time."""
    when_naive = when.replace(tzinfo=None) if when.tzinfo else when
    floor = when_naive - timedelta(days=_PRICE_LOOKUP_WINDOW_DAYS)
    bar = session.exec(
        select(PriceBar)
        .where(PriceBar.ticker == ticker)
        .where(PriceBar.ts <= when_naive)
        .where(PriceBar.ts >= floor)
        .order_by(PriceBar.ts.desc())
        .limit(1)
    ).first()
    return bar.close if bar is not None else None


def review_cycle() -> dict:
    """Walk active theses, apply state transitions based on accumulated
    events + price moves. Pure rules; LLM-free; cheap.

    Transitions implemented:
    - target_price hit (long → last_price ≥ target; short → last_price
      ≤ target) → state=`validated`
    - last_price has moved ≥ ``_INVALIDATE_ADVERSE_PCT`` against the
      thesis direction since creation → state=`invalidated`
      (matches the default invalidation_criteria text auto_thesis
      writes; was missing as actual logic, so a thesis going 30%
      adverse just sat until events or horizon caught it)
    - challenge ratio ≥ `_INVALIDATE_CHALLENGE_RATIO` and total events
      ≥ `_INVALIDATE_MIN_EVENTS` → state=`invalidated`
    - horizon_days elapsed without invalidation → state=`matured`
    """
    now = _now()
    validated = invalidated = matured = 0
    transitions: list[tuple[str, str | None, str, str | None]] = []
    with session_scope() as s:
        active = s.exec(
            select(Thesis).where(Thesis.state == "active")
        ).all()
        for t in active:
            close_state: str | None = None
            close_reason: str | None = None

            # Look up current mark once per thesis — used by both
            # target-hit and adverse-move checks below.
            pc = s.get(PriceContext, t.ticker)
            last = pc.last_price if pc else None

            # Target hit?
            if t.target_price and t.target_price > 0 and last is not None:
                if t.direction == "long" and last >= t.target_price:
                    close_state = "validated"
                    close_reason = (
                        f"target ${t.target_price:.4g} hit "
                        f"(last ${last:.4g})"
                    )
                elif t.direction == "short" and last <= t.target_price:
                    close_state = "validated"
                    close_reason = (
                        f"target ${t.target_price:.4g} hit "
                        f"(last ${last:.4g})"
                    )

            # Price-based invalidation: hard adverse move from creation.
            # Anchors against the price bar at thesis open (not the
            # live mark) so a thesis that ran up and reverted only
            # invalidates when actually underwater from inception.
            # Neutral-direction theses can't auto-invalidate this way —
            # no direction to be adverse to.
            if close_state is None and last is not None and t.direction in ("long", "short"):
                anchor = _price_at_or_before(s, t.ticker, _aware(t.created_at))
                if anchor and anchor > 0:
                    move = (last - anchor) / anchor
                    adverse = -move if t.direction == "long" else move
                    if adverse >= _INVALIDATE_ADVERSE_PCT:
                        close_state = "invalidated"
                        close_reason = (
                            f"{t.direction} ${t.ticker} moved "
                            f"{move * 100:+.1f}% from ${anchor:.4g} "
                            f"(open) to ${last:.4g} — adverse "
                            f"≥ {_INVALIDATE_ADVERSE_PCT * 100:.0f}%"
                        )

            # Decisive challenge accumulation
            if close_state is None:
                total = t.supporting_events + t.challenging_events
                if (
                    total >= _INVALIDATE_MIN_EVENTS
                    and t.supporting_events > 0
                    and t.challenging_events / max(1, t.supporting_events)
                    >= _INVALIDATE_CHALLENGE_RATIO
                ) or (
                    total >= _INVALIDATE_MIN_EVENTS
                    and t.supporting_events == 0
                    and t.challenging_events >= _INVALIDATE_MIN_EVENTS
                ):
                    close_state = "invalidated"
                    close_reason = (
                        f"challenge ratio {t.challenging_events}/"
                        f"{max(1, t.supporting_events)} after "
                        f"{total} linked events"
                    )

            # Horizon elapsed
            if close_state is None and t.horizon_days:
                age = (now - _aware(t.created_at)).days
                if age >= t.horizon_days:
                    close_state = "matured"
                    close_reason = (
                        f"horizon of {t.horizon_days}d reached "
                        f"({age}d since open)"
                    )

            if close_state:
                t.state = close_state
                t.closed_at = now
                t.close_reason = close_reason
                t.updated_at = now
                s.add(t)
                transitions.append((t.ticker, t.direction, close_state, close_reason))
                if close_state == "validated":
                    validated += 1
                elif close_state == "invalidated":
                    invalidated += 1
                elif close_state == "matured":
                    matured += 1

    # Cross-pollination: tell the rest of the system a thesis just
    # changed state. Without these emits the transition was silent —
    # synthesis, book_risk, and the dashboard had no way to learn that
    # the active stance on $X just flipped. Tier 2 so a coincident
    # filing / why_moved on the same ticker still supersedes (those
    # are the actual moves driving the invalidation).
    if transitions:
        try:
            from .narrative import record_event
            for tk, direction, state, reason in transitions:
                headline = (
                    f"thesis {state}"
                    + (f" ({direction})" if direction in ("long", "short") else "")
                )
                record_event(
                    tk, "thesis_state", headline,
                    tier=2, detail=(reason or "")[:1200],
                )
        except Exception as e:
            logger.debug("thesis.review_cycle: narrative emit failed: {}", e)
        try:
            from . import events as _ev
            for tk, direction, state, reason in transitions:
                _ev.publish("thesis", {
                    "kind": f"thesis_{state}",
                    "ticker": tk,
                    "direction": direction,
                    "summary": reason or state,
                })
        except Exception:
            pass

    if validated or invalidated or matured:
        logger.info(
            "thesis.review_cycle: validated={} invalidated={} matured={}",
            validated, invalidated, matured,
        )
    return {
        "validated": validated,
        "invalidated": invalidated,
        "matured": matured,
    }


# ── async wrappers for the scheduler ──────────────────────────────────────


async def run_generate_cycle() -> None:
    """APScheduler entrypoint — runs `generate_cycle` off the loop."""
    import asyncio
    try:
        await asyncio.to_thread(generate_cycle)
    except Exception as e:
        logger.exception("thesis.run_generate_cycle failed: {}", e)


async def run_review_cycle() -> None:
    """APScheduler entrypoint — runs `review_cycle` off the loop."""
    import asyncio
    try:
        await asyncio.to_thread(review_cycle)
    except Exception as e:
        logger.exception("thesis.run_review_cycle failed: {}", e)
