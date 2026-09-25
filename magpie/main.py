"""HTTP API and web UI."""

import asyncio
import hmac
import logging
import mimetypes
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import CATEGORIES, AnalysisError
from .analyzers import AnalyzerRouter
from .batch import BatchWorker
from .links import URL_TOO_LONG, normalize_url
from .config import Settings
from .db import Database
from .pipeline import Pipeline

STATIC_DIR = Path(__file__).parent / "static"
mimetypes.add_type("application/manifest+json", ".webmanifest")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
API_VERSION = 1
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class Correction(BaseModel):
    """What the user says the screenshot really is. Give facts, a free-text hint, or both."""
    title: str | None = None
    category: str | None = None
    year: int | None = None
    canonical_url: str | None = None
    hint: str | None = None  # e.g. "it's the 2019 remake, not the original" -> Claude looks again


class ItemPatch(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    summary: str | None = None
    category: str | None = None
    note: str | None = None
    canonical_url: str | None = None
    tags: list[str] | None = None


def create_app(
    settings: Settings | None = None,
    analyzer: Any = None,
    http: httpx.AsyncClient | None = None,
    start_batch_worker: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = http or httpx.AsyncClient(timeout=20)
        chosen = analyzer
        if chosen is None:
            try:
                chosen = AnalyzerRouter(settings, client)
            except ValueError as e:  # misconfiguration: keep serving, report it on each item
                logging.getLogger("magpie").error("Analyzer not available: %s", e)
                chosen = _Unavailable(str(e))
        state["pipeline"] = app.state.pipeline = Pipeline(db, settings, chosen, client)
        worker_task = None
        if isinstance(chosen, AnalyzerRouter) and chosen.batch and chosen.claude is not None and start_batch_worker:
            app.state.batch_worker = BatchWorker(db, state["pipeline"], chosen.claude.client, settings.batch_poll_seconds)
            worker_task = asyncio.create_task(app.state.batch_worker.run_forever())
        yield
        if worker_task:
            worker_task.cancel()
        if http is None:
            await client.aclose()

    app = FastAPI(title="Magpie", lifespan=lifespan)
    app.state.db = db

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        protected = path.startswith("/api/") or path.startswith("/media/")
        if settings.api_token and protected and path != "/api/health":
            auth = request.headers.get("authorization", "")
            supplied = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else unquote(request.cookies.get("magpie_token") or request.cookies.get("keeper_token", ""))
            if not hmac.compare_digest(supplied.encode(), settings.api_token.encode()):
                return JSONResponse({"detail": "Missing or invalid API token"}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {
            "ok": True, "api_version": API_VERSION, "auth_required": bool(settings.api_token),
            "analyzer": settings.resolved_analyzer(),
        }

    @app.get("/api/sync")
    def sync(since: str | None = Query(None, description="server_time returned by the previous sync")):
        """Delta sync for offline clients: items changed since the cursor, plus deletions."""
        return db.changes_since(since)

    def get_or_404(item_id: str) -> dict:
        item = db.get_item(item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        return item

    @app.post("/api/items", status_code=202)
    async def upload(
        background: BackgroundTasks,
        file: UploadFile | None = File(None),
        url: str | None = Form(None, description="Capture a link instead of a screenshot"),
        note: str | None = Form(None),
        tags: str | None = Form(None, description="Comma-separated tags to add"),
        id: str | None = Form(None, description="Client-generated id; re-sending the same id is a no-op"),
        created_at: str | None = Form(None, description="When the screenshot was captured (ISO 8601)"),
    ):
        if id is not None:
            if not CLIENT_ID_RE.match(id):
                raise HTTPException(422, "id must be 8-64 characters of [A-Za-z0-9_-]")
            existing = db.get_item(id)
            if existing:
                return existing
            if db.is_deleted(id):
                raise HTTPException(410, "This item was deleted")
        if (file is None) == (not url):
            raise HTTPException(422, "Send either a screenshot (file) or a link (url)")
        if url:
            return capture_url(url, id, note, tags, created_at, background)
        media_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
        if media_type not in IMAGE_TYPES:
            raise HTTPException(415, f"Unsupported image type {media_type!r}; use PNG, JPEG, WebP or GIF")
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Screenshot is larger than 20 MB")
        if not data:
            raise HTTPException(400, "Empty file")
        name = uuid.uuid4().hex + IMAGE_TYPES[media_type]
        (settings.uploads_dir / name).write_bytes(data)
        item = db.create_item(
            name, note=note or None, tags=[t for t in (tags or "").split(",") if t.strip()],
            item_id=id, created_at=_normalize_time(created_at),
        )
        background.add_task(state["pipeline"].process, item["id"])
        return item

    def capture_url(url: str, item_id: str | None, note: str | None, tags: str | None, created_at: str | None,
                    background: BackgroundTasks) -> dict:
        url = url.strip()
        if len(url) > URL_TOO_LONG:
            raise HTTPException(422, "URL is too long")
        normalized = normalize_url(url)
        if not re.match(r"^https?://[^/\s]+\.[^/\s]+", normalized):
            raise HTTPException(422, "That doesn't look like a web link (http or https)")
        existing = db.find_by_source_url(normalized)
        if existing:  # already saved: don't identify (or pay for) it twice
            new_tags = [t for t in (tags or "").split(",") if t.strip()]
            if new_tags:
                db.add_tags(existing["id"], new_tags)
            if note and not existing.get("note"):
                db.update_item(existing["id"], note=note)
            return {**db.get_item(existing["id"]), "duplicate": True}
        item = db.create_item(
            "", note=note or None, tags=[t for t in (tags or "").split(",") if t.strip()],
            item_id=item_id, created_at=_normalize_time(created_at), kind="url", source_url=normalized,
        )
        background.add_task(state["pipeline"].process, item["id"])
        return item

    @app.get("/api/items")
    def list_items(
        q: str | None = None,
        category: str | None = None,
        tag: list[str] = Query(default=[]),
        needs_review: bool = False,
        limit: int = Query(200, le=500),
        offset: int = 0,
    ):
        return db.list_items(q=q, category=category, tags=tag, needs_review=needs_review, limit=limit, offset=offset)

    @app.get("/api/items/{item_id}")
    def get_item(item_id: str):
        return get_or_404(item_id)

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: str, patch: ItemPatch):
        get_or_404(item_id)
        fields = patch.model_dump(exclude_unset=True)
        if "category" in fields and fields["category"] not in CATEGORIES:
            raise HTTPException(422, f"category must be one of {CATEGORIES}")
        tags = fields.pop("tags", None)
        if tags is not None:
            db.set_tags(item_id, tags)
        return db.update_item(item_id, **fields)

    @app.post("/api/items/{item_id}/reanalyze", status_code=202)
    def reanalyze(item_id: str, background: BackgroundTasks):
        get_or_404(item_id)
        item = db.update_item(item_id, status="processing", error=None)
        background.add_task(state["pipeline"].process, item_id, None, "reanalyze")
        return item

    @app.post("/api/items/{item_id}/correct", status_code=202)
    def correct(item_id: str, correction: Correction, background: BackgroundTasks):
        """Fix a wrong identification. The item is re-enriched in the background."""
        get_or_404(item_id)
        fix = {k: (v.strip() if isinstance(v, str) else v) for k, v in correction.model_dump().items()}
        fix = {k: v for k, v in fix.items() if v not in (None, "")}
        if not fix:
            raise HTTPException(422, "Give at least one of title, category, year, canonical_url or hint")
        if "category" in fix and fix["category"] not in CATEGORIES:
            raise HTTPException(422, f"category must be one of {CATEGORIES}")
        if "canonical_url" in fix and not re.match(r"^https?://", fix["canonical_url"]):
            raise HTTPException(422, "canonical_url must start with http:// or https://")
        preview = {k: fix[k] for k in ("title", "category") if k in fix}
        item = db.update_item(item_id, status="processing", error=None, **preview)
        background.add_task(state["pipeline"].correct, item_id, fix)
        return item

    @app.delete("/api/items/{item_id}", status_code=204)
    def delete_item(item_id: str):
        item = get_or_404(item_id)
        db.delete_item(item_id)
        if item["image_file"]:
            (settings.uploads_dir / item["image_file"]).unlink(missing_ok=True)

    @app.get("/api/usage")
    def usage(days: int = Query(30, ge=1, le=366)):
        """Measured cost of identifying screenshots: totals, per screenshot, per analyzer, per day."""
        report = db.usage_report(days)
        report["config"] = {
            "analyzer": settings.resolved_analyzer(), "claude_model": settings.model, "effort": settings.effort,
            "claude_batch": settings.claude_batch, "fetch_max_tokens": settings.fetch_max_tokens,
            "escalate_below": settings.escalate_below,
        }
        return report

    @app.get("/api/tags")
    def tags():
        return db.tag_counts()

    @app.get("/api/categories")
    def categories():
        return {"all": CATEGORIES, "counts": db.category_counts()}

    @app.get("/media/{name}")
    def media(name: str):
        path = (settings.uploads_dir / name).resolve()
        if path.parent != settings.uploads_dir.resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it can control the whole app; never cached so updates ship.
        return FileResponse(
            STATIC_DIR / "sw.js", media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.post("/share-target")
    def share_target_fallback():
        # Normally the service worker handles shares; if it isn't active yet, just open the app.
        return RedirectResponse("/", status_code=303)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _normalize_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "created_at must be ISO 8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


class _Unavailable:
    def __init__(self, reason: str):
        self.reason = reason

    async def analyze(self, *args, **kwargs):
        raise AnalysisError(f"No analyzer configured: {self.reason}")
