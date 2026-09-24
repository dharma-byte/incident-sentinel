"""Settings tests (Phase 6).

The deploy path hands us a database URL we do not control, so the normalisation
that makes it usable is worth pinning down.
"""

from __future__ import annotations

import pytest

from app.config import Settings

PSYCOPG = "postgresql+psycopg://"


@pytest.mark.parametrize(
    "given",
    [
        # What Render's `fromDatabase: connectionString` actually hands over.
        "postgres://sentinel:secret@dpg-abc123.oregon-postgres.render.com/incident_sentinel",
        # The other spelling managed providers use.
        "postgresql://sentinel:secret@dpg-abc123.oregon-postgres.render.com/incident_sentinel",
    ],
)
def test_hosted_database_urls_are_rewritten_for_psycopg3(given: str) -> None:
    """SQLAlchemy 2.0 rejects `postgres://`, and `postgresql://` wants psycopg2."""
    url = Settings(database_url=given).database_url

    assert url.startswith(PSYCOPG)
    # Everything after the scheme must survive untouched -- credentials, host
    # and database name all matter.
    assert url[len(PSYCOPG) :] == given.split("://", 1)[1]


def test_explicit_driver_is_left_alone() -> None:
    given = f"{PSYCOPG}sentinel:sentinel@localhost:5432/incident_sentinel"

    assert Settings(database_url=given).database_url == given


def test_cors_origins_are_split_and_stripped() -> None:
    settings = Settings(cors_origins="https://example.vercel.app, http://localhost:5173 , ")

    assert settings.cors_origin_list == [
        "https://example.vercel.app",
        "http://localhost:5173",
    ]
