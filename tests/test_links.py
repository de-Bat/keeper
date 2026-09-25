"""Capturing shared links (no screenshot) and readability extraction."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from magpie.analyzer import ScreenshotAnalyzer
from magpie.analyzers import AnalyzerRouter
from magpie.config import Settings
from magpie.fetch import BlockedURL, check_url
from magpie.links import classify, normalize_url
from magpie.main import create_app
from magpie.readability import extract

ARTICLE = """<html><head><title>Why uv is fast | Astral</title>
<meta property="og:site_name" content="Astral"><meta name="author" content="Charlie Marsh">
<meta property="og:type" content="article"><meta property="og:image" content="/img/lead.png">
<meta property="article:published_time" content="2026-03-01"></head>
<body><nav>Home · Blog · Docs · Pricing</nav><article><h1>Why uv is fast</h1>""" + "".join(
    f"<p>Paragraph {i}: uv resolves dependencies with a PubGrub solver, keeps a global cache and hard-links "
    f"packages into environments, which is why installs finish in milliseconds rather than minutes.</p>"
    for i in range(14)) + """</article><footer>© 2026 Astral · Privacy · Terms</footer></body></html>"""

LANDING = "<html><head><title>Acme Rockets</title><meta name='description' content='Rockets for everyone.'></head><body><p>Buy now</p></body></html>"

IMDB_TV = """<html><head><title>The Bear (TV Series 2022– ) - IMDb</title><meta property="og:type" content="video.tv_show">
<script type="application/ld+json">{"@type": "TVSeries", "name": "The Bear", "datePublished": "2022-06-23",
 "description": "A young chef returns to Chicago.", "image": "https://m.media-amazon.com/bear.jpg"}</script></head><body></body></html>"""

GITHUB_REPO = {"full_name": "astral-sh/uv", "html_url": "https://github.com/astral-sh/uv", "description": "An extremely fast Python package manager.",
               "stargazers_count": 70000, "forks_count": 2000, "language": "Rust", "topics": ["python", "packaging"],
               "license": {"spdx_id": "Apache-2.0"}, "homepage": "https://docs.astral.sh/uv"}


def html(body, status=200, headers=None):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **(headers or {})})


class Web:
    """MockTransport that records every requested URL."""

    def __init__(self, routes):
        self.routes, self.requested = routes, []

    def client(self):
        def handler(request):
            url = str(request.url)
            self.requested.append(url)
            for prefix, response in self.routes.items():
                if url.startswith(prefix):
                    return response(request) if callable(response) else response
            return httpx.Response(404)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def app_for(tmp_path, web, mode="ocr", analyzer=None, **settings_kw):
    settings = Settings(data_dir=tmp_path, analyzer=mode, anthropic_api_key=None, local_llm_url=None,
                        api_token=None, claude_batch=False, **settings_kw)
    return create_app(settings, analyzer=analyzer, http=web.client())


def capture(client, url, **form):
    return client.post("/api/items", data={"url": url, **form})


# ---- URL normalization & recognition --------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("https://www.GitHub.com/astral-sh/uv/?utm_source=x&tab=readme#top", "https://github.com/astral-sh/uv?tab=readme"),
    ("github.com/astral-sh/uv", "https://github.com/astral-sh/uv"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://x.com/simonw/status/1?s=20&t=abc", "https://x.com/simonw/status/1"),
    ("https://example.com/search?s=shakshuka&fbclid=1", "https://example.com/search?s=shakshuka"),
    ("http://m.imdb.com/title/tt14452776/", "http://imdb.com/title/tt14452776"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_classify_patterns_without_a_page():
    repo = classify("https://github.com/astral-sh/uv/tree/main/docs", None)
    assert repo["category"] == "github_repo" and repo["title"] == "astral-sh/uv" and repo["confidence"] == 97
    assert classify("https://github.com/features/actions", None) is None
    film = classify("https://imdb.com/title/tt1375666", None)
    assert film["category"] == "movie" and film["details"]["imdb_id"] == "tt1375666"
    assert classify("https://themoviedb.org/tv/136315-the-bear", None)["_tmdb"] == ["tv", 136315]
    assert classify("https://example.com/some-post", None) is None


def test_readability_extracts_reader_view():
    a = extract(ARTICLE, "https://astral.sh/blog/uv-fast")
    assert a.title == "Why uv is fast" and a.author == "Charlie Marsh" and a.site_name == "Astral"
    assert a.published_date == "2026-03-01" and a.image == "https://astral.sh/img/lead.png"
    assert a.is_article and a.reading_minutes >= 1
    assert "Pricing" not in a.text and "Privacy" not in a.text   # navigation and footer stripped
    assert a.excerpt.startswith("Paragraph 0")


# ---- end to end -----------------------------------------------------------------


def test_github_link_becomes_a_repo_card_without_any_model(tmp_path):
    web = Web({"https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json=GITHUB_REPO)})
    with TestClient(app_for(tmp_path, web)) as client:
        r = capture(client, "https://github.com/astral-sh/uv?utm_source=newsletter", note="from Dana")
        assert r.status_code == 202
        item = client.get(f"/api/items/{r.json()['id']}").json()
    assert item["kind"] == "url" and item["source_url"] == "https://github.com/astral-sh/uv" and item["image_file"] == ""
    assert item["category"] == "github_repo" and item["title"] == "astral-sh/uv" and item["status"] == "ready"
    assert item["metadata"]["stars"] == 70000 and item["metadata"]["programming_language"] == "Rust"
    assert item["metadata"]["topics"] == ["python", "packaging"]
    assert item["confidence"] == 97 and "Recognized from GitHub" in item["confidence_reason"]
    assert item["metadata"]["sources"][:1] == ["link"] and item["usage"]["cost_usd"] == 0


def test_imdb_tv_link_uses_structured_data_and_tmdb(tmp_path):
    web = Web({
        "https://imdb.com/title/tt14452776": html(IMDB_TV),
        "https://api.themoviedb.org/3/find/tt14452776": httpx.Response(200, json={"tv_results": [{"id": 136315}]}),
        "https://api.themoviedb.org/3/tv/136315": httpx.Response(200, json={
            "id": 136315, "name": "The Bear", "vote_average": 8.5, "genres": [{"name": "Drama"}], "number_of_seasons": 3,
            "poster_path": "/bear.jpg", "external_ids": {"imdb_id": "tt14452776"},
            "credits": {"cast": [{"name": "Jeremy Allen White"}]}, "videos": {}, "watch/providers": {}}),
    })
    with TestClient(app_for(tmp_path, web, tmdb_api_key="k")) as client:
        item = client.get(f"/api/items/{capture(client, 'https://www.imdb.com/title/tt14452776/').json()['id']}").json()
    assert item["category"] == "tv_show" and item["title"] == "The Bear" and item["metadata"]["year"] == 2022
    assert item["image_url"] == "https://image.tmdb.org/t/p/w500/bear.jpg"
    assert item["metadata"]["cast"] == ["Jeremy Allen White"]
    assert item["metadata"]["imdb_id"] == "tt14452776" and item["metadata"]["tmdb_id"] == 136315
    assert item["canonical_url"] == "https://www.imdb.com/title/tt14452776/"


def test_unrecognized_article_gets_a_readability_card_without_a_model(tmp_path):
    web = Web({"https://astral.sh/blog/uv-fast": html(ARTICLE)})
    with TestClient(app_for(tmp_path, web)) as client:
        item_id = capture(client, "https://astral.sh/blog/uv-fast").json()["id"]
        item = client.get(f"/api/items/{item_id}").json()
        found = client.get("/api/items", params={"q": "pubgrub hard-links"}).json()
    assert item["category"] == "article" and item["title"] == "Why uv is fast"
    m = item["metadata"]
    assert m["author"] == "Charlie Marsh" and m["site_name"] == "Astral" and m["reading_time"].endswith("min read")
    assert item["summary"].startswith("Paragraph 0") and item["image_url"] == "https://astral.sh/img/lead.png"
    assert 60 <= item["confidence"] < 90
    assert [i["id"] for i in found] == [item_id]  # the article body is searchable


def test_page_that_cannot_load_still_gets_a_card(tmp_path):
    with TestClient(app_for(tmp_path, Web({}))) as client:
        item = client.get(f"/api/items/{capture(client, 'https://instagram.com/p/abc123').json()['id']}").json()
    assert item["status"] == "ready" and item["source_platform"] == "instagram"
    assert item["needs_review"] is True and "Couldn't load" in item["confidence_reason"]


def test_duplicate_links_are_not_processed_twice(tmp_path):
    web = Web({"https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json=GITHUB_REPO)})
    with TestClient(app_for(tmp_path, web)) as client:
        first = capture(client, "https://github.com/astral-sh/uv").json()
        calls = len(web.requested)
        again = capture(client, "https://www.github.com/astral-sh/uv/?utm_campaign=x", tags="later").json()
    assert again["id"] == first["id"] and again["duplicate"] is True
    assert len(web.requested) == calls
    assert "later" in again["tags"]


def test_offline_link_capture_is_idempotent(tmp_path):
    with TestClient(app_for(tmp_path, Web({}))) as client:
        a = capture(client, "https://example.com/a", id="web-0123456789").json()
        b = capture(client, "https://example.com/a", id="web-0123456789").json()
    assert a["id"] == b["id"] == "web-0123456789"


def test_capture_validation(tmp_path):
    with TestClient(app_for(tmp_path, Web({}))) as client:
        assert client.post("/api/items", data={}).status_code == 422
        assert capture(client, "notaurl").status_code == 422
        assert capture(client, "ftp://example.com/file").status_code == 422
        r = client.post("/api/items", data={"url": "https://example.com"}, files={"file": ("s.png", b"x", "image/png")})
        assert r.status_code == 422


# ---- unrecognized links with a model ----------------------------------------------


class FakeClaude:
    def __init__(self):
        self.params = []

    async def analyze(self, image, media_type, note=None, correction=None, hints="", link_url=None, web=True):
        self.params.append(ScreenshotAnalyzer(client=object()).build_params(image, media_type, note, correction, hints, link_url, web))
        return {"category": "article", "title": "Why uv is fast", "summary": "How uv gets its speed.", "tags": ["python"],
                "confidence": 90, "confidence_reason": "Clear article.", "alternatives": [], "details": {}, "_runs": []}


def router_app(tmp_path, web):
    settings = Settings(data_dir=tmp_path, analyzer="claude", anthropic_api_key="k", api_token=None, claude_batch=False)
    claude = FakeClaude()
    router = AnalyzerRouter(settings, httpx.AsyncClient(), ocr=SimpleNamespace(read=None), claude=claude)
    return create_app(settings, analyzer=router, http=web.client()), claude


def test_unrecognized_link_goes_to_the_model_as_text_without_web_tools(tmp_path):
    web = Web({"https://astral.sh/blog/uv-fast": html(ARTICLE)})
    app, claude = router_app(tmp_path, web)
    with TestClient(app) as client:
        item = client.get(f"/api/items/{capture(client, 'https://astral.sh/blog/uv-fast').json()['id']}").json()
    [params] = claude.params
    content = params["messages"][0]["content"]
    assert [c["type"] for c in content] == ["text"]                       # no image
    assert "https://astral.sh/blog/uv-fast" in content[0]["text"] and "PubGrub" in content[0]["text"]
    assert [t["name"] for t in params["tools"]] == ["save_analysis"]       # readable page: no web search
    assert item["title"] == "Why uv is fast" and item["confidence"] == 90
    assert item["metadata"]["sources"][:3] == ["link", "readability", "claude"]
    assert item["canonical_url"] == "https://astral.sh/blog/uv-fast"


def test_unreadable_link_lets_the_model_search(tmp_path):
    app, claude = router_app(tmp_path, Web({"https://acme.example/": html(LANDING)}))
    with TestClient(app) as client:
        capture(client, "https://acme.example/")
    assert "web_search" in [t["name"] for t in claude.params[0]["tools"]]


# ---- network safety -------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8000/api/items",
                                 "http://intranet.internal/admin", "file:///etc/passwd"])
async def test_private_addresses_are_blocked(url):
    with pytest.raises(BlockedURL):
        await check_url(url)


def test_links_to_private_addresses_are_never_fetched(tmp_path):
    web = Web({"http://intranet.internal/": html(ARTICLE)})
    with TestClient(app_for(tmp_path, web)) as client:
        item = client.get(f"/api/items/{capture(client, 'http://intranet.internal/admin').json()['id']}").json()
    assert web.requested == []
    assert "Couldn't load" in item["confidence_reason"]


def test_redirects_to_private_addresses_are_blocked(tmp_path):
    web = Web({"https://short.example/x": httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})})
    with TestClient(app_for(tmp_path, web)) as client:
        capture(client, "https://short.example/x")
    assert web.requested == ["https://short.example/x"]


def test_private_addresses_can_be_allowed(monkeypatch):
    monkeypatch.setenv("MAGPIE_ALLOW_PRIVATE_URLS", "true")
    import asyncio
    asyncio.run(check_url("http://192.168.1.10/recipes/1"))
