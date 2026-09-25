"""Capturing a shared link: recognize it without a model whenever possible.

Order of evidence, most to least certain:
  1. The URL itself (github.com/owner/repo, imdb.com/title/tt…, themoviedb.org/movie/…)
  2. The page's schema.org JSON-LD (@type Recipe, Movie, Book, NewsArticle, Product, …)
  3. The page's OpenGraph og:type (video.movie, book, article, …)
Recognized links skip the LLM entirely. Anything else goes to the configured model with the
page's reader-view text, or, with no model, becomes a generic card from readability.
"""

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .enrich import Page, _ld_image, _ld_name, _ld_types
from .readability import Article

TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|igshid|igsh|mc_cid|mc_eid|ref_src|ref_url|_hsenc|_hsmi)$", re.I)
# Parameters that are only tracking on specific sites (elsewhere they can matter, e.g. ?s= search).
SITE_TRACKING = {"x.com": {"s", "t"}, "twitter.com": {"s", "t"}, "youtube.com": {"si", "feature", "pp"},
                 "open.spotify.com": {"si"}, "instagram.com": {"img_index"}}
GITHUB_RESERVED = {
    "about", "apps", "collections", "contact", "customer-stories", "enterprise", "events", "explore", "features",
    "login", "marketplace", "new", "notifications", "orgs", "organizations", "pricing", "pulls", "issues", "search",
    "security", "settings", "site", "sponsors", "topics", "trending", "users", "join", "codespaces", "readme",
}
PLATFORM_HOSTS = {
    "youtube.com": "youtube", "youtu.be": "youtube", "instagram.com": "instagram", "facebook.com": "facebook",
    "fb.watch": "facebook", "x.com": "twitter", "twitter.com": "twitter", "reddit.com": "reddit",
    "tiktok.com": "tiktok", "linkedin.com": "linkedin", "threads.net": "threads", "t.me": "telegram",
    "pinterest.com": "pinterest", "bsky.app": "bluesky",
}
LD_CATEGORIES = [  # first match wins, so specific types come before generic ones
    ({"recipe"}, "recipe"), ({"movie"}, "movie"), ({"tvseries", "tvseason", "tvepisode"}, "tv_show"),
    ({"book"}, "book"), ({"podcastepisode", "podcastseries"}, "podcast"),
    ({"musicrecording", "musicalbum", "musicplaylist", "musicgroup"}, "music"),
    ({"softwareapplication", "mobileapplication", "webapplication", "videogame"}, "app"),
    ({"course"}, "course"), ({"event", "musicevent", "festival", "screeningevent"}, "event"),
    ({"restaurant", "cafeorcoffeeshop", "barorpub", "localbusiness", "touristattraction", "place", "hotel", "museum"}, "place"),
    ({"product"}, "product"), ({"videoobject"}, "video"),
    ({"newsarticle", "article", "blogposting", "report", "scholarlyarticle", "techarticle"}, "article"),
]
OG_CATEGORIES = {
    "video.movie": "movie", "video.tv_show": "tv_show", "video.episode": "tv_show", "video.other": "video",
    "book": "book", "books.book": "book", "music.song": "music", "music.album": "music", "music.playlist": "music",
    "product": "product", "og:product": "product", "article": "article", "restaurant.restaurant": "place",
}
URL_TOO_LONG = 2048


def normalize_url(url: str) -> str:
    """Canonical form for de-duplication: https, lowercase host, no fragment, no tracking params."""
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    netloc = host + (f":{parts.port}" if parts.port and parts.port not in (80, 443) else "")
    site = SITE_TRACKING.get(host, set())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING_PARAMS.match(k) and k not in site])
    path = parts.path.rstrip("/") or ""
    if host == "youtu.be" and path:  # youtu.be/ID -> youtube.com/watch?v=ID
        return f"https://youtube.com/watch?v={path.lstrip('/')}"
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def platform_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    for domain, platform in PLATFORM_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return "web"


def _blank() -> dict:
    from .analyzers import blank_analysis  # avoid an import cycle
    return blank_analysis()


def _year(value: Any) -> int | None:
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", str(value or ""))
    return int(m.group(1)) if m else None


def _clean_title(title: str | None, host: str) -> str | None:
    if not title:
        return None
    title = re.sub(r"\s*[|\-–—·]\s*(IMDb|Letterboxd|Goodreads|YouTube|Rotten Tomatoes|GitHub)\s*$", "", title.strip())
    return title or None


def classify(url: str, page: Page | None) -> dict | None:
    """A complete analysis for recognized links, or None if the link needs a model (or a generic card)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    segments = [s for s in parts.path.split("/") if s]
    out = _blank()
    out.update(canonical_url=url, source_platform=platform_of(url), confidence=0)
    reason = ""

    # 1. URL patterns
    if host == "github.com" and len(segments) >= 2 and segments[0].lower() not in GITHUB_RESERVED:
        full = f"{segments[0]}/{segments[1].removesuffix('.git')}"
        out.update(category="github_repo", title=full, canonical_url=f"https://github.com/{full}", confidence=97)
        out["details"]["github_full_name"] = full
        reason = f"GitHub repository link ({full})"
    elif host.endswith("imdb.com") and (m := re.search(r"/title/(tt\d{7,9})", parts.path)):
        out.update(category="movie", canonical_url=f"https://www.imdb.com/title/{m.group(1)}/", confidence=95)
        out["details"]["imdb_id"] = m.group(1)
        reason = f"IMDb title link ({m.group(1)})"
    elif host.endswith("themoviedb.org") and len(segments) >= 2 and segments[0] in ("movie", "tv"):
        tmdb_id = re.match(r"\d+", segments[1])
        if tmdb_id:
            out.update(category="movie" if segments[0] == "movie" else "tv_show", confidence=95)
            out["_tmdb"] = [segments[0], int(tmdb_id.group(0))]
            reason = "TMDB link"
    elif host.endswith("letterboxd.com") and len(segments) >= 2 and segments[0] == "film":
        out.update(category="movie", confidence=90)
        reason = "Letterboxd film link"
    elif host.endswith("goodreads.com") and len(segments) >= 3 and segments[:2] == ["book", "show"]:
        out.update(category="book", confidence=90)
        reason = "Goodreads book link"
    elif host == "open.spotify.com" and segments and segments[0] in ("track", "album", "playlist", "artist", "episode", "show"):
        out.update(category="podcast" if segments[0] in ("episode", "show") else "music", confidence=90)
        reason = "Spotify link"
    elif host in ("apps.apple.com", "play.google.com") and ("app" in segments or "apps" in segments):
        out.update(category="app", confidence=90)
        reason = "app store link"
    elif host == "youtube.com" and (parts.path == "/watch" or segments[:1] == ["shorts"]):
        out.update(category="video", confidence=85)
        reason = "YouTube video link"

    # 2. structured data / 3. OpenGraph, from the page
    ld_node = None
    if page:
        for types, category in LD_CATEGORIES:
            ld_node = next((n for n in page.ld if types & set(_ld_types(n))), None)
            if ld_node:
                break
        if ld_node and not reason:
            out.update(category=category, confidence=90)
            reason = f"the page's structured data (schema.org {_ld_types(ld_node)[0]})"
        # Structured data can refine a URL guess (e.g. an IMDb title that is a TV series).
        if ld_node and reason.startswith("IMDb") and "tvseries" in _ld_types(ld_node):
            out["category"] = "tv_show"
        og_type = (page.meta.get("og:type") or "").lower()
        if not reason and og_type in OG_CATEGORIES:
            out.update(category=OG_CATEGORIES[og_type], confidence=80)
            reason = f"the page type (og:type {og_type})"
        if reason.startswith("IMDb") and og_type == "video.tv_show":
            out["category"] = "tv_show"

    if not reason:
        return None
    if out["category"] == "article" and not ld_node:
        return None  # plain og:type=article is on nearly every page; let the model or readability decide

    if page:
        meta = page.meta
        node = ld_node or {}
        if out["category"] != "github_repo":
            out["title"] = (_ld_name(node.get("name")) or node.get("headline") or _clean_title(meta.get("og:title"), host)
                            or _clean_title(page.title, host) or out["title"])
        out["summary"] = node.get("description") or meta.get("og:description") or meta.get("description") or ""
        out["image_url"] = _ld_image(node.get("image")) or meta.get("og:image")
        out["year"] = _year(node.get("datePublished") or node.get("dateCreated") or node.get("startDate")
                            or meta.get("video:release_date")) if out["category"] not in ("github_repo", "article") else None
        if isinstance(node.get("keywords"), str):
            out["tags"] = [k.strip().lower() for k in node["keywords"].split(",") if k.strip()][:5]
        elif isinstance(node.get("keywords"), list):
            out["tags"] = [str(k).lower() for k in node["keywords"]][:5]
        if node.get("author"):
            out["details"]["author"] = _ld_name(node["author"])
    if not out["title"]:
        out["title"] = segments[-1].replace("-", " ").replace("_", " ") if segments else host
    out["confidence_reason"] = f"Recognized from {reason}."
    out["_analyzer"] = ["link"]
    return out


def generic(url: str, page: Page | None, article: Article | None, note: str | None = None) -> dict:
    """A sensible card for an unrecognized link, from the page itself (no model)."""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    out = _blank()
    out.update(canonical_url=url, source_platform=platform_of(url), _analyzer=["link", "readability"])
    if page is None:
        out.update(title=host + urlsplit(url).path.rstrip("/"), category="other", confidence=20,
                   confidence_reason="Couldn't load the page (login wall, blocked, or offline). Please check.")
        return out
    meta = page.meta
    title = (article.title if article else None) or _clean_title(meta.get("og:title"), host) or _clean_title(page.title, host) or host
    is_article = bool(article and article.is_article)
    out.update(
        title=title,
        category="article" if is_article else "other",
        summary=(article.excerpt if article else None) or meta.get("og:description") or meta.get("description") or note or "",
        image_url=(article.image if article else None) or meta.get("og:image"),
        confidence=65 if is_article else 45,
        confidence_reason=("A readable article: title and summary taken from the page itself (no AI model)."
                           if is_article else "Unrecognized page: title and description taken from the page itself (no AI model)."),
        tags=[host.split(".")[-2]] if host.count(".") >= 1 else [],
    )
    if article:
        out["details"]["author"] = article.author
        out["details"]["published_date"] = article.published_date
    return out


def page_context(url: str, page: Page | None, article: Article | None, max_chars: int = 12_000) -> str:
    """What a model sees for a shared link (instead of a screenshot)."""
    if page is None:
        return f"\n\nThe page could not be loaded (it may need a login). URL: {url}"
    parts = [f"URL: {page.url}"]
    for label, value in (("Title", (article.title if article else None) or page.meta.get("og:title") or page.title),
                         ("Site", (article.site_name if article else None) or page.meta.get("og:site_name")),
                         ("Description", page.meta.get("og:description") or page.meta.get("description")),
                         ("Author", article.author if article else None),
                         ("Published", article.published_date if article else None)):
        if value:
            parts.append(f"{label}: {value}")
    text = (article.text if article else "")[:max_chars]
    if text:
        parts.append(f"Main text:\n{text}")
    return "\n\n<page>\n" + "\n".join(parts) + "\n</page>"
