"""Point the DB to a tmpfile before any sentinel imports run.

Setting this env var at module-load time (before the first test imports
sentinel.db) is what lets us use a fresh sqlite per-process without
touching the real data/radar.db.
"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="sentinel-test-")
os.environ["SENTINEL_DB_URL"] = f"sqlite:///{_tmpdir}/test.db"

import pytest  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from sentinel.db import engine  # noqa: E402
import sentinel.models  # noqa: F401, E402 — registers tables on import


@pytest.fixture(autouse=True)
def fresh_db():
    """Drop and recreate all tables before every test."""
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield
