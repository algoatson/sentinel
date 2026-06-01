"""Grounding-preamble contract.

The preamble is what stops the LLM from dismissing 2026 news as fake on
a 2024 prior — and it ships on EVERY reasoning call. So the contract
matters:

- Today's date is always in the block (otherwise the "trust the data"
  framing has nothing to anchor on).
- The trust-rules paragraph is always present, even when the YAML is
  missing or broken (the rules are the load-bearing part; the anchor
  is a nice-to-have).
- `prepend` is idempotent so the heavy→light fallback path doesn't
  stack the block twice.
- The cache returns the same body for ~5 min, then re-reads.
- `LLM.complete(grounded=True)` calls `prepend` exactly once.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

from sentinel import grounding


# ── block() basics ────────────────────────────────────────────────────────


def test_block_always_includes_today_and_trust_rules():
    grounding.reset_cache()
    body = grounding.block()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert today in body
    # the trust-rules paragraph is load-bearing — its key phrases must ship
    assert "BELIEVE THE DATA" in body
    assert "training" in body.lower()


def test_block_is_cached_between_close_calls(monkeypatch):
    grounding.reset_cache()
    a = grounding.block()
    # Make `_build_block` blow up — if the cache is honoured, the next
    # call still returns the cached string and never calls the builder.
    def _boom():
        raise AssertionError("cache miss")
    monkeypatch.setattr(grounding, "_build_block", _boom)
    b = grounding.block()
    assert a == b


def test_block_truncates_when_anchor_is_huge(tmp_path, monkeypatch):
    """An over-padded YAML must not silently inflate every prompt — the
    truncation cap is part of the contract."""
    grounding.reset_cache()
    huge = tmp_path / "world_anchor.yaml"
    huge.write_text(
        "us_government:\n"
        "  president: " + "X" * 4000 + "\n"
    )
    monkeypatch.setattr(grounding, "CONFIG_DIR", tmp_path)
    body = grounding.block()
    assert len(body) <= grounding._MAX_BLOCK_CHARS
    assert "truncated" in body


# ── prepend() behaviour ───────────────────────────────────────────────────


def test_prepend_keeps_original_prompt_intact():
    grounding.reset_cache()
    out = grounding.prepend("Summarise this filing in 3 sentences.")
    assert "Summarise this filing in 3 sentences." in out
    # the separator is the contract for "grounding ends here, task begins"
    assert "\n---\n" in out


def test_prepend_is_idempotent():
    """The heavy→light fallback path re-uses the already-grounded prompt;
    a second `prepend` must NOT stack the block twice."""
    grounding.reset_cache()
    once = grounding.prepend("do X")
    twice = grounding.prepend(once)
    assert once == twice


def test_prepend_handles_empty_prompt():
    assert grounding.prepend("") == ""


# ── degradation when YAML is missing or malformed ─────────────────────────


def test_missing_anchor_yields_minimum_preamble(tmp_path, monkeypatch):
    grounding.reset_cache()
    # CONFIG_DIR pointing at an empty dir → no world_anchor.yaml
    monkeypatch.setattr(grounding, "CONFIG_DIR", tmp_path)
    body = grounding.block()
    # still has today + trust rules…
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert today in body
    assert "BELIEVE THE DATA" in body
    # …but no anchor section header
    assert "WORLD STATE ANCHOR" not in body


def test_malformed_yaml_falls_through_to_minimum(tmp_path, monkeypatch):
    grounding.reset_cache()
    bad = tmp_path / "world_anchor.yaml"
    bad.write_text("us_government: : : : :\n  : not yaml")
    monkeypatch.setattr(grounding, "CONFIG_DIR", tmp_path)
    body = grounding.block()
    assert "BELIEVE THE DATA" in body
    assert "WORLD STATE ANCHOR" not in body


def test_valid_anchor_renders_facts(tmp_path, monkeypatch):
    grounding.reset_cache()
    yaml_path = tmp_path / "world_anchor.yaml"
    yaml_path.write_text(textwrap.dedent("""
        us_government:
          president: "Test President"
          vice_president: "Test VP"
        themes:
          - "Test theme one."
          - "Test theme two."
    """).strip())
    monkeypatch.setattr(grounding, "CONFIG_DIR", tmp_path)
    body = grounding.block()
    assert "Test President" in body
    assert "Test VP" in body
    assert "Test theme one" in body
    assert "Test theme two" in body
    assert "WORLD STATE ANCHOR" in body


def test_shipped_anchor_has_no_obviously_stale_facts():
    """Smoke against the actual `config/world_anchor.yaml`: rule is "only
    facts I'd defend today". This pins the *kind* of content — anything
    explicitly Biden-era should fail loud so a careless future edit gets
    caught."""
    grounding.reset_cache()
    body = grounding.block()
    # If this fails, you put a Biden-era fact back into world_anchor.yaml
    # — and the whole point of this preamble was to defeat that prior.
    assert "Biden" not in body, (
        "world_anchor.yaml shipped a Biden-era fact — remove or rephrase"
    )


# ── LLM injection contract ────────────────────────────────────────────────


def test_llm_complete_prepends_grounding_by_default(monkeypatch):
    """The whole point of `grounded=True` defaulting: every LLM call gets
    the preamble without each caller having to remember to ask for it."""
    from sentinel import llm
    grounding.reset_cache()

    seen: list[str] = []

    def _fake_once(self, prompt, *, model, json_mode=False, max_tokens=800,
                   reasoning=None):
        seen.append(prompt)
        return "ok"

    monkeypatch.setattr(llm.LLM, "_complete_once", _fake_once)
    monkeypatch.setattr(llm, "_singleton", llm.LLM.__new__(llm.LLM))
    llm._singleton.client = None  # avoid Ollama touch

    out = llm.get_llm().complete("plain prompt", model="light")
    assert out == "ok"
    assert seen, "complete() never invoked the inner call"
    # the grounding preamble is on the front; the original prompt is
    # below the separator
    assert "TODAY:" in seen[0]
    assert "BELIEVE THE DATA" in seen[0]
    assert "plain prompt" in seen[0]
    assert seen[0].endswith("plain prompt")


def test_llm_complete_grounded_false_skips_preamble(monkeypatch):
    from sentinel import llm
    grounding.reset_cache()

    seen: list[str] = []

    def _fake_once(self, prompt, *, model, json_mode=False, max_tokens=800,
                   reasoning=None):
        seen.append(prompt)
        return "ok"

    monkeypatch.setattr(llm.LLM, "_complete_once", _fake_once)
    monkeypatch.setattr(llm, "_singleton", llm.LLM.__new__(llm.LLM))
    llm._singleton.client = None

    llm.get_llm().complete("raw prompt", model="light", grounded=False)
    assert "TODAY:" not in seen[0]
    assert seen[0] == "raw prompt"


def test_path_to_real_world_anchor_yaml_is_resolvable():
    """The shipped YAML must be where `grounding._load_anchor` looks — a
    typo here would degrade every reasoning call silently to "no anchor"."""
    grounding.reset_cache()
    target = Path(grounding.CONFIG_DIR) / "world_anchor.yaml"
    assert target.exists(), f"expected world anchor at {target}"
