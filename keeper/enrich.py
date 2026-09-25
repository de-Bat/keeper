"""Type-specific metadata enrichment from public sources.

Each enricher gets the analysis produced by Claude and returns an `Enrichment` patch.
They are best-effort: a missing API key or a failed request just means less metadata.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from . import readability
from .config import Settings
from .fetch import MAX_BYTES, BlockedURL, safe_get

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TMDB_IMG = "https://image.tmdb.org/t/p/w500"


@dataclass
class Enrichment:
    metadata: dict[str, Any] = field(default_factory=dict)
    canonical_url: str | None = None
    image_url: str | None = None
    summary: str | None = None
    subtitle: str | None = None
    links: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    # Name of the thing the source matched; used to double-check non-Claude identifications.
    matched_title: str | None = None


# ---------------------------------------------------------------------------
# GitHub


GITHUB_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.I)


def github_full_name(analysis: dict) -> str | None:
    details = analysis.get("details") or {}
    candidates = [details.get("github_full_name"), analysis.get("canonical_url"), analysis.get("title")]
    candidates += [l.get("url") for l in analysis.get("links") or []]
    for c in candidates:
        if not c:
            continue
        m = GITHUB_RE.search(c)
        if m:
            return f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
        if re.fullmatch(r"[\w.-]+/[\w.-]+", c.strip()):
            return c.strip()
    return None


async def enrich_github(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    full_name = github_full_name(analysis)
    if not full_name:
        return None
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    r = await http.get(f"https://api.github.com/repos/{full_name}", headers=headers)
    if r.status_code != 200:
        log.info("GitHub lookup for %s failed: %s", full_name, r.status_code)
        return None
    repo = r.json()
    meta = {
        "github_full_name": repo["full_name"],
        "stars": repo.get("stargazers_count"),
        "forks": repo.get("forks_count"),
        "open_issues": repo.get("open_issues_count"),
        "programming_language": repo.get("language"),
        "topics": repo.get("topics") or [],
        "license": (repo.get("license") or {}).get("spdx_id"),
        "last_push": repo.get("pushed_at"),
        "archived": repo.get("archived"),
        "homepage": repo.get("homepage") or None,
    }
    links = [{"label": "Homepage", "url": repo["homepage"]}] if repo.get("homepage") else []
    tags = list((repo.get("topics") or [])[:6])
    if repo.get("language"):
        tags.append(repo["language"])
    return Enrichment(
        metadata=meta,
        canonical_url=repo["html_url"],
        image_url=f"https://opengraph.githubassets.com/1/{repo['full_name']}",
        subtitle=repo.get("description"),
        links=links,
        tags=tags,
        source="github",
        matched_title=repo["full_name"],
    )


# ---------------------------------------------------------------------------
# Movies & TV: TMDB (+ OMDb for IMDb / RT / Metacritic scores)


def _tmdb_auth(key: str) -> tuple[dict, dict]:
    # v4 read tokens are JWTs; v3 keys are short hex strings.
    if key.startswith("eyJ"):
        return {"Authorization": f"Bearer {key}"}, {}
    return {}, {"api_key": key}


async def enrich_screen(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    is_tv = analysis.get("category") == "tv_show"
    details = analysis.get("details") or {}
    imdb_id = details.get("imdb_id")
    if not imdb_id:
        m = re.search(r"imdb\.com/title/(tt\d+)", analysis.get("canonical_url") or "")
        imdb_id = m.group(1) if m else None
    out = Enrichment(source="tmdb")

    if settings.tmdb_api_key:
        headers, params = _tmdb_auth(settings.tmdb_api_key)
        base = "https://api.themoviedb.org/3"
        kind = "tv" if is_tv else "movie"
        tmdb_id = None
        if analysis.get("_tmdb"):  # a shared themoviedb.org link
            kind, tmdb_id = analysis["_tmdb"]
        if imdb_id and not tmdb_id:
            r = await http.get(f"{base}/find/{imdb_id}", headers=headers, params={**params, "external_source": "imdb_id"})
            if r.status_code == 200:
                data = r.json()
                order = ("tv", "movie") if is_tv else ("movie", "tv")
                for k in order:
                    if data.get(f"{k}_results"):
                        tmdb_id, kind = data[f"{k}_results"][0]["id"], k
                        break
        if not tmdb_id and analysis.get("title"):
            q = {**params, "query": analysis["title"]}
            if analysis.get("year"):
                q["first_air_date_year" if is_tv else "year"] = analysis["year"]
            r = await http.get(f"{base}/search/{kind}", headers=headers, params=q)
            if r.status_code == 200 and r.json().get("results"):
                tmdb_id = r.json()["results"][0]["id"]
        if tmdb_id:
            r = await http.get(
                f"{base}/{kind}/{tmdb_id}", headers=headers,
                params={**params, "append_to_response": "credits,external_ids,videos,watch/providers"},
            )
            if r.status_code == 200:
                _apply_tmdb(out, r.json(), kind, os.environ.get("KEEPER_REGION", "US"))
                imdb_id = out.metadata.get("imdb_id") or imdb_id

    if settings.omdb_api_key and imdb_id:
        r = await http.get("https://www.omdbapi.com/", params={"i": imdb_id, "apikey": settings.omdb_api_key})
        if r.status_code == 200 and r.json().get("Response") == "True":
            _apply_omdb(out, r.json())
            out.source = "tmdb+omdb" if out.metadata.get("tmdb_id") else "omdb"

    if imdb_id:
        out.metadata["imdb_id"] = imdb_id
        out.canonical_url = f"https://www.imdb.com/title/{imdb_id}/"
    return out if (out.metadata or out.canonical_url) else None


def _apply_tmdb(out: Enrichment, d: dict, kind: str, region: str) -> None:
    credits = d.get("credits") or {}
    meta = out.metadata
    meta["tmdb_id"] = d["id"]
    out.matched_title = d.get("title") or d.get("name")
    meta["tmdb_rating"] = f"{d['vote_average']:.1f}/10" if d.get("vote_average") else None
    meta["genres"] = [g["name"] for g in d.get("genres") or []]
    meta["cast"] = [c["name"] for c in (credits.get("cast") or [])[:6]]
    meta["tagline"] = d.get("tagline") or None
    meta["imdb_id"] = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
    if kind == "movie":
        meta["directors"] = [c["name"] for c in credits.get("crew") or [] if c.get("job") == "Director"]
        meta["release_date"] = d.get("release_date")
        if d.get("runtime"):
            meta["runtime"] = f"{d['runtime'] // 60}h {d['runtime'] % 60}m"
        studios = d.get("production_companies") or []
        meta["network_or_studio"] = studios[0]["name"] if studios else None
    else:
        meta["creators"] = [c["name"] for c in d.get("created_by") or []]
        meta["first_air_date"] = d.get("first_air_date")
        meta["seasons"] = str(d["number_of_seasons"]) if d.get("number_of_seasons") else None
        meta["episodes"] = d.get("number_of_episodes")
        meta["status"] = d.get("status")
        nets = d.get("networks") or []
        meta["network_or_studio"] = nets[0]["name"] if nets else None
        if d.get("episode_run_time"):
            meta["runtime"] = f"{d['episode_run_time'][0]}m per episode"
    providers = ((d.get("watch/providers") or {}).get("results") or {}).get(region) or {}
    if providers.get("flatrate"):
        meta["where_to_watch"] = [p["provider_name"] for p in providers["flatrate"]]
    if providers.get("link"):
        out.links.append({"label": f"Where to watch ({region})", "url": providers["link"]})
    for v in (d.get("videos") or {}).get("results") or []:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            out.links.append({"label": "Trailer", "url": f"https://www.youtube.com/watch?v={v['key']}"})
            break
    out.links.append({"label": "TMDB", "url": f"https://www.themoviedb.org/{kind}/{d['id']}"})
    if d.get("poster_path"):
        out.image_url = TMDB_IMG + d["poster_path"]
    if d.get("overview"):
        out.summary = d["overview"]
    out.tags.extend(g.lower() for g in meta["genres"])


def _apply_omdb(out: Enrichment, d: dict) -> None:
    meta = out.metadata
    if d.get("imdbRating") not in (None, "N/A"):
        meta["imdb_rating"] = f"{d['imdbRating']}/10"
        if d.get("imdbVotes") not in (None, "N/A"):
            meta["imdb_votes"] = d["imdbVotes"]
    for r in d.get("Ratings") or []:
        if r.get("Source") == "Rotten Tomatoes":
            meta["rotten_tomatoes"] = r["Value"]
        elif r.get("Source") == "Metacritic":
            meta["metacritic"] = r["Value"]
    for key, name in (("Rated", "rated"), ("Awards", "awards")):
        if d.get(key) not in (None, "N/A"):
            meta[name] = d[key]
    if not out.image_url and d.get("Poster") not in (None, "N/A"):
        out.image_url = d["Poster"]
    if not meta.get("genres") and d.get("Genre") not in (None, "N/A"):
        meta["genres"] = [g.strip() for g in d["Genre"].split(",")]


# ---------------------------------------------------------------------------
# Books: Open Library (no key needed)


async def enrich_book(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    if not analysis.get("title"):
        return None
    params = {"title": analysis["title"], "limit": 1, "fields": "key,title,author_name,first_publish_year,cover_i,isbn,number_of_pages_median,subject,ratings_average"}
    author = (analysis.get("details") or {}).get("author")
    if author:
        params["author"] = author
    r = await http.get("https://openlibrary.org/search.json", params=params)
    if r.status_code != 200 or not r.json().get("docs"):
        return None
    doc = r.json()["docs"][0]
    meta = {
        "author": ", ".join(doc.get("author_name") or []) or None,
        "first_published": doc.get("first_publish_year"),
        "pages": doc.get("number_of_pages_median"),
        "isbn": (doc.get("isbn") or [None])[0],
        "openlibrary_rating": f"{doc['ratings_average']:.1f}/5" if doc.get("ratings_average") else None,
    }
    return Enrichment(
        metadata=meta,
        image_url=f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg" if doc.get("cover_i") else None,
        links=[{"label": "Open Library", "url": f"https://openlibrary.org{doc['key']}"}],
        tags=[s.lower() for s in (doc.get("subject") or [])[:4]],
        source="openlibrary",
        matched_title=doc.get("title"),
    )


# ---------------------------------------------------------------------------
# Web pages: schema.org Recipe JSON-LD and OpenGraph


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.ld_json: list[str] = []
        self.title = ""
        self._in_ld = False
        self._in_title = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content") and key.lower() not in self.meta:
                self.meta[key.lower()] = a["content"]
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.ld_json.append("".join(self._buf))
            self._in_ld = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._in_title:
            self.title += data


@dataclass
class Page:
    url: str
    meta: dict[str, str]
    ld: list[dict]
    title: str
    html: str = ""


_PAGE_CACHE: dict[str, tuple[float, Page | None]] = {}
PAGE_CACHE_SECONDS = 120


async def fetch_page(url: str, http: httpx.AsyncClient) -> Page | None:
    hit = _PAGE_CACHE.get(url)
    if hit and time.monotonic() - hit[0] < PAGE_CACHE_SECONDS:
        return hit[1]
    page = await _fetch_page(url, http)
    if len(_PAGE_CACHE) > 200:
        _PAGE_CACHE.clear()
    _PAGE_CACHE[url] = (time.monotonic(), page)
    return page


async def _fetch_page(url: str, http: httpx.AsyncClient) -> Page | None:
    try:
        r = await safe_get(http, url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml"})
    except (httpx.HTTPError, BlockedURL) as e:
        log.info("Fetching %s failed: %s", url, e)
        return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return None
    html = r.text[:MAX_BYTES]
    p = _PageParser()
    p.feed(html)
    ld: list[dict] = []
    for raw in p.ld_json:
        try:
            ld.extend(_walk_ld(json.loads(raw)))
        except json.JSONDecodeError:
            continue
    return Page(url=str(r.url), meta=p.meta, ld=ld, title=p.title.strip(), html=html)


def _walk_ld(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [x for n in node for x in _walk_ld(n)]
    if isinstance(node, dict):
        out = [node]
        if "@graph" in node:
            out += _walk_ld(node["@graph"])
        return out
    return []


def _ld_types(node: dict) -> list[str]:
    t = node.get("@type")
    return [x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def _ld_image(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _ld_image(value[0])
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl")
    return None


def _ld_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        names = [n for n in (_ld_name(v) for v in value) if n]
        return ", ".join(names) or None
    if isinstance(value, dict):
        return value.get("name")
    return None


def iso_duration(value: str | None) -> str | None:
    """PT1H30M -> '1h 30m'."""
    if not value or not isinstance(value, str):
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", value.strip())
    if not m:
        return value
    d, h, mins = (int(x) if x else 0 for x in m.groups())
    h += d * 24
    parts = [f"{h}h" if h else "", f"{mins}m" if mins else ""]
    return " ".join(p for p in parts if p) or None


def _instructions(value: Any) -> list[str]:
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"\n+", value) if s.strip()]
    steps: list[str] = []
    for v in value if isinstance(value, list) else [value]:
        if isinstance(v, str):
            steps.append(v.strip())
        elif isinstance(v, dict):
            if "itemListElement" in v:
                steps.extend(_instructions(v["itemListElement"]))
            elif v.get("text"):
                steps.append(re.sub(r"\s+", " ", v["text"]).strip())
    return steps


def recipe_from_page(page: Page) -> Enrichment | None:
    recipe = next((n for n in page.ld if "recipe" in _ld_types(n)), None)
    if not recipe:
        return None
    rating = recipe.get("aggregateRating") or {}
    nutrition = recipe.get("nutrition") or {}
    yield_ = recipe.get("recipeYield")
    if isinstance(yield_, list):
        yield_ = next((str(y) for y in yield_ if not str(y).isdigit()), str(yield_[0]) if yield_ else None)
    meta = {
        "ingredients": [re.sub(r"\s+", " ", i).strip() for i in recipe.get("recipeIngredient") or [] if isinstance(i, str)],
        "instructions": _instructions(recipe.get("recipeInstructions")),
        "prep_time": iso_duration(recipe.get("prepTime")),
        "cook_time": iso_duration(recipe.get("cookTime")),
        "total_time": iso_duration(recipe.get("totalTime")),
        "servings": str(yield_) if yield_ else None,
        "cuisine": _ld_name(recipe.get("recipeCuisine")),
        "course": _ld_name(recipe.get("recipeCategory")),
        "author": _ld_name(recipe.get("author")),
        "calories": nutrition.get("calories") if isinstance(nutrition, dict) else None,
        "rating": f"{float(rating['ratingValue']):.1f}/5" if rating.get("ratingValue") else None,
        "rating_count": rating.get("ratingCount") or rating.get("reviewCount"),
    }
    tags = [t.strip().lower() for t in re.split(r",", recipe.get("keywords") or "") if t.strip()][:5] if isinstance(recipe.get("keywords"), str) else []
    return Enrichment(
        metadata=meta,
        canonical_url=page.url,
        image_url=_absolute(page.url, _ld_image(recipe.get("image"))),
        summary=recipe.get("description") or None,
        tags=tags,
        source="schema.org/Recipe",
        matched_title=recipe.get("name"),
    )


def opengraph_from_page(page: Page) -> Enrichment:
    m = page.meta
    meta = {
        "site_name": m.get("og:site_name"),
        "page_title": m.get("og:title") or m.get("twitter:title") or page.title or None,
        "author": m.get("author") or m.get("article:author"),
        "published_date": m.get("article:published_time"),
    }
    article = next((n for n in page.ld if {"article", "newsarticle", "blogposting"} & set(_ld_types(n))), None)
    if article:
        meta["author"] = meta["author"] or _ld_name(article.get("author"))
        meta["published_date"] = meta["published_date"] or article.get("datePublished")
    # og:description goes into metadata rather than overriding Claude's summary.
    desc = m.get("og:description") or m.get("description")
    if desc:
        meta["page_description"] = desc
    return Enrichment(
        metadata=meta,
        canonical_url=m.get("og:url") or page.url,
        image_url=_absolute(page.url, m.get("og:image") or m.get("twitter:image")),
        source="opengraph",
    )


def _absolute(base: str, url: str | None) -> str | None:
    return urljoin(base, url) if url else None


async def enrich_web(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> Enrichment | None:
    url = analysis.get("canonical_url")
    if not url or not url.startswith("http"):
        return None
    page = await fetch_page(url, http)
    if not page:
        return None
    if analysis.get("category") == "recipe":
        found = recipe_from_page(page)
        if found:
            return found
    return with_readability(opengraph_from_page(page), page)


def with_readability(e: Enrichment, page: Page) -> Enrichment:
    """Add the page's main content (reader view): excerpt, byline, reading time, full text."""
    article = readability.extract(page.html, page.url)
    if not article:
        return e
    m = e.metadata
    m["author"] = m.get("author") or article.author
    m["site_name"] = m.get("site_name") or article.site_name
    m["published_date"] = m.get("published_date") or article.published_date
    m["page_title"] = m.get("page_title") or article.title
    if article.excerpt:
        m["excerpt"] = article.excerpt
    if article.word_count:
        m["word_count"] = article.word_count
        m["reading_time"] = f"{article.reading_minutes} min read"
        m["article_text"] = article.text  # hidden in the UI; makes the article searchable
    if article.language:
        m["language"] = article.language
    e.image_url = e.image_url or article.image
    e.source = "opengraph+readability"
    return e


# ---------------------------------------------------------------------------


ENRICHERS = {
    "github_repo": [enrich_github],
    "movie": [enrich_screen],
    "tv_show": [enrich_screen],
    "book": [enrich_book, enrich_web],
    "recipe": [enrich_web],
}
DEFAULT_ENRICHERS = [enrich_web]


async def run_enrichers(analysis: dict, settings: Settings, http: httpx.AsyncClient) -> list[Enrichment]:
    results = []
    if not settings.enrich:
        return results
    for fn in ENRICHERS.get(analysis.get("category"), DEFAULT_ENRICHERS):
        try:
            e = await fn(analysis, settings, http)
        except Exception:  # enrichment is best-effort
            log.exception("Enricher %s failed", fn.__name__)
            continue
        if e:
            results.append(e)
    return results
