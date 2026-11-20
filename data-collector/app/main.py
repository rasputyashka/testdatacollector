"""Local receiver for the workload collector. Run: uvicorn app.main:app --port 8080."""
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import presentation as display
from .schemas import Report
from .storage import Storage

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(default_factory=lambda: Path(os.getenv(
        "COLLECTOR_DATABASE", str(BASE.parent / "data" / "collector.sqlite3"))))
    stale_seconds: int = field(default_factory=lambda: int(os.getenv("COLLECTOR_STALE_SECONDS", "180")))
    max_body_bytes: int = field(default_factory=lambda: int(os.getenv("COLLECTOR_MAX_BODY_BYTES", "1048576")))
    allowed_hosts: tuple[str, ...] = field(default_factory=lambda: tuple(
        value.strip() for value in os.getenv("COLLECTOR_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
        if value.strip()))

    def __post_init__(self):
        if self.stale_seconds <= 0 or self.max_body_bytes <= 0 or not self.allowed_hosts:
            raise ValueError("Collector settings must be positive and allowed_hosts cannot be empty")


def reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    storage = Storage(settings.database_path)

    @asynccontextmanager
    async def lifespan(application):
        await run_in_threadpool(storage.initialize)
        yield

    application = FastAPI(title="Data Collector", lifespan=lifespan, redirect_slashes=False)
    application.state.storage = storage
    application.state.settings = settings
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    application.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")
    templates.env.filters.update(bytes=display.byte_size, percent=display.percent,
                                 timestamp=display.timestamp, uptime=display.uptime)
    templates.env.globals.update(memory_percent=display.memory_percent)

    @application.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        LOG.error("SQLite operation failed", exc_info=error)
        return JSONResponse({"detail": "Хранилище временно недоступно"}, status_code=503)

    @application.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.post("/api/v1/reports", status_code=204)
    async def receive_report(request: Request):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(415, "Expected application/json")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(415, "Content encoding is not supported")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                declared_length = int(length)
            except ValueError:
                raise HTTPException(400, "Invalid Content-Length") from None
            if declared_length < 0:
                raise HTTPException(400, "Invalid Content-Length")
            if declared_length > settings.max_body_bytes:
                raise HTTPException(413, "Report is too large")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > settings.max_body_bytes:
                raise HTTPException(413, "Report is too large")
            body.extend(chunk)
        try:
            data = json.loads(body, parse_constant=reject_constant)
            # Check the raw tree before Pydantic can normalize unknown infinities to null.
            json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            report = Report.model_validate(data)
            # Includes additive fields, which are intentionally accepted.
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except ValidationError as error:
            errors = error.errors(include_url=False, include_context=False, include_input=False)
            raise HTTPException(422, errors) from None
        except (ValueError, UnicodeError, RecursionError, OverflowError):
            raise HTTPException(422, "Invalid JSON report") from None
        await run_in_threadpool(storage.save, report)
        return Response(status_code=204)

    def render(request, name, **context):
        return templates.TemplateResponse(request=request, name=name, context=context)

    @application.get("/", response_class=HTMLResponse)
    def hosts(request: Request, q: str = Query("", max_length=256),
              page: int = Query(1, ge=1, le=1000000)):
        records, total = storage.list_hosts(q, page, 25)
        for record in records:
            record["stale"] = display.is_stale(record["last_received_at"], settings.stale_seconds)
        return render(request, "hosts.html", records=records, total=total, q=q, page=page,
                      pages=max(1, (total + 24) // 25), stale_seconds=settings.stale_seconds)

    @application.get("/hosts/{host_id}", response_class=HTMLResponse)
    def host_detail(request: Request, host_id: UUID, page: int = Query(1, ge=1, le=1000000)):
        record = storage.host(str(host_id))
        if record is None:
            raise HTTPException(404, "Хост не найден")
        history, total = storage.history(str(host_id), page, 25)
        return render(request, "host_detail.html", record=record, report=record["report"],
                      network=display.network_rows(record, storage.previous(record)), history=history,
                      total=total, page=page, pages=max(1, (total + 24) // 25),
                      stale=display.is_stale(record["last_received_at"], settings.stale_seconds))

    @application.get("/reports/{report_id}", response_class=HTMLResponse)
    def report_detail(request: Request, report_id: int):
        record = storage.report(report_id)
        if record is None:
            raise HTTPException(404, "Отчёт не найден")
        return render(request, "report.html", record=record,
                      payload=json.dumps(record["report"], ensure_ascii=False, indent=2))

    @application.get("/api/v1/reports/{report_id}")
    def report_json(report_id: int):
        record = storage.report(report_id)
        if record is None:
            raise HTTPException(404, "Отчёт не найден")
        return record["report"]

    @application.get("/healthz")
    def health():
        storage.healthy()
        return {"status": "ok"}

    return application


app = create_app()
