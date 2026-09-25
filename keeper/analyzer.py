"""Screenshot understanding with Claude.

Claude looks at the screenshot, works out what it is actually about (the movie a friend
posted about, the repo in a "10 tools you need" post, the recipe in a reel), uses web
search to pin down the canonical source, and reports back through a strict tool call so
the result always matches the schema below.
"""

import base64
import io
import logging
from typing import Any

import anthropic

log = logging.getLogger(__name__)

CATEGORIES = [
    "movie", "tv_show", "github_repo", "recipe", "book", "music", "podcast", "video",
    "article", "product", "place", "event", "app", "course", "other",
]
PLATFORMS = [
    "facebook", "instagram", "twitter", "threads", "tiktok", "reddit", "youtube", "linkedin",
    "whatsapp", "telegram", "pinterest", "mastodon", "bluesky", "email", "web", "other",
]

# Models that accept the server-side refusal fallback parameter.
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1", "claude-fable-5"}

MAX_IMAGE_EDGE = 2000
MAX_IMAGE_BYTES = 3_500_000

_nstr = {"type": ["string", "null"]}
_strs = {"type": "array", "items": {"type": "string"}}

DETAILS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Type-specific facts. Use null / [] for anything that does not apply or you could not verify.",
    "properties": {
        # film & tv
        "imdb_id": {**_nstr, "description": "e.g. tt0111161"},
        "imdb_rating": {**_nstr, "description": "e.g. '8.1/10'"},
        "rotten_tomatoes": {**_nstr, "description": "Tomatometer, e.g. '94%'"},
        "metacritic": _nstr,
        "genres": _strs,
        "directors": _strs,
        "cast": {**_strs, "description": "Top-billed cast, at most 6"},
        "runtime": {**_nstr, "description": "e.g. '2h 22m' or '45m per episode'"},
        "seasons": _nstr,
        "network_or_studio": _nstr,
        "where_to_watch": {**_strs, "description": "Streaming services, if known"},
        # github
        "github_full_name": {**_nstr, "description": "owner/repo"},
        "programming_language": _nstr,
        # recipes
        "ingredients": _strs,
        "total_time": _nstr,
        "servings": _nstr,
        "cuisine": _nstr,
        "diet": _strs,
        # books / articles / general
        "author": _nstr,
        "publisher": _nstr,
        "published_date": _nstr,
        "price": _nstr,
        "location": _nstr,
        # the post itself
        "posted_by": {**_nstr, "description": "Account/person who shared it in the screenshot"},
        "post_url": {**_nstr, "description": "URL of the social post/page itself, if visible"},
    },
}
DETAILS_SCHEMA["required"] = list(DETAILS_SCHEMA["properties"])
DETAILS_SCHEMA["additionalProperties"] = False

SAVE_TOOL: dict[str, Any] = {
    "name": "save_analysis",
    "description": "Record the final identification of what the screenshot is about. Call exactly once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": CATEGORIES},
            "source_platform": {
                "type": "string", "enum": PLATFORMS,
                "description": "Where the screenshot was taken (the app or site showing the recommendation).",
            },
            "title": {"type": "string", "description": "Canonical name of the thing, e.g. 'The Bear', 'astral-sh/uv', 'Shakshuka'."},
            "subtitle": {**_nstr, "description": "Short qualifier, e.g. '2022 · TV series · FX' or 'Python package manager'."},
            "year": {"type": ["integer", "null"]},
            "summary": {"type": "string", "description": "2-4 sentence description of the thing itself (not of the screenshot)."},
            "canonical_url": {
                **_nstr,
                "description": "Best official URL: IMDb title page for film/TV, github.com repo for code, original recipe page, article URL, etc.",
            },
            "image_url": {**_nstr, "description": "Direct URL to a poster/cover/preview image, only if you actually found one."},
            "links": {
                "type": "array",
                "description": "Other useful links (official site, trailer, streaming page, docs...).",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["label", "url"],
                    "additionalProperties": False,
                },
            },
            "tags": {"type": "array", "items": {"type": "string"}, "description": "5-10 short lowercase retrieval tags (genre, topic, mood, cuisine, tech...)."},
            "screenshot_text": {"type": "string", "description": "The key text visible in the screenshot, condensed (max ~500 chars)."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "details": DETAILS_SCHEMA,
        },
        "additionalProperties": False,
    },
}
SAVE_TOOL["input_schema"]["required"] = list(SAVE_TOOL["input_schema"]["properties"])

SYSTEM_PROMPT = """You catalogue screenshots for a personal "save for later" library.

Each screenshot usually shows a recommendation seen somewhere: a Facebook or Instagram post, a tweet, a web page, a chat message. Your job is to identify the actual thing being recommended (a movie, TV show, GitHub repository, recipe, book, product, article, ...) — not the post about it — and record it with save_analysis.

How to work:
1. Read the screenshot carefully: the app/site chrome tells you the source platform; captions, overlays, handles, and partially visible titles tell you the subject.
2. Use web search to confirm the identity and find the canonical source. For films and TV find the IMDb page and scores; for code find the github.com repository; for recipes find the original recipe page; for articles find the article URL.
3. Only report URLs, ratings and facts you actually saw in search results or the screenshot. Use null rather than guessing.
4. If the screenshot recommends several things, catalogue the most prominent one and mention the others in the summary.
5. Finish by calling save_analysis once. Do not ask the user questions."""


class AnalysisError(Exception):
    pass


def prepare_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Downscale very large screenshots so they fit the API's image limits."""
    try:
        from PIL import Image
    except ImportError:  # Pillow is optional; send the original
        return data, media_type

    img = Image.open(io.BytesIO(data))
    if max(img.size) <= MAX_IMAGE_EDGE and len(data) <= MAX_IMAGE_BYTES and media_type in (
        "image/png", "image/jpeg", "image/webp", "image/gif",
    ):
        return data, media_type
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue(), "image/jpeg"


class ScreenshotAnalyzer:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None, model: str = "claude-opus-5"):
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model

    async def analyze(self, image: bytes, media_type: str, note: str | None = None) -> dict:
        image, media_type = prepare_image(image, media_type)
        prompt = "Identify what this screenshot is recommending and catalogue it."
        if note:
            prompt += f"\n\nThe user added this note when saving it: {note}"
        messages: list[dict] = [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(image).decode()}},
                {"type": "text", "text": prompt},
            ],
        }]
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
            SAVE_TOOL,
        ]
        nudged = False
        for _ in range(8):
            response = await self._create(messages, tools)
            if response.stop_reason == "refusal":
                raise AnalysisError("The model declined to analyze this screenshot.")

            for block in response.content:
                if block.type == "tool_use" and block.name == "save_analysis":
                    return dict(block.input)

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue  # long server-side search turn; resend to let it continue
            if response.stop_reason == "tool_use":
                # A client tool other than save_analysis — we don't have any, so report back.
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content": "Unknown tool."}
                    for b in response.content if b.type == "tool_use"
                ]})
                continue
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content": "Please record your findings now by calling save_analysis."})
        raise AnalysisError("The model did not return an analysis.")

    async def _create(self, messages: list[dict], tools: list[dict]):
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            tools=tools,
            tool_choice={"type": "auto"},
            messages=messages,
        )
        if self.model in FALLBACK_MODELS:
            return await self.client.beta.messages.create(
                **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        return await self.client.messages.create(**kwargs)
