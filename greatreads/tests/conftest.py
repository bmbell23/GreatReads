"""Shared pytest fixtures for the unified GreatReads app.

Isolation: the app reads its DB URL (DATABASE_URL) and ereader data dir
(EREADER_DATA_DIR) from the environment at IMPORT time, so they are set here,
before anything imports `greatreads`, and point at a throwaway temp dir. The
production DB is never touched. Background schedulers / the Calibre watcher are
disabled via ENABLE_SCHEDULERS=false (read by the app lifespan).
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="greatreads-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["EREADER_DATA_DIR"] = os.path.join(_TMP, "ereader-data")
os.environ["ENABLE_SCHEDULERS"] = "false"
# Point external services at dead local ports so nothing real is ever contacted.
os.environ["CALIBRE_URL"] = "http://127.0.0.1:9"
os.environ["CALIBRE_DB_PATH"] = os.path.join(_TMP, "no-calibre.db")
os.environ["ABS_DB_PATH"] = os.path.join(_TMP, "no-abs.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from greatreads.config import settings  # noqa: E402
from greatreads.database import Base, SessionLocal, engine, create_tables  # noqa: E402
from greatreads.main import app  # noqa: E402
from greatreads.models.user import User  # noqa: E402


def pytest_sessionstart(session):
    # Guard: refuse to run if the engine somehow isn't the temp DB.
    assert str(engine.url).startswith("sqlite:///" + _TMP), engine.url
    assert "projects/GreatReads" not in settings.database_url


@pytest.fixture()
def db():
    """Fresh schema + the default 'brandon' user (auth falls back to it) per test."""
    Base.metadata.drop_all(bind=engine)
    create_tables()
    session = SessionLocal()
    session.add(User(username="brandon", email="b@example.test", password_hash="x"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    """TestClient on the real app (lifespan runs with schedulers disabled)."""
    with TestClient(app) as c:
        yield c
