"""Sentiment-scoring retrospective.

For every news item with both a `sentiment` score (set at ingest)
AND an `impact_1d_pct` (set after 24h by `news_impact`), grade
whether the bot's sentiment direction predicted the actual price
move.

Outcomes per article:
- "right": sentiment > 0 and impact > 0.5%, or sentiment < 0 and impact < -0.5%
- "wrong": opposite direction
- "muted": |impact| <= 0.5% (no real move to predict)
- "neutral_sentiment": |sentiment| <= 0.15

Aggregated per source so the user can spot "RSS feed X has -3
calibration; the bot reads it bullish but the stock drops."
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import NewsItem


def sentiment_quality(days: int = 60) -> dict:
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).replace(tzinfo=None)

    by_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"right": 0, "wrong": 0, "muted": 0, "neutral": 0, "n": 0}
    )
    overall = {"right": 0, "wrong": 0, "muted": 0, "neutral": 0, "n": 0}

    with session_scope() as s:
        rows = s.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff_naive)
            .where(NewsItem.sentiment.is_not(None))
            .where(NewsItem.impact_1d_pct.is_not(None))
        ).all()

        for n in rows:
            sent = n.sentiment or 0.0
            impact = n.impact_1d_pct or 0.0
            bucket = by_source[n.source]
            overall["n"] += 1
            bucket["n"] += 1

            if abs(impact) <= 0.5:
                overall["muted"] += 1
                bucket["muted"] += 1
                continue
            if abs(sent) <= 0.15:
                overall["neutral"] += 1
                bucket["neutral"] += 1
                continue
            same_dir = (sent > 0 and impact > 0) or (sent < 0 and impact < 0)
            if same_dir:
                overall["right"] += 1
                bucket["right"] += 1
            else:
                overall["wrong"] += 1
                bucket["wrong"] += 1

    def _shape(b: dict) -> dict:
        directional = b["right"] + b["wrong"]
        return {
            "n": b["n"],
            "right": b["right"],
            "wrong": b["wrong"],
            "muted": b["muted"],
            "neutral": b["neutral"],
            "directional_accuracy": (
                b["right"] / directional if directional else None
            ),
        }

    return {
        "window_days": days,
        "overall": _shape(overall),
        "by_source": [
            {"source": k, **_shape(v)}
            for k, v in sorted(
                by_source.items(),
                key=lambda kv: kv[1]["n"],
                reverse=True
            )
        ],
    }
