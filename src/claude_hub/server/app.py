"""Application factory."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import db, ingest, views


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    data_path = Path(data_dir or os.environ.get("HUB_DATA_DIR", "./data")).resolve()
    db.init(data_path)

    app = FastAPI(title="Claude hub", version=__version__, docs_url=None, redoc_url=None)
    app.state.data_dir = data_path
    rulesets = os.environ.get("HUB_RULESETS_DIR")
    app.state.rulesets_dir = Path(rulesets).resolve() if rulesets else Path.cwd() / "rulesets"
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(ingest.router)
    app.include_router(views.router)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    password = os.environ.get("HUB_UI_PASSWORD")
    if password:
        user = os.environ.get("HUB_UI_USER", "hub")

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            # Collectors authenticate with their own tokens, so the API is exempt.
            path = request.url.path
            if path.startswith("/api/") or path == "/healthz":
                return await call_next(request)
            scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "basic":
                try:
                    given_user, _, given_password = base64.b64decode(encoded).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    given_user = given_password = ""
                if secrets.compare_digest(given_user, user) and secrets.compare_digest(
                    given_password, password
                ):
                    return await call_next(request)
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Claude hub"'})

    return app
