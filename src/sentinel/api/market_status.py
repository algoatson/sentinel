"""Market-state endpoint — open / pre / after / closed / holiday.

US equity session timing (NYSE / NASDAQ):
  pre-market   04:00–09:30 ET
  regular      09:30–16:00 ET
  after-hours  16:00–20:00 ET
  closed       otherwise (incl. weekends + holidays)

NYSE holidays for the next 24 months are hardcoded — they're
known years in advance and the schedule rarely changes mid-year.
If we miss one (rare half-day before Thanksgiving etc) the
endpoint just reports "closed" on that date — degrade is safe.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter


router = APIRouter()

_ET = ZoneInfo("America/New_York")

# NYSE full-day closures. Half-days (early close at 13:00 ET) noted
# separately. Update yearly.
_HOLIDAYS_FULL: dict[str, str] = {
    # 2026
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Presidents' Day",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas Day",
    # 2027
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Presidents' Day",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving",
    "2027-12-24": "Christmas Day (observed)",
}
_HALF_DAYS: dict[str, str] = {
    "2026-11-27": "Day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "Day after Thanksgiving",
}


def _today_et() -> date:
    return datetime.now(_ET).date()


def _next_open_day(d: date) -> date:
    """Next weekday that isn't a holiday."""
    nxt = d + timedelta(days=1)
    while nxt.isoweekday() > 5 or nxt.isoformat() in _HOLIDAYS_FULL:
        nxt += timedelta(days=1)
    return nxt


@router.get("/market-status")
def market_status() -> dict:
    now = datetime.now(_ET)
    today = now.date()
    is_weekend = today.isoweekday() > 5
    iso = today.isoformat()
    holiday_label = _HOLIDAYS_FULL.get(iso)
    half_day_label = _HALF_DAYS.get(iso)

    # Defaults
    state = "closed"
    label = "Closed"
    emoji = "🌙"
    next_event: str | None = None

    if holiday_label:
        state = "holiday"
        label = holiday_label
        # Pick a thematic emoji.
        lowered = holiday_label.lower()
        emoji = (
            "🎄" if "christmas" in lowered else
            "🎆" if "new year" in lowered else
            "🦃" if "thanksgiving" in lowered else
            "🎇" if "independence" in lowered else
            "🌷"  # spring-ish default for the rest
        )
        next_event = f"Reopens {_next_open_day(today).isoformat()}"
    elif is_weekend:
        state = "closed"
        emoji = "📅"
        label = "Weekend"
        next_event = f"Opens Mon {_next_open_day(today).isoformat()}"
    else:
        h, m = now.hour, now.minute
        minute_of_day = h * 60 + m
        regular_open = 9 * 60 + 30
        regular_close = 16 * 60
        pre_open = 4 * 60
        after_close = 20 * 60

        early_close = half_day_label is not None
        eff_close = 13 * 60 if early_close else regular_close

        if minute_of_day < pre_open:
            state = "closed"
            emoji = "🌙"
            label = "Pre-pre-market"
            next_event = "Pre-market opens 04:00 ET"
        elif minute_of_day < regular_open:
            state = "pre"
            emoji = "🌅"
            label = "Pre-market"
            next_event = "Regular open 09:30 ET"
        elif minute_of_day < eff_close:
            state = "open"
            emoji = "🟢"
            label = (
                f"Open · half-day ({half_day_label})"
                if early_close else "Open"
            )
            close_h, close_m = divmod(eff_close, 60)
            next_event = f"Closes {close_h:02d}:{close_m:02d} ET"
        elif minute_of_day < after_close:
            state = "after"
            emoji = "🌆"
            label = "After-hours"
            next_event = "After-hours ends 20:00 ET"
        else:
            state = "closed"
            emoji = "🌙"
            label = "Closed (post)"
            next_event = (
                f"Pre-market opens {_next_open_day(today).isoformat()} 04:00 ET"
            )

    return {
        "state": state,                 # open | pre | after | closed | holiday
        "label": label,                 # human-readable
        "emoji": emoji,                 # display chip
        "session_open": state == "open" or state == "pre" or state == "after",
        "regular_open": state == "open",
        "next_event": next_event,
        "as_of": now.isoformat(),
        "et_clock": now.strftime("%H:%M ET"),
        "today": iso,
        "holiday": holiday_label,
        "half_day": half_day_label,
    }
