"""Analyzer backends and the router that picks between them.

  claude  - Claude with web search (most accurate; needs ANTHROPIC_API_KEY)
  local   - an on-prem LLM behind an OpenAI-compatible API (Ollama, vLLM, LM Studio, llama.cpp)
  hybrid  - local first; escalate to Claude only when the local model isn't confident
  ocr     - no LLM at all: OCR + rules (fast, private, rough)

All of them get the OCR pre-pass (see ocr.py) and return the same dict shape as
analyzer.SAVE_TOOL, so the rest of the pipeline doesn't care which one ran.
"""

import base64
import json
import logging
import re
from typing import Any

import httpx

from .analyzer import CATEGORIES, PLATFORMS, SAVE_TOOL, SYSTEM_PROMPT, AnalysisError, ScreenshotAnalyzer, correction_prompt, prepare_image
from .config import Settings
from .ocr import Ocr, OcrResult, Signals, extract_signals

log = logging.getLogger(__name__)

SCHEMA = SAVE_TOOL["input_schema"]
DETAIL_KEYS = list(SCHEMA["properties"]["details"]["properties"])
ARRAY_DETAILS = {k for k, v in SCHEMA["properties"]["details"]["properties"].items() if v.get("type") == "array"}

LOCAL_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    "2. Use web search to confirm the identity and find the canonical source.",
    "2. You have no web access. Use what you know plus the screenshot and OCR text to name the thing and, "
    "when you are sure of it, its canonical URL.",
).replace(
    "Finish by calling save_analysis once. Do not ask the user questions.",
    "Reply with a single JSON object matching the requested schema. Do not ask the user questions.",
).replace(
    "Only report URLs, ratings and facts you actually saw in search results or the screenshot.",
    "Only report URLs, ratings and facts you saw in the screenshot or are certain of; ratings are looked up later.",
)


def blank_analysis() -> dict:
    return {
        "category": "other", "source_platform": "other", "title": "", "subtitle": None, "year": None,
        "summary": "", "canonical_url": None, "image_url": None, "links": [], "tags": [],
        "screenshot_text": "", "confidence": 0, "confidence_reason": "", "alternatives": [],
        "details": {k: ([] if k in ARRAY_DETAILS else None) for k in DETAIL_KEYS},
    }


def normalize(raw: dict) -> dict:
    """Coerce a model's (possibly partial or sloppy) output into the analysis shape."""
    out = blank_analysis()
    for k in out:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    out["details"] = {k: details.get(k, [] if k in ARRAY_DETAILS else None) for k in DETAIL_KEYS}
    for k in ARRAY_DETAILS:
        v = out["details"][k]
        out["details"][k] = [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])

    cat = str(out["category"]).lower().replace(" ", "_").replace("-", "_")
    out["category"] = {"tv": "tv_show", "tv_series": "tv_show", "series": "tv_show", "film": "movie",
                       "repo": "github_repo", "github": "github_repo"}.get(cat, cat)
    if out["category"] not in CATEGORIES:
        out["category"] = "other"
    plat = str(out["source_platform"]).lower()
    out["source_platform"] = {"x": "twitter", "fb": "facebook", "ig": "instagram"}.get(plat, plat)
    if out["source_platform"] not in PLATFORMS:
        out["source_platform"] = "other"
    try:
        c = out["confidence"]
        c = {"high": 90, "medium": 70, "low": 40}.get(c, c) if isinstance(c, str) else c
        c = float(c)
        out["confidence"] = int(round(c * 100 if 0 < c <= 1 else c))  # some models answer 0.8
        out["confidence"] = max(0, min(100, out["confidence"]))
    except (TypeError, ValueError):
        out["confidence"] = 30
    try:
        out["year"] = int(out["year"]) if out["year"] not in (None, "") else None
    except (TypeError, ValueError):
        out["year"] = None
    out["tags"] = [str(t) for t in out["tags"]] if isinstance(out["tags"], list) else []
    out["links"] = [l for l in out["links"] if isinstance(l, dict) and l.get("url")] if isinstance(out["links"], list) else []
    out["alternatives"] = [
        {"title": str(a.get("title")), "category": a.get("category") if a.get("category") in CATEGORIES else "other",
         "year": a.get("year") if isinstance(a.get("year"), int) else None,
         "canonical_url": a.get("canonical_url"), "why": str(a.get("why") or "")}
        for a in (out["alternatives"] if isinstance(out["alternatives"], list) else [])
        if isinstance(a, dict) and a.get("title")
    ][:3]
    for k in ("title", "summary", "screenshot_text", "confidence_reason"):
        out[k] = str(out[k] or "")
    return out


def parse_json(text: str) -> dict:
    """Pull a JSON object out of a reply that may wrap it in prose or ``` fences."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AnalysisError("The local model did not return JSON.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AnalysisError(f"The local model returned invalid JSON: {e}") from e


def hints_prompt(ocr: OcrResult | None, signals: Signals | None) -> str:
    if not ocr or not ocr.lines:
        return ""
    text = ocr.text
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    out = f"\n\nText found in the screenshot by OCR (may contain recognition errors, e.g. missing spaces):\n<ocr>\n{text}\n</ocr>"
    if signals and signals.as_hint_text():
        out += f"\nAutomatic clues (may be wrong): {signals.as_hint_text()}"
    return out


class LocalLLMAnalyzer:
    """On-prem model via the OpenAI-compatible /chat/completions API."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        if not settings.local_llm_url:
            raise ValueError("LOCAL_LLM_URL is not set")
        self.url = settings.local_llm_url.rstrip("/") + "/chat/completions"
        self.model = settings.local_llm_model
        self.vision = settings.local_llm_vision
        self.timeout = settings.local_llm_timeout
        self.headers = {"Authorization": f"Bearer {settings.local_llm_api_key}"} if settings.local_llm_api_key else {}
        self.http = http or httpx.AsyncClient()
        self._format_mode = "json_schema"  # downgraded automatically if the server doesn't support it

    @property
    def label(self) -> str:
        return f"local:{self.model}"

    async def analyze(self, image: bytes, media_type: str, note: str | None = None,
                      correction: dict | None = None, hints: str = "") -> dict:
        prompt = "Identify what this screenshot is recommending and catalogue it."
        if not self.vision:
            prompt = "Identify what this screenshot is recommending, using only the OCR text below, and catalogue it."
        if note:
            prompt += f"\n\nThe user added this note when saving it: {note}"
        if correction:
            prompt += "\n\n" + correction_prompt(correction)
        prompt += hints
        prompt += ("\n\nAnswer with JSON only, with these keys: " + ", ".join(SCHEMA["properties"]) +
                   ". confidence is an integer 0-100. details has these keys: " + ", ".join(DETAIL_KEYS) + ".")

        content: list[dict] = [{"type": "text", "text": prompt}]
        if self.vision:
            data, mt = prepare_image(image, media_type)
            content.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{base64.b64encode(data).decode()}"}})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": LOCAL_SYSTEM_PROMPT}, {"role": "user", "content": content}],
            "temperature": 0.1,
            "stream": False,
        }
        return normalize(parse_json(await self._complete(body)))

    FORMATS = ("json_schema", "json_object", "none")

    async def _complete(self, body: dict) -> str:
        # Prefer schema-constrained decoding; step down for servers that don't support it,
        # and remember what worked so later requests skip the failed attempts.
        for mode in self.FORMATS[self.FORMATS.index(self._format_mode):]:
            req = dict(body)
            if mode == "json_schema":
                req["response_format"] = {"type": "json_schema", "json_schema": {"name": "save_analysis", "schema": SCHEMA, "strict": True}}
            elif mode == "json_object":
                req["response_format"] = {"type": "json_object"}
            try:
                r = await self.http.post(self.url, json=req, headers=self.headers, timeout=self.timeout)
            except httpx.HTTPError as e:
                raise AnalysisError(f"Can't reach the local LLM at {self.url}: {e!r}") from e
            if r.status_code in (400, 422) and mode != "none":
                log.info("Local LLM rejected response_format=%s (%s); trying a simpler format", mode, r.text[:200])
                continue
            if r.status_code == 404:
                raise AnalysisError(f"Local LLM: model or endpoint not found ({r.text[:200]}). Is '{self.model}' pulled?")
            if r.status_code >= 400:
                raise AnalysisError(f"Local LLM error {r.status_code}: {r.text[:300]}")
            self._format_mode = mode
            try:
                return r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError, TypeError) as e:
                raise AnalysisError(f"Unexpected reply from the local LLM: {r.text[:300]}") from e
        raise AnalysisError("The local LLM rejected every request format.")


def rules_analysis(ocr: OcrResult | None, signals: Signals | None, note: str | None = None) -> dict:
    """No-LLM identification from OCR + rules. Rough by design: always low confidence."""
    out = blank_analysis()
    if not ocr or not ocr.lines:
        out["confidence_reason"] = "No text could be read from the screenshot."
        out["title"] = "Screenshot"
        return out
    s = signals or extract_signals(ocr)
    out["screenshot_text"] = ocr.text[:500]
    out["source_platform"] = s.platform or "other"
    out["details"]["posted_by"] = s.handles[0] if s.handles else None
    reason = []
    if s.github_repos:
        repo = s.github_repos[0]
        out.update(category="github_repo", title=repo, canonical_url=f"https://github.com/{repo}", confidence=75)
        out["details"]["github_full_name"] = repo
        reason.append(f"a GitHub link ({repo}) is visible")
    elif s.imdb_ids:
        out.update(category="movie", canonical_url=f"https://www.imdb.com/title/{s.imdb_ids[0]}/", confidence=65)
        out["details"]["imdb_id"] = s.imdb_ids[0]
        out["title"] = s.title_candidates[0] if s.title_candidates else s.imdb_ids[0]
        reason.append("an IMDb id is visible")
    else:
        out["category"] = s.category or "other"
        out["title"] = s.title_candidates[0] if s.title_candidates else ocr.lines[0].text
        out["canonical_url"] = next((u if u.startswith("http") else f"https://{u}" for u in s.urls), None)
        out["confidence"] = 35 if s.category else 20
        reason.append(f"guessed from {'vocabulary and ' if s.category else ''}the most prominent text")
    out["summary"] = note or ""
    out["tags"] = [t for t in (out["category"] if out["category"] != "other" else None, s.platform) if t]
    out["confidence_reason"] = "OCR + rules only (no AI model): " + ", ".join(reason) + ". Please check."
    return out


class AnalyzerRouter:
    """Runs OCR once, then the configured backend(s)."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient, ocr: Ocr | None = None,
                 claude: Any = None, local: Any = None):
        self.mode = settings.resolved_analyzer()
        self.escalate_below = settings.escalate_below
        self.ocr = ocr if ocr is not None else Ocr(settings.ocr_engine, settings.ocr_langs)
        self.claude = claude
        self.local = local
        if self.mode in ("claude", "hybrid") and self.claude is None:
            if not settings.anthropic_api_key:
                raise ValueError(f"KEEPER_ANALYZER={self.mode} needs ANTHROPIC_API_KEY")
            self.claude = ScreenshotAnalyzer(model=settings.model)
        if self.mode in ("local", "hybrid") and self.local is None:
            self.local = LocalLLMAnalyzer(settings, http)
        log.info("Analyzer: %s (OCR: %s)", self.mode, settings.ocr_engine)

    async def analyze(self, image: bytes, media_type: str, note: str | None = None, correction: dict | None = None) -> dict:
        ocr = await self.ocr.read(image)
        signals = extract_signals(ocr) if ocr and ocr.lines else None
        hints = hints_prompt(ocr, signals)
        used: list[str] = ["ocr"] if ocr and ocr.lines else []

        if self.mode == "ocr":
            result = rules_analysis(ocr, signals, note)
            used.append("rules")
        elif self.mode == "claude":
            result = await self.claude.analyze(image, media_type, note=note, correction=correction, hints=hints)
            used.append("claude")
        elif self.mode == "local":
            result = await self.local.analyze(image, media_type, note=note, correction=correction, hints=hints)
            used.append(self.local.label)
        else:  # hybrid
            result = None
            try:
                result = await self.local.analyze(image, media_type, note=note, correction=correction, hints=hints)
                used.append(self.local.label)
            except AnalysisError as e:
                log.warning("Local model failed, asking Claude: %s", e)
            if result is None or result.get("confidence", 0) < self.escalate_below:
                result = await self.claude.analyze(image, media_type, note=note, correction=correction, hints=hints)
                used.append("claude")

        result["_analyzer"] = used
        if ocr and ocr.lines:
            result["_ocr_text"] = ocr.text
            if not result.get("screenshot_text"):
                result["screenshot_text"] = ocr.text[:500]
        return result
