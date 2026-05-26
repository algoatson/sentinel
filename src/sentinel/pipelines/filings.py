"""Filings pipeline per SPEC §7 + §9.

Discovery model:
  Old: poll EDGAR's submissions.json for each of ~600 watchlist CIKs every
       cycle. Each cycle = ~600 rate-limited HTTP calls, often >>10 min wall.
  New: cheap probe via EDGAR's getcurrent Atom feed (single HTTP call returns
       the 100 most-recent filings across all of EDGAR), filter to watchlist
       CIKs + unseen accession numbers, then per-CIK deep-fetch only for hits.
       Typical cycle = 1 + N HTTP calls where N is small.

Concurrency model:
  EdgarClient + LLM client + httpx.Client are sync. The pipeline runs inside
  AsyncIOScheduler, so every blocking call (HTTP, LLM, primary-doc fetch) is
  wrapped in `asyncio.to_thread` to keep the event loop free for sibling
  jobs (prices_poll, news_poll, news_alerts, etc.).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..edgar.client import EdgarClient, FilingMeta
from ..llm import LLM_ERROR_SENTINEL, get_llm, parse_json_response
from ..models import Filing, SeenFiling, Watchlist
from ..prompts import get_prompt
from .enrich import EnrichmentContext, enrich


# Empirically calibrated: with max_tokens=800 the light model was hitting
# `done_reason=length` on the majority of filings (prompt 1.5–2.8k tokens →
# the response ran out of budget mid-JSON and the parser returned
# LLM_ERROR_SENTINEL, dropping ~70% of detected filings in a real run on
# 2026-05-19). 1500 gives headroom for the long-tail filings without paying
# much more on the median case (Ollama stops at first stop-token; this is
# only a ceiling).
_FILINGS_LLM_MAX_TOKENS = 1500


FORM_TYPE_MAPPING: dict[str, tuple[str, str]] = {
    "8-K": ("summarize_8k", "light"),
    "8-K/A": ("summarize_8k", "light"),
    "4": ("summarize_form4", "light"),
    "4/A": ("summarize_form4", "light"),
    "10-Q": ("summarize_10q", "heavy"),
    "10-Q/A": ("summarize_10q", "heavy"),
    "10-K": ("summarize_10k", "heavy"),
    "10-K/A": ("summarize_10k", "heavy"),
    "13F-HR": ("summarize_13f", "heavy"),
    "13F-HR/A": ("summarize_13f", "heavy"),
    "S-1": ("summarize_offering", "heavy"),
    "S-1/A": ("summarize_offering", "heavy"),
    "DEF 14A": ("summarize_proxy", "heavy"),
    "PRE 14A": ("summarize_proxy", "heavy"),
}

_INSIDER_FORMS = {"4", "4/A", "13F-HR", "13F-HR/A"}


def _select_prompt_and_model(form_type: str) -> tuple[str, str]:
    if form_type in FORM_TYPE_MAPPING:
        return FORM_TYPE_MAPPING[form_type]
    if form_type.startswith("424B"):
        return ("summarize_offering", "light")
    return ("summarize_generic", "light")


def _score_materiality_sync(
    form_type: str,
    ticker: Optional[str],
    summary: str,
    enrichment: EnrichmentContext,
) -> tuple[Optional[int], Optional[str]]:
    """Synchronous materiality scorer — call via asyncio.to_thread from async code."""
    llm = get_llm()
    tmpl = get_prompt("materiality")
    rendered = tmpl.safe_substitute(
        form_type=form_type,
        ticker=ticker or "",
        summary=summary[:2000],
        enrichment_json=json.dumps(enrichment.to_dict(), default=str),
    )
    # Materiality scoring is a pure JSON classifier — date and
    # world-state anchor don't influence the score. Saves the
    # ~250-token grounding overhead per filing.
    raw = llm.complete(
        rendered, model="light", json_mode=True, max_tokens=300,
        grounded=False,
    )
    parsed = parse_json_response(raw, expect=dict)
    if parsed is None:
        return None, None
    try:
        score = int(parsed.get("score"))
        reason = str(parsed.get("reason", ""))[:240]
    except (TypeError, ValueError):
        return None, None
    if score not in (0, 1, 2, 3):
        return None, None
    return score, reason


def _route(form_type: str, score: Optional[int]) -> tuple[Optional[int], Optional[str]]:
    """SPEC §9 routing. Returns (channel_id, channel_name).

    No @-mention is emitted: this is a single-user bot whose client sets
    AllowedMentions.none() globally, so importance badges — not pings — are
    the triage signal.
    """
    if score is None:
        return None, None
    is_insider = form_type in _INSIDER_FORMS
    if is_insider:
        if score >= 2:
            return settings.DISCORD_INSIDERS_CHANNEL_ID, "insiders"
        return None, None
    if score == 3:
        return settings.DISCORD_PRIORITY_CHANNEL_ID, "priority"
    if score == 2:
        return settings.DISCORD_FILINGS_CHANNEL_ID, "filings"
    return None, None


async def run_filings_cycle() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_filings_cycle top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ filings cycle error: {e}")
        except Exception:
            pass


async def _run() -> None:
    client = EdgarClient()
    llm = get_llm()
    now = datetime.now(timezone.utc)
    # 2h overlap window absorbs poll jitter and getcurrent feed turnover.
    since = now - timedelta(hours=2)

    # Watchlist as a set for O(1) membership checks.
    with session_scope() as session:
        watch_ciks = {row.cik for row in session.exec(select(Watchlist)).all()}
    logger.info("filings cycle: {} watchlist CIKs", len(watch_ciks))

    # Step 1 — single cheap probe across all of EDGAR.
    try:
        recent_global = await asyncio.to_thread(
            client.fetch_recent_filings_global, since, 100
        )
    except Exception as e:
        logger.warning("getcurrent feed fetch failed: {}", e)
        return
    logger.info("filings cycle: getcurrent returned {} entries", len(recent_global))

    # Step 2 — filter to watchlist CIKs + not-already-seen.
    pending_by_cik: dict[str, set[str]] = {}  # cik → set of accession numbers
    with session_scope() as session:
        for entry in recent_global:
            cik = entry["cik"]
            if cik not in watch_ciks:
                continue
            acc = entry["accession_number"]
            if session.get(SeenFiling, acc) is not None:
                continue
            pending_by_cik.setdefault(cik, set()).add(acc)

    if not pending_by_cik:
        logger.info("filings cycle: no new filings on watchlist")
        return

    total_hits = sum(len(v) for v in pending_by_cik.values())
    logger.info(
        "filings cycle: {} CIKs hit, {} new filings to process",
        len(pending_by_cik),
        total_hits,
    )

    # Step 3 — for each CIK with hits, fetch its submissions.json once to get
    # full FilingMeta records, then process only the accession numbers from
    # the getcurrent feed.
    # Use a wider since-window here: getcurrent's filed_at is full timestamp
    # but submissions.json stores filed_at as date-only (parsed as midnight
    # UTC). A filing made at 14:00 has submissions.json filed_at=midnight,
    # which is older than now-2h — list_recent_filings would drop it. The
    # accession_number filter below ensures we only process the wanted ones.
    deep_since = now - timedelta(days=2)
    new_count = 0
    posted_count = 0
    for cik, wanted_accs in pending_by_cik.items():
        try:
            metas = await asyncio.to_thread(client.list_recent_filings, cik, deep_since)
        except Exception as e:
            logger.warning("list_recent_filings failed for {}: {}", cik, e)
            continue

        for meta in metas:
            if meta.accession_number not in wanted_accs:
                continue
            new_count += 1
            posted = await _process_filing(client, llm, meta)
            if posted:
                posted_count += 1

    logger.info(
        "filings cycle: {} new, {} posted", new_count, posted_count
    )


async def _process_filing(client: EdgarClient, llm, meta: FilingMeta) -> bool:
    """Process one filing: mark seen, fetch + summarize + score + route + persist.
    Returns True if a Discord post happened.
    """
    # Crash-safe dedupe: write SeenFiling row before doing any work.
    with session_scope() as session:
        if session.get(SeenFiling, meta.accession_number) is not None:
            return False
        session.add(
            SeenFiling(
                accession_number=meta.accession_number,
                seen_at=datetime.now(timezone.utc),
            )
        )

    try:
        doc_text = await asyncio.to_thread(
            client.fetch_primary_document,
            meta.primary_doc_url,
        )
    except Exception as e:
        logger.warning(
            "fetch_primary_document failed for {}: {}", meta.accession_number, e
        )
        return False

    prompt_name, model = _select_prompt_and_model(meta.form_type)
    tmpl = get_prompt(prompt_name)
    rendered = tmpl.safe_substitute(text=doc_text)
    summary = await asyncio.to_thread(
        llm.complete, rendered, model=model,
        max_tokens=_FILINGS_LLM_MAX_TOKENS,
    )

    filing_obj = Filing(
        cik=meta.cik,
        ticker=meta.ticker,
        form_type=meta.form_type,
        accession_number=meta.accession_number,
        filed_at=meta.filed_at,
        primary_doc_url=meta.primary_doc_url,
    )

    if summary == LLM_ERROR_SENTINEL:
        logger.error("LLM error on {}", meta.accession_number)
        filing_obj.summary = None
        with session_scope() as session:
            session.add(filing_obj)
        return False

    filing_obj.summary = summary

    # Enrich (pure DB) + score (LLM, async-wrapped).
    context = enrich(filing_obj)
    score, reason = await asyncio.to_thread(
        _score_materiality_sync,
        meta.form_type,
        meta.ticker,
        summary,
        context,
    )
    filing_obj.materiality_score = score
    filing_obj.materiality_reason = reason

    channel_id, channel_name = _route(meta.form_type, score)
    posted = False
    if channel_id and channel_name:
        try:
            message_id = await discord_client.post_filing(
                filing_obj,
                enrichment=context,
                channel_id=channel_id,
            )
            filing_obj.message_id = message_id
            filing_obj.channel = channel_name
            filing_obj.posted_at = datetime.now(timezone.utc)
            posted = True
            if filing_obj.ticker and (score or 0) >= 2:
                from ..narrative import record_event

                record_event(
                    filing_obj.ticker,
                    "filing",
                    f"{meta.form_type} (mat {score}) — {(reason or '')[:120]}",
                    tier=3,
                    detail=(filing_obj.summary or "")[:600],
                    channel_id=channel_id,
                    message_id=message_id,
                )
        except Exception as e:
            logger.exception(
                "discord post failed for {}: {}", meta.accession_number, e
            )

    with session_scope() as session:
        session.add(filing_obj)
        session.flush()
        filing_id = filing_obj.id

    # Link this filing to any active theses on the same ticker so the
    # thesis timeline tracks the event. Best-effort; failure logs and
    # doesn't bubble — filings ingestion is not a hard dependency of
    # the thesis engine, and we'd rather ship the filing than block
    # on a downstream enrichment.
    if filing_obj.ticker and filing_id is not None:
        try:
            from .. import thesis
            thesis.link_filing(filing_id)
        except Exception as e:
            logger.debug("thesis.link_filing({}) failed: {}", filing_id, e)

    # Broadcast to live dashboard subscribers.
    if filing_id is not None:
        try:
            from .. import events
            events.publish("filing", {
                "id": filing_id,
                "ticker": filing_obj.ticker,
                "form_type": filing_obj.form_type,
                "accession_number": filing_obj.accession_number,
                "materiality_score": filing_obj.materiality_score,
                "summary": (filing_obj.summary or "")[:200],
            })
        except Exception as e:
            logger.debug("events.publish(filing) failed: {}", e)
    return posted
