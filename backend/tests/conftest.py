"""Shared test fixtures.

API tests need a real PostgreSQL because the schema uses JSONB and UUID[].
They run against ``TEST_DATABASE_URL`` (default: the dev database with a
``_test`` suffix), which is created on demand. If no server is reachable the
database tests skip with an explanatory message rather than failing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Base

SKIP_REASON = (
    "PostgreSQL is not reachable at {url}. Start it with "
    "`docker compose up -d postgres` from the repo root."
)


def _test_database_url() -> str:
    configured = os.getenv("TEST_DATABASE_URL")
    if configured:
        return configured
    url = make_url(settings.database_url)
    # str(URL) masks the password as '***' -- render it in full or the
    # connection silently authenticates with the literal mask.
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _ensure_database(url_str: str) -> None:
    """Create the test database if the server is up and it does not exist."""
    url = make_url(url_str)
    admin_url = url.set(database="postgres").render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = _test_database_url()
    try:
        _ensure_database(url)
        eng = create_engine(url, future=True, pool_pre_ping=True)
        with eng.connect():
            pass
    except OperationalError:
        pytest.skip(SKIP_REASON.format(url=make_url(url).render_as_string(hide_password=True)))
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def client(db_session: Session):
    """TestClient wired to the test database (lifespan is not run)."""
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
