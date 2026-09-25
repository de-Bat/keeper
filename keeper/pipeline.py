"""Screenshot -> analysis -> enrichment -> stored item."""

import logging
import mimetypes
from pathlib import Path
from typing import Any

import anthropic
import httpx

from .analyzer import AnalysisError, ScreenshotAnalyzer
from .config import Settings
from .db import Database, normalize_tag
from .enrich import Enrichment, run_enrichers

log = logging.getLogger(__name__)


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


LEGACY_CONFIDENCE = {"high": 90, "medium": 70, "low": 40}
CORRECTABLE = ("title", "category", "year", "canonical_url")


def confidence_score(value: Any) -> int | None:
    if isinstance(value, str):
        return LEGACY_CONFIDENCE.get(value.lower())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, min(100, int(round(value))))
    return None


def merge(analysis: dict, enrichments: list[Enrichment]) -> dict:
    """Combine Claude's identification with enrichment results into item fields.

    Enrichers come from authoritative sources (GitHub, TMDB, the recipe page itself),
    so their facts win over the model's; the model's summary and tags are kept.
    """
    details = analysis.get("details") or {}
    metadata: dict[str, Any] = {k: v for k, v in details.items() if not _empty(v)}
    for key in ("year", "screenshot_text"):
        if not _empty(analysis.get(key)):
            metadata[key] = analysis[key]

    canonical_url = analysis.get("canonical_url")
    image_url = analysis.get("image_url")
    subtitle = analysis.get("subtitle")
    summary = analysis.get("summary") or None
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
            if summary:
                metadata["description"] = e.summary
            else:
                summary = e.summary  # e.g. after a manual correction, when there is no model summary
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

    alternatives = [
        {k: a.get(k) for k in ("title", "category", "year", "canonical_url", "why")}
        for a in analysis.get("alternatives") or [] if isinstance(a, dict) and a.get("title")
    ][:3]

    return {
        "category": analysis.get("category") or "other",
        "source_platform": analysis.get("source_platform"),
        "title": analysis.get("title"),
        "subtitle": subtitle,
        "summary": summary,
        "canonical_url": canonical_url,
        "image_url": image_url,
        "metadata": metadata,
        "links": unique_links,
        "tags": tags,
        "confidence": confidence_score(analysis.get("confidence")),
        "confidence_reason": analysis.get("confidence_reason") or None,
        "alternatives": alternatives,
    }


def corrected_analysis(previous: dict, correction: dict) -> dict:
    """Rebuild an analysis from the user's corrected facts, without asking the model again.

    Anything that described the wrongly identified thing (details, poster, links, summary)
    is dropped so the enrichers can look up the right one; facts about the post itself stay.
    """
    fixed = {k: correction[k] for k in CORRECTABLE if correction.get(k) not in (None, "")}
    same_thing = all(fixed.get(k, previous.get(k)) == previous.get(k) for k in ("title", "category"))
    old_details = previous.get("details") or {}
    return {
        "category": fixed.get("category") or previous.get("category") or "other",
        "source_platform": previous.get("source_platform"),
        "title": fixed.get("title") or previous.get("title"),
        "year": fixed.get("year", previous.get("year") if same_thing else None),
        "canonical_url": fixed.get("canonical_url") or (previous.get("canonical_url") if same_thing else None),
        "subtitle": previous.get("subtitle") if same_thing else None,
        "summary": previous.get("summary") if same_thing else None,
        "image_url": None,
        "links": [],
        "tags": list(previous.get("tags") or []) if same_thing else [],
        "screenshot_text": previous.get("screenshot_text"),
        "details": {k: old_details.get(k) for k in ("posted_by", "post_url") if old_details.get(k)},
        "confidence": 100,
        "confidence_reason": "Corrected by you.",
        "alternatives": [],
    }


class Pipeline:
    def __init__(self, db: Database, settings: Settings, analyzer: ScreenshotAnalyzer, http: httpx.AsyncClient):
        self.db, self.settings, self.analyzer, self.http = db, settings, analyzer, http

    async def process(self, item_id: str, correction: dict | None = None) -> dict | None:
        """Identify the screenshot with Claude (optionally guided by a user's correction)."""
        item = self.db.get_item(item_id)
        if not item:
            return None

        async def run():
            path = self.settings.uploads_dir / item["image_file"]
            media_type = mimetypes.guess_type(path.name)[0] or "image/png"
            context = None
            if correction:
                context = {**correction, "previous_title": item.get("title"), "previous_category": item.get("category")}
            analysis = await self.analyzer.analyze(Path(path).read_bytes(), media_type, note=item.get("note"), correction=context)
            if correction:
                # The user's explicit facts beat the model's.
                explicit = {k: correction[k] for k in CORRECTABLE if correction.get(k) not in (None, "")}
                analysis.update(explicit)
                if explicit:
                    analysis["confidence"], analysis["confidence_reason"] = 100, "Corrected by you."
            return await self.apply_analysis(item_id, analysis, corrected=bool(correction))

        return await self._guard(item_id, run())

    async def correct(self, item_id: str, correction: dict) -> dict | None:
        """Apply a user's correction. With a free-text hint Claude looks again; otherwise the
        corrected facts are used as-is and only the metadata lookups run."""
        if correction.get("hint"):
            return await self.process(item_id, correction)
        item = self.db.get_item(item_id)
        if not item:
            return None
        previous = item.get("analysis") if isinstance(item.get("analysis"), dict) else {
            k: item.get(k) for k in ("title", "category", "canonical_url", "subtitle", "summary", "source_platform")
        }
        return await self._guard(item_id, self.apply_analysis(item_id, corrected_analysis(previous, correction), corrected=True))

    async def apply_analysis(self, item_id: str, analysis: dict, corrected: bool = False) -> dict | None:
        enrichments = await run_enrichers(analysis, self.settings, self.http)
        fields = merge(analysis, enrichments)

        # Replace the tags generated for the previous identification, keep the user's own.
        item = self.db.get_item(item_id) or {}
        previous = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        stale = {normalize_tag(t) for t in previous.get("_auto_tags") or previous.get("tags") or []}
        auto_tags = sorted({t for t in (normalize_tag(t) for t in fields.pop("tags")) if t})
        analysis = {**analysis, "_auto_tags": auto_tags}
        self.db.set_tags(item_id, [t for t in item.get("tags", []) if t not in stale] + auto_tags)

        return self.db.update_item(
            item_id, **fields, analysis=analysis, status="ready", error=None,
            corrected=int(corrected or bool(item.get("corrected"))),
        )

    async def _guard(self, item_id: str, work) -> dict | None:
        try:
            return await work
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
