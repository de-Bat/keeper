"""HTTP API and web UI."""

import hmac
import logging
import mimetypes
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import CATEGORIES, ScreenshotAnalyzer
from .config import Settings
from .db import Database
from .pipeline import Pipeline

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
API_VERSION = 1
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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
    analyzer: ScreenshotAnalyzer | None = None,
    http: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = http or httpx.AsyncClient(timeout=20)
        state["pipeline"] = Pipeline(db, settings, analyzer or ScreenshotAnalyzer(model=settings.model), client)
        yield
        if http is None:
            await client.aclose()

    app = FastAPI(title="Keeper", lifespan=lifespan)
    app.state.db = db

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        protected = path.startswith("/api/") or path.startswith("/media/")
        if settings.api_token and protected and path != "/api/health":
            auth = request.headers.get("authorization", "")
            supplied = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else unquote(request.cookies.get("keeper_token", ""))
            if not hmac.compare_digest(supplied.encode(), settings.api_token.encode()):
                return JSONResponse({"detail": "Missing or invalid API token"}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {"ok": True, "api_version": API_VERSION, "auth_required": bool(settings.api_token)}

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
        file: UploadFile = File(...),
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

    @app.get("/api/items")
    def list_items(
        q: str | None = None,
        category: str | None = None,
        tag: list[str] = Query(default=[]),
        limit: int = Query(200, le=500),
        offset: int = 0,
    ):
        return db.list_items(q=q, category=category, tags=tag, limit=limit, offset=offset)

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
        background.add_task(state["pipeline"].process, item_id)
        return item

    @app.delete("/api/items/{item_id}", status_code=204)
    def delete_item(item_id: str):
        item = get_or_404(item_id)
        db.delete_item(item_id)
        (settings.uploads_dir / item["image_file"]).unlink(missing_ok=True)

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
        return FileResponse(STATIC_DIR / "index.html")

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
