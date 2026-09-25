"""Screenshot -> analysis -> enrichment -> stored item."""

import logging
import mimetypes
from pathlib import Path
from typing import Any

import anthropic
import httpx

from .analyzer import AnalysisError, ScreenshotAnalyzer
from .config import Settings
from .db import Database
from .enrich import Enrichment, run_enrichers

log = logging.getLogger(__name__)


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def merge(analysis: dict, enrichments: list[Enrichment]) -> dict:
    """Combine Claude's identification with enrichment results into item fields.

    Enrichers come from authoritative sources (GitHub, TMDB, the recipe page itself),
    so their facts win over the model's; the model's summary and tags are kept.
    """
    details = analysis.get("details") or {}
    metadata: dict[str, Any] = {k: v for k, v in details.items() if not _empty(v)}
    for key in ("year", "confidence", "screenshot_text"):
        if not _empty(analysis.get(key)):
            metadata[key] = analysis[key]

    canonical_url = analysis.get("canonical_url")
    image_url = analysis.get("image_url")
    subtitle = analysis.get("subtitle")
    links = list(analysis.get("links") or [])
    tags = list(analysis.get("tags") or [])
    sources = ["claude"]

    for e in enrichments:
        metadata.update({k: v for k, v in e.metadata.items() if not _empty(v)})
        canonical_url = e.canonical_url or canonical_url
        image_url = e.image_url or image_url
        subtitle = subtitle or e.subtitle
        if e.subtitle and e.subtitle != subtitle:
            metadata.setdefault("description", e.subtitle)
        if e.summary:
            metadata["description"] = e.summary
        links.extend(e.links)
        tags.extend(e.tags)
        if e.source:
            sources.append(e.source)

    if details.get("post_url"):
        links.insert(0, {"label": "Original post", "url": details["post_url"]})
    seen, unique_links = {canonical_url}, []
    for link in links:
        url = (link or {}).get("url")
        if url and url.startswith("http") and url not in seen:
            seen.add(url)
            unique_links.append({"label": link.get("label") or url, "url": url})
    metadata["sources"] = sources

    return {
        "category": analysis.get("category") or "other",
        "source_platform": analysis.get("source_platform"),
        "title": analysis.get("title"),
        "subtitle": subtitle,
        "summary": analysis.get("summary"),
        "canonical_url": canonical_url,
        "image_url": image_url,
        "metadata": metadata,
        "links": unique_links,
        "tags": tags,
    }


class Pipeline:
    def __init__(self, db: Database, settings: Settings, analyzer: ScreenshotAnalyzer, http: httpx.AsyncClient):
        self.db, self.settings, self.analyzer, self.http = db, settings, analyzer, http

    async def process(self, item_id: str) -> dict | None:
        item = self.db.get_item(item_id)
        if not item:
            return None
        path = self.settings.uploads_dir / item["image_file"]
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        try:
            analysis = await self.analyzer.analyze(Path(path).read_bytes(), media_type, note=item.get("note"))
            return await self.apply_analysis(item_id, analysis)
        except AnalysisError as e:
            return self.db.update_item(item_id, status="error", error=str(e))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return self.db.update_item(item_id, status="error", error=(
                "The server's Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY and re-analyze."
            ))
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            return self.db.update_item(item_id, status="error", error=f"Temporary problem reaching Claude ({type(e).__name__}). Try re-analyzing.")
        except Exception as e:  # keep the item; the user can retry
            log.exception("Processing %s failed", item_id)
            return self.db.update_item(item_id, status="error", error=f"{type(e).__name__}: {e}")

    async def apply_analysis(self, item_id: str, analysis: dict) -> dict | None:
        enrichments = await run_enrichers(analysis, self.settings, self.http)
        fields = merge(analysis, enrichments)
        tags = fields.pop("tags")
        self.db.add_tags(item_id, tags)
        return self.db.update_item(item_id, **fields, analysis=analysis, status="ready", error=None)
