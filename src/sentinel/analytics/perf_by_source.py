"""Performance attribution by entry setup.

Groups closed trades by `open_reason` (the bot's pipeline name when
the trade originated — synthesis, convergence, why_moved, manual via
/book, research_execute, …) and emits per-source stats: count, win
rate, total PnL, average R-multiple, average hold time.

Answers the trader's most important question: "which of my entry
styles actually works?" — the one stat that, if you watch it long
enough, decides whether your edge survives.

Reuses funds.closed_trades_recent() so no new queries.
"""

from __future__ import annotations

from .. import funds as _funds


def _bucket(open_reason: str | None) -> str:
    """Normalise an open_reason string into a stable bucket key. We
    use the first token before a colon/space because the source
    strings are like "manual via /book", "convergence:NVDA",
    "research_execute" — the prefix is the meaningful axis."""
    if not open_reason:
        return "unknown"
    s = open_reason.strip().lower()
    # Take everything before the first colon, space, or slash.
    for sep in (":", " ", "/"):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s or "unknown"


def perf_by_source(limit: int = 500) -> dict:
    rows = _funds.closed_trades_recent(limit=limit)
    if not rows:
        return {
            "n": 0,
            "groups": [],
        }

    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(_bucket(r.get("open_reason")), []).append(r)

    groups: list[dict] = []
    for key, items in buckets.items():
        wins = [r for r in items if (r.get("realized_pnl") or 0) > 0]
        losses = [r for r in items if (r.get("realized_pnl") or 0) < 0]
        pnl = sum(r.get("realized_pnl") or 0 for r in items)
        rs = [r["r_multiple"] for r in items if r.get("r_multiple") is not None]
        holds = [r["hold_h"] for r in items if r.get("hold_h") is not None]
        # Expectancy in $ per trade (signed) — turns a noisy sample
        # of trades into one number the trader can compare across
        # sources.
        expectancy = pnl / len(items) if items else 0.0
        avg_win = (
            sum(r["realized_pnl"] for r in wins) / len(wins) if wins else None
        )
        avg_loss = (
            sum(r["realized_pnl"] for r in losses) / len(losses) if losses else None
        )
        groups.append({
            "source": key,
            "n": len(items),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(
                len(wins) / max(1, len(wins) + len(losses)) * 100, 1
            ) if (wins or losses) else None,
            "total_pnl": round(pnl, 2),
            "expectancy": round(expectancy, 2),
            "avg_win": round(avg_win, 2) if avg_win is not None else None,
            "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
            "avg_r": (
                round(sum(rs) / len(rs), 2) if rs else None
            ),
            "avg_hold_h": (
                round(sum(holds) / len(holds), 1) if holds else None
            ),
            # last-3 realised PnLs so the UI can render a tiny per-row
            # scoreboard without a second query.
            "recent_pnls": [
                round(r.get("realized_pnl") or 0, 2) for r in items[:5]
            ],
        })

    # Sort: highest total PnL first; sources with no closed trades
    # filtered to bottom.
    groups.sort(key=lambda g: (g["n"] == 0, -g["total_pnl"]))

    return {
        "n": len(rows),
        "groups": groups,
    }
