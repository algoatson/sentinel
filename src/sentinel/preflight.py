"""Boot self-check — `python -m sentinel.main --preflight`.

Runs a focused battery of "would this bot actually work?" checks BEFORE
the scheduler arms. The goal is to catch the class of failure that
otherwise shows up 15 seconds into a live cycle: stale DB schema, wrong
LLM model id, unset channel id, malformed config YAML.

Each check returns a `CheckResult` with `(name, ok, severity, message,
ms_taken)`. The runner prints a table, then exits 0 (no criticals) or
1 (any critical fail). Warnings don't fail the run — they're for
"you'll regret this later" issues like an empty watchlist or a missing
optional channel.

Deliberately fast: < 5 seconds end-to-end on the Pi. Anything that
needs a real network round-trip (LLM ping) is allowed up to 8s before
it's reported as a timeout. Discord login isn't done here — the IDs
are checked structurally only, since a real connect costs ~5s and
risks racing the live scheduler if preflight is part of a restart loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
import yaml
from loguru import logger
from sqlmodel import select

from .config import CONFIG_DIR, settings


# ── result type ───────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str  # "critical" | "warning" | "info"
    message: str
    ms_taken: int = 0
    details: list[str] = field(default_factory=list)


# ── check helpers ─────────────────────────────────────────────────────────


def _timed(fn: Callable[[], CheckResult]) -> CheckResult:
    """Wrap a sync check so it records timing + catches any exception
    as a critical failure (an unhandled error in a check IS a failure)."""
    t0 = time.monotonic()
    try:
        r = fn()
    except Exception as e:
        r = CheckResult(
            name=getattr(fn, "__name__", "check"),
            ok=False, severity="critical",
            message=f"check raised: {type(e).__name__}: {e}",
        )
    r.ms_taken = int((time.monotonic() - t0) * 1000)
    return r


# ── individual checks ─────────────────────────────────────────────────────


def check_db_writable() -> CheckResult:
    """The DB path's parent must exist and be writable. A read-only mount
    is the classic Pi-deployment foot-gun (someone moved data/ to an
    SSD and forgot to chown)."""
    from .db import _sqlite_file_path, DB_URL
    db_path = _sqlite_file_path(DB_URL)
    if db_path is None:
        # in-memory / non-sqlite → nothing to check
        return CheckResult(
            "db_writable", True, "info",
            "DB URL is non-file (in-memory or postgres) — skipped",
        )
    parent = db_path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return CheckResult(
                "db_writable", False, "critical",
                f"cannot create DB parent {parent}: {e}",
            )
    # Probe with a tempfile in the parent
    probe = parent / ".preflight_write_probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return CheckResult(
            "db_writable", False, "critical",
            f"DB parent {parent} not writable: {e}",
        )
    return CheckResult(
        "db_writable", True, "info",
        f"OK — DB parent {parent} is writable",
    )


def check_db_init_and_schema() -> CheckResult:
    """`init_db()` must succeed AND every table declared in models.py
    must be present after it runs. A stale-schema Pi will pass init but
    fail the introspection because new tables (ResearchTask, CallSummary,
    NewsAnalysis) only appear if `metadata.create_all` ran against it."""
    from sqlalchemy import inspect
    from sqlmodel import SQLModel
    from .db import engine, init_db

    init_db()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    expected = set(SQLModel.metadata.tables.keys())
    missing = expected - existing
    if missing:
        return CheckResult(
            "db_schema", False, "critical",
            f"missing tables after init: {sorted(missing)}",
            details=[f"existing: {sorted(existing)}"],
        )
    return CheckResult(
        "db_schema", True, "info",
        f"OK — {len(existing)} tables present, all declared models accounted for",
    )


def check_yaml_configs() -> CheckResult:
    """Load every config/*.yaml. A malformed YAML manifests as a confusing
    AttributeError 30 seconds into the catalyst pipeline; catch it now."""
    root = Path(CONFIG_DIR)
    if not root.exists():
        return CheckResult(
            "yaml_configs", False, "critical",
            f"config dir missing: {root}",
        )
    bad: list[str] = []
    loaded: list[str] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as f:
                yaml.safe_load(f)
            loaded.append(path.name)
        except Exception as e:
            bad.append(f"{path.name}: {e}")
    if bad:
        return CheckResult(
            "yaml_configs", False, "critical",
            f"{len(bad)} YAML(s) failed to parse",
            details=bad,
        )
    return CheckResult(
        "yaml_configs", True, "info",
        f"OK — {len(loaded)} YAMLs load cleanly",
        details=loaded,
    )


def check_required_env() -> CheckResult:
    """The bot is useless without a Discord token. Other env vars are
    optional or have fallbacks; the token is the single hard requirement.
    EDGAR_USER_AGENT is required by SEC's fair-access rules — soft-warn
    if it's still the placeholder."""
    missing: list[str] = []
    warnings: list[str] = []
    if not settings.DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not settings.DISCORD_GUILD_ID:
        warnings.append("DISCORD_GUILD_ID is 0 — no startup ping target")
    ua = (settings.EDGAR_USER_AGENT or "").lower()
    if ("you@example.com" in ua or "example" in ua or not ua):
        warnings.append(
            "EDGAR_USER_AGENT looks like a placeholder — set your contact "
            "email per SEC fair-access (e.g. 'sentinel/0.1 you@email.com')"
        )
    if missing:
        return CheckResult(
            "env_required", False, "critical",
            f"missing env: {', '.join(missing)}",
            details=warnings,
        )
    if warnings:
        return CheckResult(
            "env_required", True, "warning",
            "required env present, but advisories below",
            details=warnings,
        )
    return CheckResult(
        "env_required", True, "info",
        "OK — required env vars set",
    )


def check_channel_ids() -> CheckResult:
    """Discord channel IDs must be 0 (= unset) or look like a valid
    snowflake (uint64, ~17-19 digits). Anything else is a typo or a
    user pasting a guild ID into a channel slot.

    We do NOT login to Discord here — that's a side-effect heavyweight
    op and preflight is supposed to be fast and idempotent."""
    fields = {
        "PRIORITY", "FILINGS", "INSIDERS", "PULSE", "DIGEST", "META",
        "NEWS", "CRYPTO", "GENERAL", "REDDIT", "CALLS", "RISK", "FUNDS",
        "HOT", "CONVERGENCE", "MACRO", "CATALYSTS",
    }
    bad: list[str] = []
    set_count = 0
    for f in fields:
        key = f"DISCORD_{f}_CHANNEL_ID"
        val = getattr(settings, key, 0)
        if val == 0:
            continue
        set_count += 1
        # Discord snowflakes are ≥17 digits and well below 2**63
        s = str(val)
        if not s.isdigit() or not (15 <= len(s) <= 20):
            bad.append(f"{key}={val} (not a valid Discord snowflake)")
    if bad:
        return CheckResult(
            "channel_ids", False, "critical",
            f"{len(bad)} channel id(s) look invalid",
            details=bad,
        )
    return CheckResult(
        "channel_ids", True, "info",
        f"OK — {set_count}/{len(fields)} channels configured, "
        "all well-formed snowflakes",
    )


def check_llm_ping() -> CheckResult:
    """Tiny ping completion per configured tier so we catch a wrong API
    key / unreachable model BEFORE the first real call. Both tiers fall
    back gracefully (light alone is fine; heavy alone with fallback_light
    is fine), so this is a warning, not a critical — except both-down is
    critical because the reasoning layer is dead."""
    from .llm import get_llm, _api_route

    light_ok = False
    heavy_ok = False
    notes: list[str] = []
    llm = get_llm()
    for tier in ("light", "heavy"):
        try:
            # max_tokens needs headroom for hidden-reasoning models (DeepSeek
            # V4 Flash, Qwen3 with thinking). A live test caught the
            # original 8 returning empty with `finish_reason=length`
            # because the model spent the budget reasoning before the
            # visible word ever emitted. 64 is still cheap (~$6e-6 per
            # ping at $0.10/1M) and gives reasoning models room to land
            # the answer.
            out = llm.complete(
                "Reply with one word: ok.",
                model=tier, max_tokens=64, grounded=False,
            )
        except Exception as e:
            notes.append(f"{tier}: raised {type(e).__name__}: {e}")
            continue
        route = _api_route(tier)
        where = (
            f"API {route[0]}@{route[2]}" if route else
            f"Ollama {settings.LLM_MODEL_LIGHT if tier == 'light' else settings.LLM_MODEL_HEAVY}"
        )
        if out and out != "[LLM_ERROR]":
            notes.append(f"{tier}: OK ({where}) → {out[:40]!r}")
            if tier == "light":
                light_ok = True
            else:
                heavy_ok = True
        else:
            notes.append(f"{tier}: returned {out!r} via {where}")
    if light_ok and heavy_ok:
        return CheckResult(
            "llm_ping", True, "info",
            "OK — both tiers responded",
            details=notes,
        )
    if light_ok or heavy_ok:
        return CheckResult(
            "llm_ping", True, "warning",
            "only one tier responded — bot can degrade with fallback_light",
            details=notes,
        )
    return CheckResult(
        "llm_ping", False, "critical",
        "BOTH LLM tiers failed — reasoning layer is dead, "
        "check OLLAMA_BASE_URL / LLM_API_*",
        details=notes,
    )


def check_watchlist_seeded() -> CheckResult:
    """An empty watchlist means no movers / news / filings / catalysts
    light up. Soft warn — the bot can technically run, but it's useless
    until someone `!watch <ticker>`'s things in."""
    from .db import session_scope
    from .models import Watchlist
    with session_scope() as s:
        n = len(s.exec(select(Watchlist)).all())
    if n == 0:
        return CheckResult(
            "watchlist", True, "warning",
            "watchlist is empty — the bot will boot but nothing will "
            "trigger. Add tickers with `!add <ticker>` or wait for the "
            "weekly rebuild from `tracked_entities.yaml`.",
        )
    return CheckResult(
        "watchlist", True, "info",
        f"OK — {n} tickers in the watchlist",
    )


def check_dashboard_port_free() -> CheckResult:
    """Don't try to bind to the configured dashboard port if it's already
    in use — uvicorn would crash a few seconds in. Quick TCP probe to
    DASHBOARD_HOST:DASHBOARD_PORT; if something responds, warn loudly
    (could be a stale sentinel process from a botched restart)."""
    if not settings.DASHBOARD_ENABLED:
        return CheckResult(
            "dashboard_port", True, "info",
            "dashboard disabled — port check skipped",
        )
    host = settings.DASHBOARD_HOST or "127.0.0.1"
    port = settings.DASHBOARD_PORT
    # Probe via httpx so an HTTP-200 (someone else's dashboard) is
    # distinguishable from "nothing listening" (connect error).
    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        with httpx.Client(timeout=1.5) as client:
            client.get(f"http://{probe_host}:{port}/")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return CheckResult(
            "dashboard_port", True, "info",
            f"OK — {host}:{port} is free to bind",
        )
    except Exception as e:
        return CheckResult(
            "dashboard_port", True, "warning",
            f"port probe inconclusive: {e}",
        )
    return CheckResult(
        "dashboard_port", False, "critical",
        f"{host}:{port} already has a server listening — kill the "
        "previous sentinel (or change DASHBOARD_PORT) before starting",
    )


# ── runner ────────────────────────────────────────────────────────────────


# Order matters here: cheap checks first so a failure shows up fast and
# blocks the more expensive LLM ping that wouldn't help debug the cheap
# failure anyway. The LLM ping IS the slowest by a wide margin.
_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    check_required_env,
    check_db_writable,
    check_db_init_and_schema,
    check_yaml_configs,
    check_channel_ids,
    check_dashboard_port_free,
    check_watchlist_seeded,
    check_llm_ping,
)


def run_all() -> tuple[list[CheckResult], int]:
    """Run every check in order. Returns `(results, exit_code)` —
    exit_code is 0 iff no `severity=='critical'` result has `ok=False`.
    Warnings never block boot."""
    results: list[CheckResult] = []
    for fn in _CHECKS:
        results.append(_timed(fn))
    code = 0 if all(
        r.ok or r.severity != "critical" for r in results
    ) else 1
    return results, code


def print_report(results: list[CheckResult]) -> None:
    """Pretty-print the preflight table to stderr-via-loguru. The format
    is intentionally grep-friendly: `[PASS|WARN|FAIL] name (ms) — message`
    so a watchdog can pipe and pattern-match."""
    logger.info("─── preflight ──────────────────────────────────────────")
    n_pass = n_warn = n_fail = 0
    for r in results:
        if not r.ok and r.severity == "critical":
            tag, fn = "FAIL", logger.error
            n_fail += 1
        elif r.severity == "warning" or not r.ok:
            tag, fn = "WARN", logger.warning
            n_warn += 1
        else:
            tag, fn = "PASS", logger.info
            n_pass += 1
        fn("  [{:4}] {:24} ({:>4}ms) — {}",
           tag, r.name, r.ms_taken, r.message)
        for line in r.details:
            logger.debug("           {}", line)
    logger.info("─── {} pass · {} warn · {} fail ──────────────────────",
                n_pass, n_warn, n_fail)
