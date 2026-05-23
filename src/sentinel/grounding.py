"""LLM grounding preamble — defeats training-cutoff bias.

LLMs we route through (Llama, Qwen, Gemma — all mid-to-late 2024 cutoffs)
will dismiss real 2026 news as "misinformation" if the news contradicts
their stale world model. The bot DOES have authoritative current data
from Reuters/CNBC/SEC/Reddit/yfinance; the LLM just needs to be told to
trust it.

The fix is a short preamble — date + "trust the data" rules + a small
world-state anchor — prepended to every LLM call by the `LLM.complete`
wrapper. ~250 tokens of overhead per call; <$0.03/day at current scale
(see HANDBOOK for the math). Catches every code path: synthesis, dossier,
chat, watches, briefings — nothing needs to opt in.

Anchor data lives in `config/world_anchor.yaml`; the file is reloaded
every 5 minutes so a hot-edit on the Pi (or a `git pull` after updating
on the dev box) is picked up without a service restart. If the YAML is
missing or malformed, the preamble still ships — minus the anchor block.
The trust-rules paragraph is the load-bearing part; the anchor is a
nice-to-have. Silence > a confident lie.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from .config import CONFIG_DIR


# ── tuning constants ──────────────────────────────────────────────────────

# Anchor YAML is hot-reloaded every 5 minutes — long enough that the load
# is amortised across thousands of LLM calls, short enough that an edit
# in production shows up the same coffee break. Adjust if you start
# editing the YAML constantly during a debugging session.
_CACHE_TTL_S = 300

# Hard cap on the prepended block size. If somebody pads the YAML with an
# essay, the bot starts paying real tokens — better to truncate visibly
# (the cutoff marker is logged) than silently inflate every prompt.
_MAX_BLOCK_CHARS = 1600


# Loaded from `config/world_anchor.yaml` on demand. A tuple so swapping
# the cache is one assignment (atomicity isn't strict here — worst case
# is a single duplicate read during the swap, which is harmless).
_cached: tuple[float, str] | None = None


# ── trust rules — the load-bearing paragraph ──────────────────────────────

_TRUST_RULES = """\
The DATA block below comes from real-time authoritative sources (Reuters,
CNBC, SEC EDGAR, Reddit, yfinance) and was ingested in the last few hours.
It IS factual.

Your training data has a hard cutoff that predates today. Do NOT dismiss
the provided data as misinformation, outdated, hypothetical, or fake based
on your training-era worldview. When the data contradicts what you "know",
BELIEVE THE DATA — your priors are a 2024 baseline that has been superseded.
"""


# ── public API ────────────────────────────────────────────────────────────


def block() -> str:
    """Return the grounding preamble (cached, refreshed every `_CACHE_TTL_S`
    seconds). Always includes today's date + the trust rules; appends the
    world-state anchor when the YAML loads cleanly."""
    global _cached
    now = time.time()
    if _cached is not None and (now - _cached[0]) < _CACHE_TTL_S:
        return _cached[1]
    body = _build_block()
    _cached = (now, body)
    return body


def prepend(prompt: str) -> str:
    """Wrap a prompt with the grounding preamble. Uses an explicit `---`
    separator so the LLM can clearly see where grounding ends and the
    task-specific prompt begins.

    Idempotent on prompts that already start with the preamble — repeat
    calls (e.g. via `fallback_light`) won't stack the block twice."""
    if not prompt:
        return prompt
    pre = block()
    if prompt.startswith(pre):
        return prompt
    return f"{pre}\n\n---\n\n{prompt}"


def reset_cache() -> None:
    """Force the next `block()` call to re-read the YAML — used by tests
    and by a `!world reload` admin command if one is wired up later."""
    global _cached
    _cached = None


# ── builders ──────────────────────────────────────────────────────────────


def _build_block() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts: list[str] = [
        f"TODAY: {today} (UTC)",
        "",
        _TRUST_RULES.rstrip(),
    ]
    anchor = _load_anchor()
    if anchor:
        formatted = _format_anchor(anchor)
        if formatted:
            parts += [
                "",
                "WORLD STATE ANCHOR (stable facts as of today):",
                formatted,
            ]
    body = "\n".join(parts)
    if len(body) > _MAX_BLOCK_CHARS:
        logger.warning(
            "grounding block hit cap ({} > {} chars) — truncating; "
            "trim config/world_anchor.yaml",
            len(body), _MAX_BLOCK_CHARS,
        )
        body = body[:_MAX_BLOCK_CHARS - 16] + "\n[…truncated]"
    return body


def _load_anchor() -> dict[str, Any] | None:
    """Read `config/world_anchor.yaml`. Returns None on any error (missing
    file, malformed YAML, wrong top-level type) — the caller falls back
    to "date + trust rules only", which is still useful."""
    path = Path(CONFIG_DIR) / "world_anchor.yaml"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.warning("world_anchor.yaml unreadable: {}", e)
        return None
    if not isinstance(data, dict):
        logger.warning(
            "world_anchor.yaml: expected a top-level mapping, got {}",
            type(data).__name__,
        )
        return None
    return data


def _format_anchor(anchor: dict[str, Any]) -> str:
    """Render the YAML into a tight bullet block. Two sections by
    convention: `us_government` (key: value pairs) and `themes` (a list
    of strings). Unknown keys are appended at the bottom as best-effort
    so a new YAML section doesn't go silently invisible."""
    lines: list[str] = []

    gov = anchor.get("us_government")
    if isinstance(gov, dict) and gov:
        if "president" in gov:
            lines.append(f"- US president: {gov['president']}")
        if "vice_president" in gov:
            lines.append(f"- US vice president: {gov['vice_president']}")
        if "treasury_secretary" in gov:
            lines.append(
                f"- US Treasury secretary: {gov['treasury_secretary']}"
            )
        # Surface any other gov fields the user added without losing them.
        for k, v in gov.items():
            if k in ("president", "vice_president", "treasury_secretary"):
                continue
            lines.append(f"- US {k.replace('_', ' ')}: {v}")

    themes = anchor.get("themes")
    if isinstance(themes, list) and themes:
        if lines:
            lines.append("")
        lines.append("Themes:")
        for t in themes:
            if not isinstance(t, str):
                continue
            # Collapse the YAML folded-string whitespace so each theme
            # reads as one line in the preamble.
            flat = " ".join(t.split())
            lines.append(f"  - {flat}")

    # Tolerate unexpected top-level sections (forward-compat: if someone
    # adds `markets:` or `regulatory:` later, it still shows up).
    for k, v in anchor.items():
        if k in ("us_government", "themes"):
            continue
        if isinstance(v, str):
            lines.append(f"- {k.replace('_', ' ')}: {v.strip()}")
        elif isinstance(v, list):
            if lines:
                lines.append("")
            lines.append(f"{k.replace('_', ' ').title()}:")
            for item in v:
                if isinstance(item, str):
                    lines.append(f"  - {' '.join(item.split())}")

    return "\n".join(lines)
