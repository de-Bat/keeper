"""Screenshot -> analysis -> enrichment -> stored item."""

import json
import logging
import mimetypes
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import anthropic
import httpx

from .analyzer import AnalysisError
from .analyzers import AnalyzerRouter, Deferred
from .usage import Run, claude_cost
from . import links, readability
from .enrich import fetch_page
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


def titles_match(a: str | None, b: str | None) -> bool:
    """Loose title equality that tolerates OCR artefacts (dropped spaces, punctuation, case)."""
    if not a or not b:
        return False
    norm = lambda s: re.sub(r"[^0-9a-z]", "", s.lower())  # noqa: E731
    a, b = norm(a), norm(b)
    return bool(a) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.9)


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
    used = analysis.get("_analyzer") or ["claude"]
    metadata["sources"] = used + sources[1:]
    if analysis.get("_ocr_text"):
        metadata["ocr_text"] = analysis["_ocr_text"][:5000]  # full OCR text: searchable even if identification fails

    confidence = confidence_score(analysis.get("confidence"))
    confidence_reason = analysis.get("confidence_reason") or None
    # Claude verifies with web search itself; other analyzers get a second opinion from the
    # authoritative source the enrichers matched (GitHub, TMDB, Open Library, the recipe page).
    if "claude" not in used and confidence is not None and confidence < 90:
        match = next((e for e in enrichments if e.matched_title and titles_match(analysis.get("title"), e.matched_title)), None)
        if match:
            confidence = max(confidence, 85)
            confidence_reason = f"{confidence_reason or ''} Confirmed by {match.source}.".strip()

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
        "confidence": confidence,
        "confidence_reason": confidence_reason,
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
    def __init__(self, db: Database, settings: Settings, analyzer: Any, http: httpx.AsyncClient):
        self.db, self.settings, self.analyzer, self.http = db, settings, analyzer, http

    async def process(self, item_id: str, correction: dict | None = None, purpose: str = "analyze") -> dict | None:
        """Identify the screenshot (optionally guided by a user's correction)."""
        item = self.db.get_item(item_id)
        if not item:
            return None
        if item.get("kind") == "url":
            return await self.process_url(item_id, correction, purpose)
        path = self.settings.uploads_dir / item["image_file"]
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        context = None
        if correction:
            context = {**correction, "previous_title": item.get("title"), "previous_category": item.get("category")}
        kwargs: dict[str, Any] = {"note": item.get("note"), "correction": context}
        if isinstance(self.analyzer, AnalyzerRouter):
            # The user is waiting on corrections and re-analyses: never send those to a batch.
            kwargs["interactive"] = purpose != "analyze"

        async def identify() -> dict:
            analysis = await self.analyzer.analyze(Path(path).read_bytes(), media_type, **kwargs)
            if correction:
                # The user's explicit facts beat the model's.
                explicit = {k: correction[k] for k in CORRECTABLE if correction.get(k) not in (None, "")}
                analysis.update(explicit)
                if explicit:
                    analysis["confidence"], analysis["confidence_reason"] = 100, "Corrected by you."
            return analysis

        return await self._run(item_id, purpose, identify(), corrected=bool(correction))

    async def process_url(self, item_id: str, correction: dict | None = None, purpose: str = "analyze") -> dict | None:
        """Identify a shared link: by URL pattern / structured data if possible (no model, free),
        otherwise from the page's reader-view text with the configured model, otherwise a generic card."""
        item = self.db.get_item(item_id)
        if not item:
            return None
        url = item["source_url"]
        page = await fetch_page(url, self.http)
        article = readability.extract(page.html, page.url) if page else None
        known = None if correction else links.classify(url, page)
        router = self.analyzer if isinstance(self.analyzer, AnalyzerRouter) else None

        async def identify() -> dict:
            if known:
                analysis = known
            elif router and router.mode != "ocr":
                context = None
                if correction:
                    context = {**correction, "previous_title": item.get("title"), "previous_category": item.get("category")}
                readable = bool(article and article.word_count >= 150)
                analysis = await router.analyze(
                    None, None, note=item.get("note"), correction=context, interactive=purpose != "analyze",
                    link_url=url, page_hints=links.page_context(url, page, article), web=not readable,
                )
                analysis["_analyzer"] = ["link", "readability"] + analysis.get("_analyzer", [])
            else:
                analysis = links.generic(url, page, article, item.get("note"))
            return self._finish_link(url, analysis, article, correction)

        return await self._run(item_id, purpose, identify(), corrected=bool(correction))

    def _finish_link(self, url: str, analysis: dict, article, correction: dict | None) -> dict:
        analysis.setdefault("source_platform", links.platform_of(url))
        if not analysis.get("canonical_url"):
            analysis["canonical_url"] = url
        if analysis["canonical_url"] != url:
            analysis.setdefault("links", []).insert(0, {"label": "Shared link", "url": url})
        if article and article.text and not analysis.get("screenshot_text"):
            analysis["_ocr_text"] = article.text  # searchable, like a screenshot's OCR text
        if correction:
            explicit = {k: correction[k] for k in CORRECTABLE if correction.get(k) not in (None, "")}
            analysis.update(explicit)
            if explicit:
                analysis["confidence"], analysis["confidence_reason"] = 100, "Corrected by you."
        return analysis

    async def correct(self, item_id: str, correction: dict) -> dict | None:
        """Apply a user's correction. With a free-text hint the model looks again; otherwise the
        corrected facts are used as-is and only the metadata lookups run."""
        if correction.get("hint"):
            return await self.process(item_id, correction, purpose="correct")
        item = self.db.get_item(item_id)
        if not item:
            return None
        previous = item.get("analysis") if isinstance(item.get("analysis"), dict) else {
            k: item.get(k) for k in ("title", "category", "canonical_url", "subtitle", "summary", "source_platform")
        }

        async def fixed() -> dict:
            return corrected_analysis(previous, correction)

        return await self._run(item_id, "correct", fixed(), corrected=True)

    async def complete_batch_job(self, job: dict, result: Any) -> dict | None:
        """Finish an analysis whose Claude step ran in a Message Batch."""
        params, context = json.loads(job["params"]), json.loads(job["context"])
        router: AnalyzerRouter = self.analyzer

        async def finish() -> dict:
            if getattr(result, "type", None) == "succeeded":
                return await router.finish_batch(params, result.message, context)
            # errored / expired / canceled: run it now at the normal price rather than leave it stuck
            log.warning("Batch request for %s ended as %s; running it in real time", job["item_id"], getattr(result, "type", "?"))
            return router.finish(await router.claude.run(params), context)

        if not self.db.get_item(job["item_id"]):
            # Deleted while queued: still account for what the batch cost.
            if getattr(result, "type", None) == "succeeded":
                self.db.record_runs(job["item_id"], [self._batch_only_run(result.message)], job["purpose"])
            return None
        return await self._run(job["item_id"], job["purpose"], finish(), corrected=job["purpose"] == "correct")

    def _batch_only_run(self, message: Any) -> dict:
        run = Run("claude", model=getattr(message, "model", ""), mode="batch")
        run.add_claude_usage(getattr(message, "usage", None))
        run.cost_usd = claude_cost(run)
        return run.to_dict()

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

    async def _run(self, item_id: str, purpose: str, work, corrected: bool) -> dict | None:
        """Run an identification, record what it cost, and store the result or the error."""
        try:
            analysis = await work
        except Deferred as d:
            self.db.record_runs(item_id, d.context.get("runs", []), purpose)  # OCR / local runs so far
            d.context["runs"] = []
            self.db.add_batch_job(item_id, purpose, d.params, d.context)
            return self.db.get_item(item_id)
        except AnalysisError as e:
            self.db.record_runs(item_id, e.runs, purpose)
            return self.db.update_item(item_id, status="error", error=str(e))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            self.db.record_runs(item_id, getattr(e, "keeper_runs", []), purpose)
            return self.db.update_item(item_id, status="error", error=(
                "The server's Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY and re-analyze."
            ))
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            self.db.record_runs(item_id, getattr(e, "keeper_runs", []), purpose)
            return self.db.update_item(item_id, status="error", error=f"Temporary problem reaching Claude ({type(e).__name__}). Try re-analyzing.")
        except Exception as e:  # keep the item; the user can retry
            log.exception("Processing %s failed", item_id)
            self.db.record_runs(item_id, getattr(e, "keeper_runs", []), purpose)
            return self.db.update_item(item_id, status="error", error=f"{type(e).__name__}: {e}")
        self.db.record_runs(item_id, analysis.pop("_runs", []), purpose)
        try:
            return await self.apply_analysis(item_id, analysis, corrected=corrected)
        except Exception as e:
            log.exception("Storing the analysis for %s failed", item_id)
            return self.db.update_item(item_id, status="error", error=f"{type(e).__name__}: {e}")
