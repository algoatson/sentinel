"""Trade-streak stats — current W/L streak, max W/L streak, hit rate.

Walks closed trades newest first. The current streak is the run of
same-sign trades at the head; max streaks scan the full window.

Read-only. Reuses funds.closed_trades_recent() so no extra query.
"""

from __future__ import annotations

from .. import funds as _funds


def streaks(limit: int = 200) -> dict:
    rows = _funds.closed_trades_recent(limit=limit)
    if not rows:
        return {
            "n": 0,
            "current": {"kind": "none", "length": 0, "started_at": None},
            "max_win": 0,
            "max_loss": 0,
            "last_pnls": [],
            "hit_rate": None,
            "wins": 0,
            "losses": 0,
            "scratches": 0,
            "expectancy": 0.0,
            "avg_win": None,
            "avg_loss": None,
        }

    def sign(p: float | None) -> int:
        if p is None:
            return 0
        if p > 0:
            return 1
        if p < 0:
            return -1
        return 0

    # rows are newest-first; current streak is the prefix run
    first = sign(rows[0].get("realized_pnl"))
    cur_len = 0
    started_at = None
    if first != 0:
        for r in rows:
            if sign(r.get("realized_pnl")) == first:
                cur_len += 1
                started_at = r.get("exit_at")
            else:
                break
    kind = "win" if first > 0 else "loss" if first < 0 else "none"

    # Max W / Max L by scanning chronologically (oldest → newest).
    chrono = list(reversed(rows))
    max_w = max_l = 0
    cw = cl = 0
    for r in chrono:
        s = sign(r.get("realized_pnl"))
        if s > 0:
            cw += 1
            cl = 0
            max_w = max(max_w, cw)
        elif s < 0:
            cl += 1
            cw = 0
            max_l = max(max_l, cl)
        else:
            cw = cl = 0

    wins = [r["realized_pnl"] for r in rows if (r.get("realized_pnl") or 0) > 0]
    losses = [r["realized_pnl"] for r in rows if (r.get("realized_pnl") or 0) < 0]
    scratches = sum(1 for r in rows if (r.get("realized_pnl") or 0) == 0)
    hit_rate = (
        round(len(wins) / max(1, len(wins) + len(losses)) * 100, 1)
        if (wins or losses) else None
    )
    total_pnl = sum(r.get("realized_pnl") or 0 for r in rows)
    expectancy = round(total_pnl / len(rows), 2) if rows else 0.0
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None

    return {
        "n": len(rows),
        "current": {
            "kind": kind,
            "length": cur_len,
            "started_at": started_at,
        },
        "max_win": max_w,
        "max_loss": max_l,
        # Last 20 PnLs newest→oldest, for a tiny inline scoreboard.
        "last_pnls": [
            round(r.get("realized_pnl") or 0, 2) for r in rows[:20]
        ],
        "hit_rate": hit_rate,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": scratches,
        "expectancy": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }
