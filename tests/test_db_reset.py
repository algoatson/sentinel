"""Reset tooling contract.

`--reset` must be *reversible*: the bot's accumulated DB is its track
record (the whole monetization thesis rests on it). So `archive_database`
moves the sqlite set aside — it must never delete, must carry the WAL/SHM
siblings with it (a stray WAL replays into the fresh file), and must be a
no-op when there is no file to archive.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.db import _sqlite_file_path, archive_database


def test_sqlite_file_path_maps_file_url_and_ignores_rest():
    assert _sqlite_file_path("sqlite:///./data/radar.db") == Path(
        "./data/radar.db"
    )
    assert _sqlite_file_path("sqlite:////abs/x.db") == Path("/abs/x.db")
    assert _sqlite_file_path("sqlite:///:memory:") is None
    assert _sqlite_file_path("postgresql://h/db") is None
    assert _sqlite_file_path("sqlite:///") is None


def test_archive_moves_db_with_wal_and_shm(tmp_path):
    db = tmp_path / "radar.db"
    db.write_bytes(b"main")
    (tmp_path / "radar.db-wal").write_bytes(b"wal")
    (tmp_path / "radar.db-shm").write_bytes(b"shm")

    dest = archive_database(db_url=f"sqlite:///{db}")

    assert dest is not None and dest.exists()
    assert dest.parent == tmp_path / "backups"
    # original set is gone (moved, not copied) — fresh init can recreate it
    assert not db.exists()
    assert not (tmp_path / "radar.db-wal").exists()
    assert not (tmp_path / "radar.db-shm").exists()
    # siblings travelled under the same stem so the backup restores together
    assert dest.read_bytes() == b"main"
    assert dest.with_name(dest.name + "-wal").read_bytes() == b"wal"
    assert dest.with_name(dest.name + "-shm").read_bytes() == b"shm"


def test_archive_is_noop_when_nothing_to_archive(tmp_path):
    # no file yet → nothing to do (a first-ever run, or already reset)
    assert archive_database(db_url=f"sqlite:///{tmp_path}/absent.db") is None
    # in-memory / non-sqlite have no file to move
    assert archive_database(db_url="sqlite:///:memory:") is None
    assert archive_database(db_url="postgresql://h/db") is None
