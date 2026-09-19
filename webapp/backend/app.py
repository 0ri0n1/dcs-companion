import asyncio
from contextlib import asynccontextmanager
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from .auth import Auth, COOKIE
from .command_queue import QueueError
from .config import APP_ROOT, Settings


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(default="", max_length=200)
    ticket: str | None = Field(default=None, min_length=1, max_length=200)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=36, max_length=36)
    action_id: str = Field(min_length=1, max_length=80)
    confirmed: bool = False


class SetupSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    install_id: str = Field(min_length=1, max_length=80)
    profile_id: str = Field(min_length=1, max_length=80)


def _worker_interval(service):
    # Keep idle maintenance at its existing cadence. A pending touch should not
    # add 150 ms between fresh focus samples or after another guarded check.
    remote = getattr(service, 'remote', None)
    interval = getattr(remote, 'worker_interval', None)
    return interval() if callable(interval) else .15


def create_app(settings=None, service=None):
    settings = settings or Settings.from_env()
    auth = Auth(settings)
    owned = service is None
    holder = {"service": service}
    from .diagnostics import ConnectionDiagnostics
    diagnostics = ConnectionDiagnostics(settings.runtime_dir if owned else None)

    @asynccontextmanager
    async def lifespan(app):
        instance_file = None
        if owned:
            settings.runtime_dir.mkdir(parents=True, exist_ok=True)
            instance_file = (settings.runtime_dir / "backend.lock").open("a+b")
            instance_file.seek(0)
            try:
                import msvcrt
                msvcrt.locking(instance_file.fileno(), msvcrt.LK_NBLCK, 1)
            except ImportError:
                import fcntl
                fcntl.flock(instance_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                instance_file.close()
                raise RuntimeError("The dashboard is already running. Open http://127.0.0.1:18787")
            from .service import DashboardService
            holder["service"] = DashboardService(settings)
        app.state.dashboard_service = holder["service"]
        diagnostics.event("server_started")

        async def worker():
            while True:
                try:
                    await asyncio.to_thread(holder["service"].tick)
                except Exception:
                    logging.exception("Queue tick failed; no send retry is attempted.")
                await asyncio.sleep(_worker_interval(holder["service"]))

        task = asyncio.create_task(worker())
        try:
            yield
        finally:
            diagnostics.stopping = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if owned:
                holder["service"].close()
            if instance_file:
                instance_file.close()
            diagnostics.event("server_stopped")
            diagnostics.close()

    app = FastAPI(title="DCS Companion", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.connection_diagnostics = diagnostics
    from .snapshots import SharedSnapshot
    shared_snapshot = SharedSnapshot()

    def build_snapshot():
        snapshot = holder["service"].snapshot(auth.clients())
        # The snapshot is already JSON data (also used by SSE). Serialize once
        # in this worker, not once per client on the streaming event loop.
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        return snapshot, encoded

    def observed_snapshot(channel):
        started = time.monotonic()
        try:
            # Concurrent browsers share the same in-progress observation. A
            # later request always builds anew; command contexts are independent.
            snapshot, encoded = shared_snapshot.read(build_snapshot)
        except Exception:
            diagnostics.event("snapshot_failed", channel=channel, duration_ms=(time.monotonic()-started)*1000)
            raise
        diagnostics.observe(snapshot, (time.monotonic()-started)*1000, started, channel)
        return encoded

    @app.middleware("http")
    async def protect(request, call_next):
        try:
            auth.validate_request(request)
            if request.url.path.startswith("/api/") and request.url.path not in ("/api/bootstrap", "/api/session"):
                auth.require(request)
            length = request.headers.get("content-length")
            body_limit = 16000 if request.url.path == "/api/remote/workflows" else 4096
            try:
                declared_length = int(length) if length else 0
            except ValueError:
                raise HTTPException(400, "Invalid request size.")
            if declared_length < 0 or declared_length > body_limit:
                raise HTTPException(413, "Request too large.")
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/bootstrap")
    def bootstrap():
        return {"lan_enabled": settings.lan_enabled, "dry_run": settings.dry_run, "pairing_required": settings.lan_enabled}

    @app.post("/api/session")
    def pair(body: PairRequest, request: Request):
        if body.token and body.ticket is not None:
            raise HTTPException(400, "Use a pairing code or a QR ticket, not both.")
        session = auth.pair(request, body.token, ticket=body.ticket)
        response = JSONResponse({"paired": True, "lan_enabled": settings.lan_enabled})
        response.set_cookie(COOKIE, session, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=12*3600)
        return response

    @app.post("/api/pairing-ticket")
    def pairing_ticket(request: Request):
        return auth.issue_pairing_ticket(request)

    @app.get("/api/state")
    def state():
        # Avoid FastAPI recursively copying thousands of catalogue rows on its
        # event loop; authentication and all original JSON fields are unchanged.
        return Response(content=observed_snapshot("poll"), media_type="application/json")

    @app.get("/api/diagnostics")
    def connection_diagnostics():
        return diagnostics.snapshot()

    @app.get("/api/bindings")
    def bindings(aircraft: Literal["F-22A", "FA-18C_hornet"] | None = None):
        return holder["service"].binding_snapshot(aircraft)

    @app.get("/api/setup")
    def setup(refresh: bool = False):
        return holder["service"].setup_snapshot(refresh=refresh)

    @app.post("/api/setup/selection")
    def setup_selection(body: SetupSelection, request: Request):
        if not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "Choose the DCS setup on the DCS computer.")
        try:
            holder["service"].setup.select(body.install_id, body.profile_id)
            snapshot = holder["service"].setup_snapshot()
            if snapshot.get("pending_restart") and getattr(holder["service"], "display_feed", None):
                holder["service"].display_feed.close()
            return snapshot
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/export/archive/{index}")
    def export_archive(index: int):
        if index not in range(4):
            raise HTTPException(404, "Unknown archive segment.")
        name = "packets.jsonl" if index == 0 else f"packets.{index}.jsonl"
        path = settings.state_path.parent / "exports" / name
        try:
            with path.open("rb") as archive:
                payload = archive.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024:
                raise HTTPException(413, "Archive exceeds its expected segment size.")
            # A writer may be appending its final line. Download complete records.
            payload = payload[:payload.rfind(b"\n") + 1]
        except OSError:
            raise HTTPException(404, "No recorded packets in this archive segment yet.")
        return Response(payload, media_type="application/x-ndjson", headers={
            "Content-Disposition": f'attachment; filename="dcs-export-{name}"'})

    @app.get("/api/navigation/{terrain}")
    def navigation_library(terrain: str):
        try:
            return holder["service"].navigation_library(terrain)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/events")
    async def events(request: Request):
        async def stream():
            diagnostics.event("stream_opened")
            try:
                while not diagnostics.stopping and not await request.is_disconnected():
                    try:
                        auth.require(request)
                    except HTTPException:
                        diagnostics.event("session_expired")
                        return
                    encoded = await asyncio.to_thread(observed_snapshot, "stream")
                    yield b"data: " + encoded + b"\n\n"
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                diagnostics.event("stream_error")
                logging.exception("Live stream failed; browser will reconnect")
                raise
            finally:
                diagnostics.event("stream_closed")
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/commands")
    def command(body: CommandRequest):
        try:
            return holder["service"].queue.enqueue(body.request_id, body.action_id, body.confirmed)
        except QueueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/commands/{request_id}/cancel")
    def cancel(request_id: str):
        try:
            return holder["service"].queue.cancel(request_id)
        except QueueError as exc:
            raise HTTPException(404, str(exc)) from exc

    from .remote_api import register_remote_routes
    register_remote_routes(app, holder, settings)
    from .display_feed_api import register_display_feed_routes
    register_display_feed_routes(app, holder, settings)

    @app.get("/{path:path}")
    def static(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Unknown API route.")
        directory = (APP_ROOT / "frontend" / "dist").resolve()
        target = (directory / path).resolve()
        if not target.is_relative_to(directory):
            raise HTTPException(404)
        if target.is_file():
            media = "application/javascript" if target.suffix == ".js" else mimetypes.guess_type(target)[0]
            return FileResponse(target, media_type=media)
        if path and "." in Path(path).name:
            raise HTTPException(404)
        if (directory / "index.html").is_file():
            return FileResponse(directory / "index.html", headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "Build the dashboard frontend first using Setup Dashboard.cmd."}, status_code=503)

    return app
