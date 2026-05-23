"""Discord embed styling — single source of truth.

The codebase had ~18 ad-hoc hex colors with no shared meaning (three
different greens for "good", same blue for two unrelated things). This module
fixes one meaning to one color so the whole server reads coherently:

    BULLISH / BEARISH / NEUTRAL  → directional P&L, moves, hit/miss
    INFO     → readouts, help, status
    ACCENT   → the bot's own voice (synthesis, lounge)
    ALERT    → warnings, breaking, high-importance
    GOLD     → discovery / highlights
    REDDIT   → the dedicated Reddit stream (brand orange)

Adopt incrementally: new/reworked embeds use these; `pct_color()` is the
go-to for anything tied to a percentage move.
"""

from __future__ import annotations

# ── Semantic palette ────────────────────────────────────────────────────────
BULLISH = 0x2ECC71  # green  — up / win / long working
BEARISH = 0xE74C3C  # red    — down / loss / thesis breaking
NEUTRAL = 0x95A5A6  # grey   — flat / unknown / no signal
INFO = 0x3498DB     # blue   — data readouts, help, health
ACCENT = 0x9B59B6   # purple — the bot's own synthesized voice
ALERT = 0xE67E22    # orange — warning / breaking / act-soon
GOLD = 0xF1C40F     # yellow — discovery, highlights
REDDIT = 0xFF4500   # reddit brand orange — the #reddit stream


def pct_color(x: float | None) -> int:
    """Color a value by the sign of a percentage move."""
    if x is None:
        return NEUTRAL
    return BULLISH if x > 0 else BEARISH if x < 0 else NEUTRAL


def tally_color(good: int, bad: int) -> int:
    """Color a win/loss tally (e.g. scorecard verdicts)."""
    return BULLISH if good > bad else BEARISH if bad > good else NEUTRAL


# ── Reddit-curation categories → (emoji, color) ─────────────────────────────
# Used by reddit_feed; the LLM curator must emit one of these keys.
REDDIT_CATEGORIES: dict[str, tuple[str, int]] = {
    "important": ("🚨", ALERT),
    "interesting": ("💡", INFO),
    "funny": ("😂", GOLD),
    "hype": ("🔥", REDDIT),
}
REDDIT_FALLBACK = ("📌", REDDIT)
