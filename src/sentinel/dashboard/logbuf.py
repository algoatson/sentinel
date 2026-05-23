"""A bounded in-memory tail of the bot's own loguru stream.

The dashboard needs "what is the bot doing right now" without shelling out to
a log file (there isn't one in this deployment — loguru goes to stderr). So we
attach one extra loguru sink that appends each formatted record to a fixed-size
ring. `maxlen` makes it self-pruning: memory is O(1), the oldest lines fall off.

`install()` is idempotent and only called when the dashboard mounts, so a
bot run without the dashboard pays nothing for this.
"""

from __future__ import annotations

from collections import deque

from loguru import logger

# Newest-last. ~500 lines is enough to see a cycle's worth of activity in the
# log pane while staying trivially small in memory.
_RING: deque[str] = deque(maxlen=500)
_sink_id: int | None = None

# Compact, dashboard-friendly line: time | LEVEL | module:line - message.
_FMT = "{time:HH:mm:ss} | {level: <7} | {name}:{line} - {message}"


def _sink(message) -> None:
    # loguru hands a str-subclass (with a .record); store the rendered line
    # minus its trailing newline. Never raise from a logging sink.
    try:
        _RING.append(str(message).rstrip("\n"))
    except Exception:
        pass


def install() -> None:
    """Attach the ring sink once. Safe to call repeatedly."""
    global _sink_id
    if _sink_id is not None:
        return
    _sink_id = logger.add(
        _sink,
        level="INFO",
        format=_FMT,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )
    logger.info("dashboard log ring attached ({} lines)", _RING.maxlen)


def tail(n: int = 200) -> list[str]:
    """The most recent up-to-n lines, oldest-first (chronological)."""
    if n <= 0:
        return []
    buf = list(_RING)
    return buf[-n:]


def text(n: int = 200) -> str:
    return "\n".join(tail(n))
