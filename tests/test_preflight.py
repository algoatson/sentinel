"""Preflight contracts.

`--preflight` is meant to catch boot-time misconfigurations BEFORE the
scheduler arms — wrong channel id, stale DB schema, malformed YAML,
unreachable LLM. These tests pin:

- Each check returns a `CheckResult` with the right severity for the
  failure mode it's claiming to detect.
- `_timed` swallows exceptions as critical so a buggy check can't bring
  down the runner.
- `run_all` exits non-zero iff a critical-severity result is failing
  (warnings never block).
- The report format is grep-friendly (a watchdog can pattern-match).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from sentinel import preflight


# ── _timed / runner mechanics ─────────────────────────────────────────────


def test_timed_wraps_exception_as_critical():
    def boom():
        raise RuntimeError("synthetic boom")
    r = preflight._timed(boom)
    assert not r.ok
    assert r.severity == "critical"
    assert "boom" in r.message
    assert r.ms_taken >= 0


def test_run_all_exit_code_blocks_only_on_critical(monkeypatch):
    """Warnings never block, criticals do."""
    def warn_only():
        return preflight.CheckResult("a", False, "warning", "just a warning")
    def crit_pass():
        return preflight.CheckResult("b", True, "critical", "ok")

    monkeypatch.setattr(preflight, "_CHECKS", (warn_only, crit_pass))
    _, code = preflight.run_all()
    assert code == 0  # only a warning failed

    def crit_fail():
        return preflight.CheckResult("c", False, "critical", "boom")
    monkeypatch.setattr(preflight, "_CHECKS", (warn_only, crit_fail))
    _, code = preflight.run_all()
    assert code == 1


def test_print_report_uses_grep_friendly_format():
    """`[PASS|WARN|FAIL] name (ms) — message` so a watchdog can pipe and
    pattern-match. Locked here so future log-format swaps don't silently
    break operators downstream.

    Loguru bypasses stdlib logging so `caplog` doesn't see its output;
    install a temporary sink that appends to a list and assert against
    that. Remove the sink by id so other tests' sinks aren't disturbed."""
    from loguru import logger
    captured: list[str] = []
    sink_id = logger.add(
        lambda msg: captured.append(str(msg)),
        format="{message}", level="DEBUG",
    )
    try:
        results = [
            preflight.CheckResult("pass_a", True, "info", "ok msg"),
            preflight.CheckResult("warn_a", False, "warning", "warn msg"),
            preflight.CheckResult("fail_a", False, "critical", "fail msg"),
        ]
        preflight.print_report(results)
    finally:
        logger.remove(sink_id)
    text = "\n".join(captured)
    assert "[PASS]" in text and "pass_a" in text
    assert "[WARN]" in text and "warn_a" in text
    assert "[FAIL]" in text and "fail_a" in text
    assert "1 pass · 1 warn · 1 fail" in text


# ── check_required_env ────────────────────────────────────────────────────


def test_required_env_flags_missing_token_as_critical(monkeypatch):
    from sentinel.config import settings
    monkeypatch.setattr(settings, "DISCORD_TOKEN", "")
    r = preflight.check_required_env()
    assert not r.ok
    assert r.severity == "critical"
    assert "DISCORD_TOKEN" in r.message


def test_required_env_warns_on_placeholder_ua(monkeypatch):
    from sentinel.config import settings
    monkeypatch.setattr(settings, "DISCORD_TOKEN", "TEST.TOKEN.HERE")
    monkeypatch.setattr(
        settings, "EDGAR_USER_AGENT", "sentinel/0.1 you@example.com",
    )
    r = preflight.check_required_env()
    # token IS present, so the check passes, but advises about the UA
    assert r.ok
    assert r.severity == "warning"
    assert any("EDGAR_USER_AGENT" in d for d in r.details)


def test_required_env_passes_clean(monkeypatch):
    from sentinel.config import settings
    monkeypatch.setattr(settings, "DISCORD_TOKEN", "TEST.TOKEN.HERE")
    monkeypatch.setattr(settings, "DISCORD_GUILD_ID", 1234567890)
    monkeypatch.setattr(
        settings, "EDGAR_USER_AGENT", "sentinel/0.1 real@person.com",
    )
    r = preflight.check_required_env()
    assert r.ok
    assert r.severity == "info"


# ── check_yaml_configs ────────────────────────────────────────────────────


def test_yaml_configs_flags_malformed_file_as_critical(
    tmp_path, monkeypatch
):
    """A malformed YAML manifests as a confusing AttributeError 30s into
    the catalyst pipeline; preflight should fail loud at boot instead."""
    good = tmp_path / "good.yaml"
    good.write_text("a: 1\nb: 2")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: : : : :\n  - not yaml\n[broken")
    monkeypatch.setattr(preflight, "CONFIG_DIR", tmp_path)
    r = preflight.check_yaml_configs()
    assert not r.ok
    assert r.severity == "critical"
    assert any("bad.yaml" in d for d in r.details)


def test_yaml_configs_passes_when_dir_empty(tmp_path, monkeypatch):
    """An empty config dir is fine (the bot can still boot — it just
    has no tracked entities)."""
    monkeypatch.setattr(preflight, "CONFIG_DIR", tmp_path)
    r = preflight.check_yaml_configs()
    assert r.ok
    assert "0 YAMLs" in r.message


def test_yaml_configs_flags_missing_dir(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist"
    monkeypatch.setattr(preflight, "CONFIG_DIR", missing)
    r = preflight.check_yaml_configs()
    assert not r.ok
    assert r.severity == "critical"
    assert "missing" in r.message.lower()


# ── check_channel_ids ─────────────────────────────────────────────────────


def test_channel_ids_passes_when_all_unset(monkeypatch):
    """Zero in every slot means the channel just isn't used — bot
    degrades cleanly. Should never be critical."""
    from sentinel.config import settings
    for f in ("PRIORITY", "FILINGS", "INSIDERS", "PULSE", "DIGEST", "META",
              "NEWS", "CRYPTO", "GENERAL", "REDDIT", "CALLS", "RISK",
              "FUNDS", "HOT", "CONVERGENCE", "MACRO", "CATALYSTS"):
        monkeypatch.setattr(settings, f"DISCORD_{f}_CHANNEL_ID", 0)
    r = preflight.check_channel_ids()
    assert r.ok
    assert "0/17" in r.message


def test_channel_ids_flags_garbage_value_as_critical(monkeypatch):
    """A typo'd channel id (paste of a guild id, paste of a username
    digit-cluster) won't be a real snowflake — catch it."""
    from sentinel.config import settings
    monkeypatch.setattr(settings, "DISCORD_FILINGS_CHANNEL_ID", 42)
    r = preflight.check_channel_ids()
    assert not r.ok
    assert r.severity == "critical"
    assert any("DISCORD_FILINGS_CHANNEL_ID" in d for d in r.details)


def test_channel_ids_accepts_real_snowflake(monkeypatch):
    from sentinel.config import settings
    # Wipe all then set one to a real-shaped snowflake
    for f in ("PRIORITY", "FILINGS", "INSIDERS", "PULSE", "DIGEST", "META",
              "NEWS", "CRYPTO", "GENERAL", "REDDIT", "CALLS", "RISK",
              "FUNDS", "HOT", "CONVERGENCE", "MACRO", "CATALYSTS"):
        monkeypatch.setattr(settings, f"DISCORD_{f}_CHANNEL_ID", 0)
    monkeypatch.setattr(
        settings, "DISCORD_FILINGS_CHANNEL_ID", 1503345390131351683,
    )
    r = preflight.check_channel_ids()
    assert r.ok
    assert "1/17" in r.message


# ── check_db_writable ─────────────────────────────────────────────────────


def test_db_writable_creates_missing_parent(tmp_path, monkeypatch):
    from sentinel import db as db_mod
    target = tmp_path / "nested" / "deeper" / "radar.db"
    monkeypatch.setattr(db_mod, "DB_URL", f"sqlite:///{target}")
    r = preflight.check_db_writable()
    assert r.ok
    assert target.parent.exists()  # was auto-created


def test_db_writable_passes_for_non_file_url(monkeypatch):
    from sentinel import db as db_mod
    monkeypatch.setattr(db_mod, "DB_URL", "sqlite:///:memory:")
    r = preflight.check_db_writable()
    assert r.ok
    assert r.severity == "info"
    assert "skipped" in r.message.lower()


# ── shipped end-to-end smoke ──────────────────────────────────────────────


def test_run_all_returns_results_for_every_check(monkeypatch):
    """The runner must execute every check and return a result for each
    — a silent skip would create a hole an operator can't see."""
    expected = len(preflight._CHECKS)
    # Stub LLM/DB heavy checks to avoid network during CI runs
    monkeypatch.setattr(
        preflight, "check_llm_ping",
        lambda: preflight.CheckResult("llm_ping", True, "info", "stubbed"),
    )
    results, _ = preflight.run_all()
    assert len(results) == expected


def test_shipped_anchor_yaml_validates():
    """The world_anchor.yaml that ships with the repo must round-trip
    through `check_yaml_configs` — a future hand-edit can break it."""
    r = preflight.check_yaml_configs()
    # The shipped configs include world_anchor.yaml; if any YAML in
    # config/ is bad, this fails loud and we catch it in CI
    assert r.ok, f"shipped YAML broken: {r.message} — {r.details}"
