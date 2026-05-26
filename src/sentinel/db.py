"""Database engine + session_scope + init_db.

DB URL is configurable via the SENTINEL_DB_URL env var (FILING_RADAR_DB_URL still honoured) so tests can point
at a tmpfile without touching the real data/radar.db.
"""

import os
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine


DB_URL = os.environ.get("SENTINEL_DB_URL") or os.environ.get("FILING_RADAR_DB_URL", "sqlite:///./data/radar.db")
_IS_SQLITE = DB_URL.startswith("sqlite")

engine = create_engine(
    DB_URL,
    echo=False,
    # `timeout` is pysqlite's busy timeout (seconds): a writer waits this long
    # for the lock instead of raising "database is locked" the instant it's
    # contended. Essential here — many pipelines write concurrently from
    # to_thread workers and the price poller holds a long write transaction.
    connect_args=(
        # 60s (was 30): a slow disk — notably a Raspberry Pi SD card — can
        # hold the write lock long enough under a poller burst to exceed a
        # 30s wait. Paired with scheduler jitter (which thins the burst) and
        # busy_timeout below.
        {"check_same_thread": False, "timeout": 60} if _IS_SQLITE else {}
    ),
)


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        """Apply concurrency-safe pragmas to EVERY pooled connection (one per
        worker thread), not just the one init_db touched. WAL lets readers
        not block writers; busy_timeout makes a contended writer queue for up
        to 30s instead of dropping the write (which silently lost reddit
        mentions + JobRun rows under load); synchronous=NORMAL keeps WAL
        commit throughput sane.
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=60000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


@contextmanager
def session_scope():
    """Yield a transactional Session. Commits on success, rolls back on error.

    `expire_on_commit=False` keeps already-loaded attributes accessible after
    the session closes — so callers can build response payloads outside the
    `with` block without hitting DetachedInstanceError.
    """
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _sqlite_file_path(db_url: str) -> "Path | None":
    """Filesystem Path of a sqlite *file* URL, else None (memory / non-sqlite).

    Pure so it can be unit-tested without touching a real DB (same
    convention as sysinfo._db_path).
    """
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return None
    raw = db_url[len(prefix):]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw)


def archive_database(*, db_url: str | None = None) -> "Path | None":
    """Move the live sqlite DB (+ its -wal/-shm siblings) into data/backups/
    so the next init_db() starts from an empty schema.

    Reversible by design: the bot's accumulated history (calls, wallets,
    scorecard) is its most valuable asset, so a "reset" *archives*, never
    deletes — a backup can be moved back to data/radar.db to restore. Returns
    the backup path, or None when there's nothing to archive (memory /
    non-sqlite / no file yet). The caller must ensure no process is holding
    the DB open before calling this.
    """
    import shutil
    from datetime import datetime, timezone

    path = _sqlite_file_path(db_url or DB_URL)
    if path is None or not path.exists():
        return None
    backups = path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backups / f"{path.stem}-{stamp}{path.suffix}"
    shutil.move(str(path), str(dest))
    # Carry the WAL/SHM siblings under the same stem so the set restores
    # together; if they lingered, sqlite would replay them into the new file.
    for suffix in ("-wal", "-shm"):
        sib = path.with_name(path.name + suffix)
        if sib.exists():
            shutil.move(str(sib), str(dest.with_name(dest.name + suffix)))
    logger.info("database archived → {}", dest)
    return dest


def init_db() -> None:
    """Create all tables, enable WAL, seed prompts. Idempotent."""
    from .config import PROJECT_ROOT

    if DB_URL.startswith("sqlite:///"):
        path_part = DB_URL.replace("sqlite:///", "", 1)
        if path_part and not path_part.startswith(":memory:"):
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)

    from . import models  # noqa: F401 — ensure model registration

    SQLModel.metadata.create_all(engine)

    if DB_URL.startswith("sqlite"):
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()

        # Inline migrations — additive only. SQLModel.create_all does NOT
        # alter columns on existing tables, so we ADD COLUMN if missing.
        _migrate_add_columns(
            "newsitem",
            [
                ("price_at_publish", "FLOAT"),
                ("impact_1h_pct", "FLOAT"),
                ("impact_1d_pct", "FLOAT"),
                ("impact_tagged_at", "DATETIME"),
                ("alerted_at", "DATETIME"),
                # Comma-joined extra-tickers column for multi-ticker stories.
                # Format ",NVDA,AMD," — LIKE '%,X,%' is safe substring search.
                ("tickers_csv", "VARCHAR"),
            ],
        )
        _migrate_add_columns(
            "watchlist",
            [("asset_class", "VARCHAR DEFAULT 'equity'")],
        )
        _migrate_add_columns(
            "redditmention",
            [("posted_at", "DATETIME")],
        )
        _migrate_add_columns(
            "tradingcall",
            [("resolved_posted_at", "DATETIME")],
        )
        # Per-position risk management + journal — set by user via
        # the /book UI. Existing rows have NULL on every field which
        # is the documented "no auto-exit, no trailing, no notes"
        # default.
        _migrate_add_columns(
            "fundtrade",
            [
                ("stop_price", "FLOAT"),
                ("target_price", "FLOAT"),
                ("trailing_stop_pct", "FLOAT"),
                ("watermark_price", "FLOAT"),
                ("notes", "VARCHAR"),
            ],
        )

    from .funds import seed_funds
    from .prompts import seed_prompts

    seed_prompts()
    seed_funds()
    logger.info("DB initialized at {}", DB_URL)


def _migrate_add_columns(table: str, columns: list[tuple[str, str]]) -> None:
    """For SQLite, ADD COLUMN any of the given (name, type) pairs that don't
    yet exist on `table`. Idempotent and safe across restarts.
    """
    with engine.connect() as conn:
        for col_name, col_type in columns:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
                conn.commit()
                logger.info("migration: added column {}.{}", table, col_name)
            except OperationalError as e:
                # "duplicate column name" → already present, fine.
                msg = str(e).lower()
                if "duplicate column" not in msg and "already exists" not in msg:
                    logger.warning("migration {}.{} skipped: {}", table, col_name, e)
