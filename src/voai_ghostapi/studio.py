"""Local-only Extraction Studio service.

The Studio is deliberately a thin HTTP layer over the existing extraction
engine.  It owns job orchestration and read-only result browsing; crawling,
HAR parsing, sanitization, inference, and snapshot generation remain in the
existing modules.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.robotparser
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field, field_validator

from .core import (
    Observation,
    Workspace,
    build_inventory,
    load_workspace,
    validate_workspace,
)
from .core import generate as generate_mock
from .extract import (
    _Links,
    _normalize,
    _read_sitemap_urls,
    extract,
    parse_resource_selection,
    persist_interactive_resources,
    validate_url,
)
from .interactive import discover_api_requests

_PACKAGE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PACKAGE_DIR / "studio_static"
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_EVENT_HISTORY = 500
_ALLOWED_FILE_SUFFIXES = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4",
    ".webm", ".mp3", ".wav", ".pdf", ".txt", ".xml", ".webmanifest",
}


class HarUpload(BaseModel):
    name: str = "capture.har"
    data_base64: str


class MockStartRequest(BaseModel):
    port: int = Field(default=8765, ge=1024, le=65535)


class DeleteProjectRequest(BaseModel):
    confirmed: bool = False


class ProjectRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CacheCleanupRequest(BaseModel):
    confirmed: bool = False


class ResourceExportRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class ExtractionRequest(BaseModel):
    mode: Literal["website", "har", "combined"]
    url: str | None = None
    har_paths: list[str] = Field(default_factory=list)
    har_uploads: list[HarUpload] = Field(default_factory=list)
    output: str = "extracted"
    scope: Literal["page", "site", "selected"] = "page"
    pages: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=lambda: ["html", "css", "js", "images", "json"])
    max_pages: int = Field(default=20, ge=1, le=1000)
    max_depth: int = Field(default=1, ge=0, le=20)
    concurrency: int = Field(default=4, ge=1, le=32)
    snapshot: bool = False
    interactive: bool = False
    storage_state: str | None = None
    manual_login: bool = False
    login_wait_seconds: int = Field(default=60, ge=0, le=600)
    resume: bool = False
    incremental: bool = False
    confirmed: bool = False

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str | None) -> str | None:
        return value.strip() if value else value


@dataclass
class StudioJob:
    id: str
    project_id: str
    request: ExtractionRequest
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    stage: str = "pending"
    counters: dict[str, Any] = field(default_factory=dict)
    checkpoint: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    upload_dir: Path | None = None

    def emit(self, event: dict[str, Any]) -> None:
        self.stage = str(event.get("stage", self.stage))
        self.counters.update({key: value for key, value in event.items() if key.endswith(("_completed", "_discovered", "_skipped", "_failed", "_downloaded", "_bytes"))})
        self.events.append({"sequence": len(self.events) + 1, **event})
        if len(self.events) > _MAX_EVENT_HISTORY:
            del self.events[:-_MAX_EVENT_HISTORY]


class StudioState:
    _TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "pending": {"running", "interrupted", "failed"},
        "running": {"cancelling", "completed", "failed", "interrupted"},
        "cancelling": {"cancelled", "failed"},
        "interrupted": {"pending", "failed"},
        "resumable": {"pending"},
        "completed": set(), "cancelled": set(), "failed": set(),
    }
    def __init__(self, root: Path) -> None:
        raw_root = Path(root)
        if raw_root.is_symlink():
            raise ValueError("Studio root may not be a symlink")
        for parent in raw_root.parents:
            if parent.is_symlink():
                raise ValueError("Studio root may not traverse a symlink")
        self.root = raw_root.resolve()
        self.db_path = self.root / ".studio.sqlite3"
        db = sqlite3.connect(self.db_path, check_same_thread=False)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, mode TEXT, source TEXT, config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
                stage TEXT, created_at REAL NOT NULL, started_at REAL, completed_at REAL,
                config_json TEXT NOT NULL, checkpoint TEXT, result_json TEXT, error TEXT
            );
        """)
        for column, definition in {
            "resource_selection": "TEXT NOT NULL DEFAULT '[]'",
            "scope_json": "TEXT NOT NULL DEFAULT '{}'",
            "api_inventory": "TEXT", "resource_manifest": "TEXT",
            "snapshot": "TEXT", "mock": "TEXT",
        }.items():
            try:
                db.execute(f"ALTER TABLE projects ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass
        for column, definition in {
            "counters_json": "TEXT NOT NULL DEFAULT '{}'",
            "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
            "checkpoint": "TEXT", "result_manifest": "TEXT",
        }.items():
            try:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass
        db.commit()
        db.close()
        upload_root = self.root / ".studio-uploads"
        if upload_root.is_dir() and not upload_root.is_symlink():
            for child in upload_root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
            try:
                upload_root.rmdir()
            except OSError:
                pass
        self.jobs: dict[str, StudioJob] = {}
        self.servers: dict[str, tuple[subprocess.Popen[bytes], int]] = {}
        self.lock = threading.RLock()

    def save_project(self, project_id: str, request: ExtractionRequest) -> None:
        now = time.time()
        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO projects(id,name,created_at,updated_at,mode,source,config_json,resource_selection,scope_json) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at, config_json=excluded.config_json, resource_selection=excluded.resource_selection, scope_json=excluded.scope_json",
            (project_id, request.output, now, now, request.mode, request.url, request.model_dump_json(), json.dumps(request.resources), json.dumps({"scope": request.scope, "pages": request.pages})),
        )
        db.commit(); db.close()

    def project_id(self, request: ExtractionRequest) -> str:
        db = sqlite3.connect(self.db_path)
        row = db.execute("SELECT id FROM projects WHERE name = ?", (request.output,)).fetchone()
        db.close()
        return str(row[0]) if row else secrets.token_urlsafe(12)

    def save_job(self, job: StudioJob, project_id: str, *, stage: str = "") -> None:
        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO jobs(id,project_id,status,stage,created_at,started_at,completed_at,config_json,counters_json,checkpoint,result_manifest,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, stage=excluded.stage, started_at=excluded.started_at, completed_at=excluded.completed_at, counters_json=excluded.counters_json, checkpoint=excluded.checkpoint, result_manifest=excluded.result_manifest, error=excluded.error, result_json=excluded.result_manifest",
            (job.id, project_id, job.status, stage or job.stage, job.created_at, job.started_at, job.completed_at, job.request.model_dump_json(), json.dumps(job.counters), job.checkpoint, job.request.output + "/extraction-report.json" if job.result else None, job.error),
        )
        db.commit(); db.close()

    def transition(self, job: StudioJob, status: str) -> None:
        if status != job.status and status not in self._TRANSITIONS.get(job.status, set()):
            raise ValueError(f"invalid job transition: {job.status} -> {status}")
        job.status = status
        if status == "running":
            job.started_at = job.started_at or time.time()
        if status in {"completed", "cancelled", "failed", "interrupted"}:
            job.completed_at = time.time()

    def safe_path(self, value: str | Path, *, must_exist: bool = False) -> Path:
        raw = Path(value)
        raw_candidate = raw if raw.is_absolute() else self.root / raw
        try:
            raw_candidate.relative_to(self.root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="path must stay inside the Studio root") from exc
        current = self.root
        for part in raw_candidate.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise HTTPException(status_code=400, detail="symlinked paths are not allowed")
        candidate = raw_candidate.resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="path must stay inside the Studio root") from exc
        if must_exist and not candidate.exists():
            raise HTTPException(status_code=404, detail="path does not exist")
        return candidate


def _workspace_summary(path: Path) -> dict[str, Any]:
    workspace_path = path / "workspace" / "workspace.json"
    report_path = path / "extraction-report.json"
    website_report = path / "website" / "extraction.json"
    result: dict[str, Any] = {"name": path.name, "path": str(path), "status": "available"}
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            result.update({"report": report, "status": "completed" if not report.get("cancelled") else "cancelled"})
        except (OSError, ValueError):
            result["status"] = "invalid"
    elif website_report.is_file():
        try:
            result["report"] = json.loads(website_report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result["status"] = "invalid"
    if workspace_path.is_file():
        try:
            workspace = Workspace.model_validate_json(workspace_path.read_text(encoding="utf-8"))
            result["observations"] = len(workspace.observations)
            result["warnings"] = workspace.warnings
        except (OSError, ValueError):
            result["status"] = "invalid"
    return result


def _mock_endpoint_count(path: Path) -> int:
    workspace = path.parent / "workspace"
    file = workspace / "workspace.json"
    if not file.is_file():
        return 0
    try:
        return len(Workspace.model_validate_json(file.read_text(encoding="utf-8")).observations)
    except (OSError, ValueError):
        return 0


def create_app(root: Path | None = None) -> FastAPI:
    """Create the localhost Studio application rooted at ``root``."""
    state = StudioState(root or Path.cwd())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        for process, _port in state.servers.values():
            if process.poll() is None:
                process.terminate()
        state.servers.clear()

    async def validate_local_request(request: Request) -> None:
        """Reject non-local Host/Origin values without HTTP middleware."""
        host = request.headers.get("host", "").split(":", 1)[0].lower().strip("[]")
        allowed_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        if host not in allowed_hosts:
            raise HTTPException(status_code=400, detail="Studio accepts localhost requests only")
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            origin_host = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or origin_host not in allowed_hosts:
                raise HTTPException(status_code=403, detail="Origin is not allowed")
            if origin_host != host:
                raise HTTPException(status_code=403, detail="Origin does not match the Studio host")

    app = FastAPI(
        title="VOAI-GhostAPI Extraction Studio",
        docs_url="/api/docs",
        redoc_url=None,
        dependencies=[Depends(validate_local_request)],
    )
    app.state.studio = state

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/assets/{asset_path:path}")
    async def asset(asset_path: str) -> Response:
        target = (_STATIC_DIR / asset_path).resolve(strict=False)
        try:
            target.relative_to(_STATIC_DIR)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="asset not found") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "root": str(state.root), "jobs": len(state.jobs)}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {"root": str(state.root), "modes": ["website", "har", "combined"], "browser": True}

    def validate_request(request: ExtractionRequest) -> None:
        if not request.confirmed:
            raise HTTPException(status_code=400, detail="explicit confirmation is required before extraction")
        if request.mode in {"website", "combined"} and not request.url:
            raise HTTPException(status_code=400, detail="a website URL is required for this mode")
        if request.mode in {"har", "combined"} and not request.har_paths and not request.har_uploads:
            raise HTTPException(status_code=400, detail="at least one HAR file is required for this mode")
        if request.mode == "website" and (request.har_paths or request.har_uploads):
            raise HTTPException(status_code=400, detail="website mode cannot include HAR files")
        if request.mode == "har" and request.url:
            raise HTTPException(status_code=400, detail="HAR mode cannot include a website URL")
        if request.scope == "selected" and not request.pages and request.mode != "har":
            raise HTTPException(status_code=400, detail="at least one page is required for selected scope")
        if request.scope != "selected" and request.pages:
            raise HTTPException(status_code=400, detail="pages are only valid with selected scope")
        try:
            parse_resource_selection(",".join(request.resources))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def run_job(job: StudioJob, har_paths: tuple[Path, ...]) -> None:
        state.transition(job, "running")
        job.emit({"stage": "starting", "status": "running"})
        state.save_job(job, job.project_id, stage="starting")
        try:
            output = state.safe_path(job.request.output)
            resource_types = parse_resource_selection(",".join(job.request.resources))

            def progress(event: dict[str, Any]) -> None:
                job.emit(event)

            max_pages = job.request.max_pages
            max_depth = job.request.max_depth
            selected_pages = tuple(job.request.pages)
            if job.request.scope == "page":
                max_pages, max_depth, selected_pages = 1, 0, ()
            elif job.request.scope == "selected":
                max_pages = min(max_pages, len(selected_pages))

            cache_context = "anonymous"
            if job.request.storage_state:
                storage_path = state.safe_path(job.request.storage_state, must_exist=True)
                cache_context = "storage-state:" + hashlib.sha256(storage_path.read_bytes()).hexdigest()

            workspace = await extract(
                job.request.url or "",
                output,
                har_paths,
                yes=True,
                max_pages=max_pages,
                max_depth=max_depth,
                concurrency=job.request.concurrency,
                resource_types=resource_types,
                resume=job.request.resume,
                incremental=job.request.incremental,
                cache_context=cache_context,
                snapshot=job.request.snapshot,
                cancel_event=job.cancel_event,
                progress=progress,
                selected_pages=selected_pages,
            )
            if job.request.interactive and job.request.url:
                storage_state = state.safe_path(job.request.storage_state, must_exist=True) if job.request.storage_state else None
                observed = await discover_api_requests(
                    job.request.url,
                    storage_state=storage_state,
                    manual_login=job.request.manual_login,
                    login_wait_ms=job.request.login_wait_seconds * 1000,
                )
                website_dir = output / "website"
                persist_interactive_resources(website_dir, observed)
                (website_dir / "interactive.json").write_text(json.dumps(observed, indent=2), encoding="utf-8")
                if observed:
                    interactive_observations = [
                        Observation(
                            method=str(item.get("method", "GET")),
                            url=str(item.get("url", "<ORIGIN>/")),
                            path=str(item.get("path", "/")),
                            status=int(item.get("status", 200)),
                            response_body=item.get("response_sample"),
                            source="website",
                            source_page=item.get("source_page"),
                        )
                        for item in observed
                    ]
                    current = workspace.observations if workspace else []
                    workspace = build_inventory([*current, *interactive_observations])
                    (output / "workspace").mkdir(exist_ok=True)
                    (output / "workspace" / "workspace.json").write_text(workspace.model_dump_json(indent=2), encoding="utf-8")
            report_path = output / "extraction-report.json"
            result = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
            result["workspace_observations"] = len(workspace.observations) if workspace else 0
            job.result = result
            run_root = output / ".runs" / job.id
            run_root.mkdir(parents=True, exist_ok=True)
            for relative in ("extraction-report.json", "website/extraction.json", "workspace/workspace.json"):
                source = output / relative
                if source.is_file():
                    destination = run_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            state.transition(job, "cancelled" if job.cancel_event.is_set() else "completed")
            job.emit({"stage": "complete", "status": job.status, **result})
            state.save_job(job, job.project_id, stage="complete")
        except asyncio.CancelledError:
            state.transition(job, "cancelled")
            job.error = "job cancelled"
            job.emit({"stage": "cancelled", "status": job.status})
            state.save_job(job, job.project_id, stage="cancelled")
        except Exception as exc:  # noqa: BLE001 - surfaced as a job error in the UI
            state.transition(job, "failed")
            job.error = str(exc)
            job.emit({"stage": "error", "status": "failed", "error": str(exc)})
            state.save_job(job, job.project_id, stage="error")
        finally:
            if job.upload_dir is not None:
                shutil.rmtree(job.upload_dir, ignore_errors=True)
                try:
                    job.upload_dir.parent.rmdir()
                except OSError:
                    # Another job may still be using the shared upload root.
                    pass

    @app.post("/api/jobs")
    async def create_job(request: ExtractionRequest) -> dict[str, Any]:
        validate_request(request)
        output_path = state.safe_path(request.output)
        job_id = secrets.token_urlsafe(10)
        upload_dir: Path | None = None
        har_paths = [state.safe_path(item, must_exist=True) for item in request.har_paths]
        decoded_uploads: list[tuple[str, bytes]] = []
        for upload in request.har_uploads:
            safe_name = Path(upload.name).name
            if not safe_name.lower().endswith(".har"):
                raise HTTPException(status_code=400, detail="uploaded files must use the .har extension")
            try:
                raw = base64.b64decode(upload.data_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(status_code=400, detail="invalid HAR upload encoding") from exc
            if len(raw) > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="HAR upload exceeds 25 MiB")
            decoded_uploads.append((safe_name, raw))
        if decoded_uploads:
            upload_dir = state.safe_path(Path(".studio-uploads") / job_id)
            upload_dir.mkdir(parents=True, exist_ok=True)
        for safe_name, raw in decoded_uploads:
            assert upload_dir is not None
            target = upload_dir / safe_name
            target.write_bytes(raw)
            har_paths.append(target)
        project_id = state.project_id(request)
        job = StudioJob(job_id, project_id, request)
        job.upload_dir = upload_dir
        with state.lock:
            state.jobs[job_id] = job
        state.save_project(project_id, request)
        state.save_job(job, project_id, stage="pending")
        job.task = asyncio.create_task(run_job(job, tuple(har_paths)))
        return {"id": job_id, "status": job.status, "output": str(output_path)}

    @app.get("/api/jobs")
    async def list_jobs() -> list[dict[str, Any]]:
        return [
            {"id": job.id, "status": job.status, "error": job.error, "events": len(job.events), "output": job.request.output}
            for job in state.jobs.values()
        ]

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"id": job.id, "status": job.status, "error": job.error, "result": job.result, "events": job.events[-100:]}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, cursor: int = 0) -> StreamingResponse:
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")

        async def stream():
            position = max(0, cursor)
            while True:
                while position < len(job.events):
                    event = job.events[position]
                    yield f"id: {event['sequence']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                    position += 1
                if job.status in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        job.cancel_event.set()
        job.emit({"stage": "cancelling", "status": "cancelling"})
        state.save_job(job, job.project_id, stage="cancelling")
        return {"id": job.id, "status": "cancelling"}

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> dict[str, Any]:
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status not in {"cancelled", "failed", "interrupted"}:
            raise HTTPException(status_code=409, detail="job is not resumable")
        request = job.request.model_copy(update={"confirmed": True, "resume": True})
        clone = StudioJob(secrets.token_urlsafe(10), job.project_id, request)
        state.jobs[clone.id] = clone
        state.save_job(clone, clone.project_id, stage="pending")
        clone.task = asyncio.create_task(run_job(clone, ()))
        return {"id": clone.id, "status": clone.status, "resumed_from": job.id}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str) -> dict[str, Any]:
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status != "failed":
            raise HTTPException(status_code=409, detail="only failed jobs can be retried")
        request = job.request.model_copy(update={"confirmed": True, "resume": False, "output": job.request.output + "-retry"})
        clone = StudioJob(secrets.token_urlsafe(10), job.project_id, request)
        state.jobs[clone.id] = clone; state.save_job(clone, clone.project_id, stage="pending")
        clone.task = asyncio.create_task(run_job(clone, ()))
        return {"id": clone.id, "status": clone.status, "retried_from": job.id}

    @app.get("/api/jobs/{job_id}/logs")
    async def job_logs(job_id: str) -> dict[str, Any]:
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"id": job.id, "status": job.status, "events": job.events, "error": job.error}

    @app.get("/api/jobs/{job_id}/partial-results")
    async def partial_results(job_id: str) -> dict[str, Any]:
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        output = state.safe_path(job.request.output)
        report = output / "extraction-report.json"
        return {"id": job.id, "status": job.status, "output": str(output),
                "available": report.is_file(), "report": json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None}

    @app.get("/api/discovery")
    async def discover_pages(url: str, max_pages: int = 100, max_depth: int = 2) -> dict[str, Any]:
        """Bounded, read-only sitemap and internal-link discovery."""
        if max_pages < 1 or max_pages > 1000 or max_depth < 0 or max_depth > 20:
            raise HTTPException(status_code=400, detail="max_pages must be between 1 and 1000")
        try:
            start = validate_url(url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        start = _normalize(start)
        origin = urlsplit(start).netloc.lower()
        urls: list[str] = []
        edges: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        queue: deque[str] = deque([start])
        depths: dict[str, int] = {start: 0}
        seen: set[str] = set()
        robots_url = urljoin(start, "/robots.txt")
        robots_status: int | None = None
        robots_loaded = False
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            robots = urllib.robotparser.RobotFileParser(robots_url)
            try:
                robots_response = await client.get(robots_url)
                robots_status = robots_response.status_code
                if robots_response.status_code == 200:
                    robots.parse(robots_response.text.splitlines())
                    robots_loaded = True
            except httpx.HTTPError:
                pass
            for sitemap_page in await _read_sitemap_urls(client, start, origin):
                candidate = _normalize(sitemap_page)
                if candidate not in depths and len(depths) < max_pages:
                    depths[candidate] = 0
                    queue.append(candidate)
            while queue and len(seen) < max_pages:
                current = queue.popleft()
                if current in seen or urlsplit(current).netloc.lower() != origin:
                    continue
                if not robots.can_fetch("VOAI-GhostAPI-Studio", current):
                    skipped.append({"url": current, "reason": "robots.txt"})
                    seen.add(current)
                    continue
                seen.add(current); urls.append(current)
                try:
                    response = await client.get(current)
                except httpx.HTTPError as exc:
                    skipped.append({"url": current, "reason": type(exc).__name__})
                    continue
                redirected = _normalize(str(response.url))
                if urlsplit(redirected).netloc.lower() != origin:
                    skipped.append({"url": current, "reason": "cross-origin redirect"})
                    continue
                if "text/html" not in response.headers.get("content-type", ""):
                    continue
                parser = _Links(); parser.feed(response.text)
                current_depth = depths.get(current, 0)
                for href in parser.links:
                    target = _normalize(urldefrag(urljoin(str(response.url), href))[0])
                    if urlsplit(target).netloc.lower() == origin and target not in seen:
                        edges.append({"source": current, "target": target})
                        if target not in depths and current_depth < max_depth and len(depths) < max_pages:
                            depths[target] = current_depth + 1
                            queue.append(target)
        return {"start": start, "urls": urls, "edges": edges, "depths": depths,
                "robots": {"url": robots_url, "status": robots_status, "loaded": robots_loaded,
                           "user_agent": "VOAI-GhostAPI-Studio", "enforced": True},
                "skipped": skipped, "count": len(urls), "truncated": bool(queue)}

    @app.get("/api/projects")
    async def projects() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for child in sorted(state.root.iterdir() if state.root.is_dir() else []):
            if child.is_dir() and not child.is_symlink() and (child / "extraction-report.json").is_file() and not (child / ".archived").exists():
                item = _workspace_summary(child)
                report = item.get("report") or {}
                item.update({"mode": report.get("mode", "unknown"), "pages_processed": report.get("processed", 0),
                             "resources_saved": report.get("resources_downloaded", 0), "apis_discovered": report.get("unique_apis_discovered", 0),
                             "has_snapshot": (child / "snapshot" / "snapshot.json").is_file(), "has_mock": (child / "mock" / "app.py").is_file(),
                             "disk_bytes": sum(f.stat().st_size for f in child.rglob("*") if f.is_file() and not f.is_symlink())})
                entries.append(item)
        return entries

    def project_dir(project: str) -> Path:
        target = state.safe_path(project, must_exist=True)
        if not target.is_dir() or not (target / "extraction-report.json").is_file():
            raise HTTPException(status_code=404, detail="project not found")
        return target

    @app.delete("/api/projects/{project}")
    async def delete_project(project: str, request: DeleteProjectRequest) -> dict[str, Any]:
        """Delete one completed Studio project after explicit confirmation."""
        if not request.confirmed:
            raise HTTPException(status_code=400, detail="explicit confirmation is required before deleting a project")
        target = project_dir(project)
        if target.parent != state.root:
            raise HTTPException(status_code=400, detail="project must be a direct child of the Studio root")
        running = state.servers.get(project)
        if running and running[0].poll() is None:
            raise HTTPException(status_code=409, detail="stop the project mock server before deleting the project")
        for child in target.rglob("*"):
            if child.is_symlink():
                raise HTTPException(status_code=400, detail="project contains a symlink and cannot be deleted safely")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"project deletion failed: {exc}") from exc
        return {"project": project, "deleted": True}

    @app.get("/api/projects/{project}/summary")
    async def project_summary(project: str) -> dict[str, Any]:
        return _workspace_summary(project_dir(project))

    @app.get("/api/projects/{project}/runs")
    async def project_runs(project: str) -> list[dict[str, Any]]:
        target = project_dir(project)
        summary = _workspace_summary(target)
        saved_runs = target / ".runs"
        db = sqlite3.connect(state.db_path)
        rows = db.execute(
            "SELECT id,status,stage,created_at,started_at,completed_at,counters_json,checkpoint,result_manifest,error "
            "FROM jobs WHERE project_id = ? ORDER BY created_at DESC",
            (state.project_id(ExtractionRequest(mode="har", output=project)),),
        ).fetchall()
        db.close()
        if not rows:
            return [{"run": target.name, "status": summary.get("status"), "report": summary.get("report"), "path": str(target)}]
        result: list[dict[str, Any]] = []
        for row in rows:
            counters = json.loads(row[6] or "{}")
            result.append({
                "run": row[0], "status": row[1], "stage": row[2],
                "created_at": row[3], "started_at": row[4], "completed_at": row[5],
                "counters": counters, "checkpoint": row[7], "result_manifest": row[8], "error": row[9],
                "path": str(saved_runs / row[0]) if (saved_runs / row[0]).is_dir() else str(target),
            })
        return result

    @app.post("/api/projects/{project}/rename")
    async def rename_project(project: str, request: ProjectRenameRequest) -> dict[str, Any]:
        target = project_dir(project)
        safe_name = Path(request.name).name
        if safe_name != request.name or safe_name in {"", ".", ".."}:
            raise HTTPException(status_code=400, detail="invalid project name")
        destination = state.safe_path(safe_name)
        if destination.exists():
            raise HTTPException(status_code=409, detail="project name already exists")
        target.rename(destination)
        return {"old_name": project, "name": safe_name}

    @app.post("/api/projects/{project}/archive")
    async def archive_project(project: str) -> dict[str, Any]:
        target = project_dir(project)
        (target / ".archived").write_text("archived", encoding="utf-8")
        return {"project": project, "archived": True}

    @app.post("/api/projects/{project}/duplicate")
    async def duplicate_project(project: str, name: str | None = None) -> dict[str, Any]:
        target = project_dir(project)
        destination = state.safe_path(name or f"{project}-copy")
        if destination.exists():
            raise HTTPException(status_code=409, detail="duplicate project already exists")
        destination.mkdir(parents=True)
        source_config = target / "extraction-report.json"
        if source_config.is_file():
            shutil.copy2(source_config, destination / source_config.name)
        return {"project": destination.name, "configuration_source": project}

    @app.post("/api/projects/{project}/cache/cleanup")
    async def cleanup_project_cache(project: str, request: CacheCleanupRequest) -> dict[str, Any]:
        if not request.confirmed:
            raise HTTPException(status_code=400, detail="explicit confirmation is required before cache cleanup")
        target = project_dir(project)
        removed = 0
        for cache in (target / ".cache", target / "website" / ".cache"):
            if cache.is_dir() and not cache.is_symlink():
                for item in cache.iterdir():
                    if item.is_file() and not item.is_symlink():
                        removed += item.stat().st_size; item.unlink()
        return {"project": project, "removed_bytes": removed}

    @app.get("/api/projects/{project}/compare")
    async def compare_project_runs(project: str, before: str, after: str) -> dict[str, Any]:
        """Compare two local run directories without claiming absence is deletion."""
        base = project_dir(project)
        left = state.safe_path(base / before, must_exist=True) if (base / before).is_dir() else base
        right = state.safe_path(base / after, must_exist=True) if (base / after).is_dir() else base
        def manifest(root: Path) -> dict[str, str]:
            values: dict[str, str] = {}
            for item in root.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    import hashlib
                    values[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
            return values
        old, new = manifest(left), manifest(right)
        return {"added": sorted(set(new) - set(old)), "missing_or_unobserved": sorted(set(old) - set(new)),
                "changed": sorted(path for path in set(old) & set(new) if old[path] != new[path]),
                "before": before, "after": after}

    @app.get("/api/projects/{project}/apis")
    async def project_apis(project: str, method: str | None = None, path: str | None = None,
                           status: int | None = None, content_type: str | None = None,
                           source: str | None = None, source_page: str | None = None,
                           offset: int = 0, limit: int = 100) -> dict[str, Any]:
        target = project_dir(project) / "workspace" / "workspace.json"
        if not target.is_file():
            return {"observations": [], "schemas": {}, "warnings": ["No API workspace was generated."]}
        workspace = load_workspace(target.parent)
        errors = validate_workspace(workspace)
        if offset < 0 or limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="offset must be non-negative and limit must be 1..1000")
        observations = workspace.observations
        if method: observations = [item for item in observations if item.method == method.upper()]
        if path: observations = [item for item in observations if path.lower() in item.path.lower()]
        if status is not None: observations = [item for item in observations if item.status == status]
        if content_type: observations = [item for item in observations if content_type.lower() in item.response_headers.get("content-type", "").lower()]
        if source: observations = [item for item in observations if item.source == source]
        if source_page: observations = [item for item in observations if source_page.lower() in (item.source_page or "").lower()]
        return {"observations": [item.model_dump() for item in observations[offset:offset + limit]], "total": len(observations), "offset": offset, "limit": limit, "schemas": workspace.schemas, "warnings": workspace.warnings, "validation_errors": errors}

    @app.get("/api/projects/{project}/apis/{index}")
    async def project_api_detail(project: str, index: int) -> dict[str, Any]:
        data = await project_apis(project, offset=index, limit=1)
        if not data["observations"]:
            raise HTTPException(status_code=404, detail="API observation not found")
        observation = data["observations"][0]
        route = f"{observation['method']} {observation['path']}"
        return {"observation": observation, "route": route, "schema": data["schemas"].get(route), "warnings": data["warnings"]}

    @app.get("/api/projects/{project}/apis/export")
    async def export_project_apis(project: str) -> dict[str, Any]:
        data = await project_apis(project, limit=1000)
        return {"project": project, "observations": data["observations"], "schemas": data["schemas"], "warnings": data["warnings"]}

    @app.get("/api/projects/{project}/resources")
    async def project_resources(project: str, search: str | None = None, resource_type: str | None = None,
                                offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        target = project_dir(project)
        resources: list[dict[str, Any]] = []
        manifest: dict[str, dict[str, Any]] = {}
        manifest_path = target / "website" / "extraction.json"
        if manifest_path.is_file():
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = {str(item.get("path")): item for item in raw.get("resources", []) if item.get("path")}
            except (OSError, ValueError, TypeError):
                manifest = {}
        for file in target.rglob("*"):
            if not file.is_file() or file.is_symlink():
                continue
            relative = file.relative_to(target).as_posix()
            if relative.startswith(".studio-"):
                continue
            import hashlib
            item = manifest.get(relative, {})
            resources.append({"path": relative, "name": file.name, "size": file.stat().st_size,
                              "checksum": hashlib.sha256(file.read_bytes()).hexdigest(),
                              "type": item.get("resource_type") or file.suffix.lower().lstrip(".") or "file",
                              "source_url": item.get("url"), "source_page": item.get("source_page"),
                              "extraction_status": item.get("extraction_status", "stored")})
        if search: resources = [item for item in resources if search.lower() in (item["path"] + " " + str(item.get("source_url") or "")).lower()]
        if resource_type: resources = [item for item in resources if item["type"].lower() == resource_type.lower()]
        return sorted(resources, key=lambda item: item["path"])[max(0, offset):max(0, offset) + max(1, min(limit, 1000))]

    @app.post("/api/projects/{project}/resources/export")
    async def export_project_resources(project: str, request: ResourceExportRequest) -> dict[str, Any]:
        target = project_dir(project)
        exported: list[dict[str, Any]] = []
        for relative in request.paths:
            file = state.safe_path(target / relative, must_exist=True)
            if file.is_file() and not file.is_symlink():
                try: file.relative_to(target)
                except ValueError as exc: raise HTTPException(status_code=400, detail="resource path escapes project") from exc
                exported.append({"path": relative, "size": file.stat().st_size})
        return {"project": project, "resources": exported, "manifest": str(target / "website" / "extraction.json")}

    @app.post("/api/projects/{project}/mock")
    async def generate_project_mock(project: str) -> dict[str, Any]:
        target = project_dir(project)
        workspace_dir = target / "workspace"
        if not (workspace_dir / "workspace.json").is_file():
            raise HTTPException(status_code=400, detail="project has no API workspace")
        mock_dir = target / "mock"
        if not (mock_dir / "app.py").is_file():
            try:
                generate_mock(load_workspace(workspace_dir), mock_dir)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"project": project, "path": str(mock_dir), "status": "ready"}

    @app.get("/api/mocks")
    async def list_mocks() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for project in await projects():
            mock_dir = Path(project["path"]) / "mock"
            running = state.servers.get(project["name"])
            alive = bool(running and running[0].poll() is None)
            managed = running if alive else None
            result.append({"project": project["name"], "ready": (mock_dir / "app.py").is_file(), "running": alive,
                           "pid": managed[0].pid if managed else None, "port": managed[1] if managed else None,
                           "output_directory": str(mock_dir), "endpoint_count": _mock_endpoint_count(mock_dir),
                           "logs": str(mock_dir / "server.log"), "url": f"http://127.0.0.1:{managed[1]}" if managed else None})
        return result

    @app.get("/api/projects/{project}/mock/status")
    async def mock_status(project: str) -> dict[str, Any]:
        await project_summary(project)
        running = state.servers.get(project)
        alive = bool(running and running[0].poll() is None)
        managed = running if alive else None
        return {"project": project, "running": alive, "pid": managed[0].pid if managed else None,
                "port": managed[1] if managed else None,
                "url": f"http://127.0.0.1:{managed[1]}" if managed else None}

    @app.post("/api/projects/{project}/mock/start")
    async def start_project_mock(project: str, request: MockStartRequest) -> dict[str, Any]:
        target = project_dir(project) / "mock"
        if not (target / "app.py").is_file():
            raise HTTPException(status_code=400, detail="generate the mock before starting it")
        existing = state.servers.get(project)
        if existing and existing[0].poll() is None:
            return {"project": project, "status": "running", "port": existing[1], "url": f"http://127.0.0.1:{existing[1]}"}
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", request.port))
        except OSError as exc:
            raise HTTPException(status_code=409, detail=f"port {request.port} is unavailable: {exc}") from exc
        finally:
            probe.close()
        log_path = target / "server.log"
        log_handle = log_path.open("ab")
        process = await asyncio.to_thread(
            subprocess.Popen,
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(request.port)],
            cwd=target,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        log_handle.close()
        state.servers[project] = (process, request.port)
        await asyncio.sleep(0.15)
        if process.poll() is not None:
            raise HTTPException(status_code=502, detail=f"mock server failed to start; see {log_path.name}")
        return {"project": project, "status": "running", "pid": process.pid, "port": request.port, "url": f"http://127.0.0.1:{request.port}", "swagger": f"http://127.0.0.1:{request.port}/docs", "openapi": f"http://127.0.0.1:{request.port}/openapi.json"}

    @app.post("/api/projects/{project}/mock/stop")
    async def stop_project_mock(project: str) -> dict[str, Any]:
        existing = state.servers.pop(project, None)
        if existing and existing[0].poll() is None:
            existing[0].terminate()
            try:
                existing[0].wait(timeout=5)
            except subprocess.TimeoutExpired:
                existing[0].kill()
        return {"project": project, "status": "stopped"}

    @app.get("/api/projects/{project}/mock/logs")
    async def mock_logs(project: str) -> dict[str, Any]:
        target = project_dir(project) / "mock" / "server.log"
        if not target.is_file(): return {"project": project, "logs": ""}
        return {"project": project, "logs": target.read_text(encoding="utf-8", errors="replace")[-100_000:]}

    @app.get("/api/projects/{project}/file")
    async def project_file(project: str, path: str) -> Response:
        target = project_dir(project)
        file = state.safe_path(target / path, must_exist=True)
        try:
            file.relative_to(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="file path escapes project") from exc
        if not file.is_file() or file.suffix.lower() not in _ALLOWED_FILE_SUFFIXES:
            raise HTTPException(status_code=404, detail="file is not available")
        media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if media_type.startswith("text/") or file.suffix.lower() in {".json", ".js", ".mjs", ".css", ".svg"}:
            return Response(file.read_text(encoding="utf-8", errors="replace"), media_type=media_type)
        return FileResponse(file, media_type=media_type)

    def snapshot_file_path(project: str, path: str = "") -> tuple[Path, Path]:
        target = project_dir(project) / "snapshot"
        if not target.is_dir() or not (target / "snapshot.json").is_file():
            raise HTTPException(status_code=404, detail="project has no offline snapshot")
        if not path:
            candidates = sorted(
                item for item in target.rglob("*")
                if item.is_file() and item.suffix.lower() in {".html", ".htm"}
            )
            if not candidates:
                raise HTTPException(status_code=404, detail="snapshot has no HTML entry point")
            file = candidates[0]
        else:
            file = state.safe_path(target / path, must_exist=True)
        try:
            file.relative_to(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="snapshot path escapes project") from exc
        if not file.is_file() or file.is_symlink() or file.name == "snapshot.json":
            raise HTTPException(status_code=404, detail="snapshot file is not available")
        return target, file

    @app.get("/api/projects/{project}/snapshot")
    async def snapshot_entry(project: str) -> RedirectResponse:
        _target, file = snapshot_file_path(project)
        relative = file.relative_to(_target).as_posix()
        return RedirectResponse(f"/api/projects/{project}/snapshot/{relative}")

    @app.get("/api/projects/{project}/snapshot/{path:path}")
    async def snapshot_file(project: str, path: str) -> Response:
        _target, file = snapshot_file_path(project, path)
        media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if media_type.startswith("text/") or file.suffix.lower() in {".html", ".htm", ".json", ".js", ".mjs", ".css", ".svg"}:
            response = Response(file.read_text(encoding="utf-8", errors="replace"), media_type=media_type)
        else:
            response = FileResponse(file, media_type=media_type)
        if file.suffix.lower() in {".html", ".htm", ".svg"}:
            response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'none'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
        return response

    return app


def run_studio(*, host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False, root: Path | None = None) -> None:
    """Run the local Studio, optionally opening the default browser."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Studio only permits localhost binding")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(create_app(root), host=host, port=port)

