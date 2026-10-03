"""Application factory."""

from __future__ import annotations

import base64
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..rulesets import read_folder
from . import calendar_views, db, google, google_api, hub_api, ingest, more_views, routines, views


def drop_shipped_edits(shipped: Path, live: Path) -> None:
    """Remove live rule sets that the repository now holds with the same text."""
    for name, text in read_folder(live).items():
        if read_folder(shipped).get(name) == text:
            (live / f"{name}.md").unlink(missing_ok=True)


def routine_clock(app: FastAPI, stop: threading.Event, every: float) -> None:
    """Send the mails of due routines that are set to send by themselves, until told to stop."""
    # The first look comes after a minute, so that a restart does not send at once.
    wait = min(60.0, every)
    while not stop.wait(wait):
        wait = every
        try:
            conn = db.connect(app.state.data_dir)
            try:
                moment = datetime.now(google.zone(app.state.google.time_zone))
                for line in routines.tick(conn, app.state.google, moment):
                    print(f"routine clock: {line}", flush=True)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - the clock must outlive any single failure
            print(f"routine clock: {exc}", flush=True)


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    data_path = Path(data_dir or os.environ.get("HUB_DATA_DIR", "./data")).resolve()
    db.init(data_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Routines that send their mail by themselves need a clock. A thread
        # looks at them every few minutes for as long as the server runs.
        stop = threading.Event()
        every = float(os.environ.get("HUB_TICK_SECONDS", "600"))
        worker = None
        if every > 0:
            worker = threading.Thread(target=routine_clock, args=(app, stop, every), daemon=True)
            worker.start()
        yield
        stop.set()
        if worker is not None:
            worker.join(timeout=5)

    app = FastAPI(title="Claude hub", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.data_dir = data_path
    rulesets = os.environ.get("HUB_RULESETS_DIR")
    app.state.rulesets_dir = Path(rulesets).resolve() if rulesets else Path.cwd() / "rulesets"
    # Rule sets that were changed in the hub. They win over the repository
    # files until a deployment brings the same text.
    app.state.live_rulesets_dir = data_path / "rulesets"
    drop_shipped_edits(app.state.rulesets_dir, app.state.live_rulesets_dir)
    pricing = os.environ.get("HUB_PRICING_FILE")
    app.state.pricing_file = Path(pricing).resolve() if pricing else Path.cwd() / "pricing.toml"
    app.state.google = google.Google(
        google.Store(data_path), time_zone=os.environ.get("HUB_TIME_ZONE", google.DEFAULT_TIME_ZONE))
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(ingest.router)
    app.include_router(google_api.router)
    app.include_router(hub_api.router)
    app.include_router(views.router)
    app.include_router(calendar_views.router)
    app.include_router(more_views.router)

    @app.exception_handler(google.GoogleError)
    async def google_failed(request: Request, exc: google.GoogleError) -> JSONResponse:
        # A problem on the side of Google is not a fault of the caller.
        status = exc.status if 400 <= exc.status < 500 else 502
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.middleware("http")
    async def same_site_forms(request: Request, call_next):
        # The pages have no sign-in, so any web page that the browser has open
        # could send a form to the hub. A browser names the page that a form
        # comes from. Forms from other sites are refused. The API is exempt:
        # it needs a token, and its callers are no browsers.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc != request.headers.get("host", ""):
                return PlainTextResponse("This form was sent from another site.", status_code=403)
        return await call_next(request)

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
