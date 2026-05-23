"""Filings-pipeline contract.

What's pinned here is *one* thing the audit on 2026-05-19 caught: the LLM
call in `_process_filing` had max_tokens=800, which the light model was
overrunning on the majority of real filings (done_reason=length → JSON
parse fails → filing dropped). The ceiling now lives in a named module
constant so a regression (hardcoded back to 800, or dialed down without a
matching prompt change) shows up in CI rather than as silent drop-out.
"""

from __future__ import annotations

import inspect

from sentinel.pipelines import filings


def test_max_tokens_above_observed_truncation_threshold():
    # 800 caused ~70% LLM-truncation drop on a real run; 1500 cleared it
    # on the same prompts. Don't dial below 1200 without re-measuring.
    assert filings._FILINGS_LLM_MAX_TOKENS >= 1200


def test_process_filing_uses_the_named_constant():
    """A new hire writing `max_tokens=800` again is the *exact* regression
    this test exists to catch — it must come through the constant."""
    src = inspect.getsource(filings._process_filing)
    assert "_FILINGS_LLM_MAX_TOKENS" in src
    # also rule out a hardcoded `max_tokens=<digits>` slipping in
    import re
    hits = re.findall(r"max_tokens\s*=\s*(\d+)", src)
    assert not hits, f"hardcoded max_tokens in _process_filing: {hits}"
