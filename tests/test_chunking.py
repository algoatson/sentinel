"""chunk_text contract — the guard that stops long Q&A/thread answers from
being chopped mid-sentence at Discord's 2000-char ceiling."""

from __future__ import annotations

from sentinel.utils import chunk_text


def test_empty_and_short():
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text("short answer") == ["short answer"]


def test_never_exceeds_limit_and_preserves_content():
    body = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(20))
    parts = chunk_text(body, limit=500)
    assert len(parts) > 1
    assert all(0 < len(p) <= 500 for p in parts)
    # every word survives (nothing truncated away)
    assert "".join(parts).count("Paragraph") == 20
    assert "".join(parts).replace("\n", "").count("x") == 20 * 300


def test_breaks_on_paragraph_boundary_when_possible():
    parts = chunk_text("A" * 400 + "\n\n" + "B" * 400, limit=500)
    assert parts == ["A" * 400, "B" * 400]  # split at the blank line, clean


def test_single_oversized_line_is_hard_split_not_dropped():
    one = "Z" * 1200          # one line, no break points, over the limit
    parts = chunk_text(one, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "".join(parts) == one        # fully preserved, just split
