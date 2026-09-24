"""FastAPI entrypoint for Incident Sentinel."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health, incidents, simulate
from app.config import settings
from app.db.session import init_db

logger = logging.getLogger("incident_sentinel")

DESCRIPTION = (
    "Multi-agent infrastructure incident triage. Simulate an incident, run the agent "
    "pipeline over it, and read back the full reasoning trace."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
        logger.info("database schema ready")
    except Exception as exc:  # the API should still boot so /health can report it
        logger.warning("could not initialise the database: %s", exc)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Incident Sentinel",
        description=DESCRIPTION,
        version="0.2.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(simulate.router)
    app.include_router(incidents.router)

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, object]:
        """Signpost, so a bare / is not a bewildering 404."""
        return {
            "service": app.title,
            "version": app.version,
            "docs": "/docs",
            "endpoints": [
                "GET  /health",
                "GET  /scenarios",
                "POST /simulate",
                "GET  /incidents",
                "GET  /incidents/{id}",
                "GET  /incidents/{id}/trace",
                "GET  /incidents/{id}/logs",
                "GET  /incidents/{id}/metrics",
            ],
        }

    return app


app = create_app()
