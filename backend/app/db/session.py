"""Engine, session factory and schema bootstrap.

Schema creation is plain ``Base.metadata.create_all`` -- no migration tool for
a demo repo. Create the tables with::

    python -m app.db.session --init
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Base

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create any missing tables."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session for use outside a request (scripts, agents)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection() -> bool:
    """True when the database answers a trivial query -- used by /health."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - operational entrypoint
    parser = argparse.ArgumentParser(description="Database maintenance for Incident Sentinel.")
    parser.add_argument("--init", action="store_true", help="create missing tables")
    parser.add_argument("--drop", action="store_true", help="DROP every table (destructive)")
    args = parser.parse_args(argv)

    masked = settings.database_url.split("@")[-1]
    if args.drop:
        confirm = input(f"Drop all Incident Sentinel tables on {masked}? [y/N] ")
        if confirm.lower() != "y":
            print("aborted")
            return 1
        Base.metadata.drop_all(bind=engine)
        print(f"dropped {len(Base.metadata.tables)} tables on {masked}")
    if args.init or not args.drop:
        init_db()
        print(f"schema ready on {masked}: {', '.join(sorted(Base.metadata.tables))}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
