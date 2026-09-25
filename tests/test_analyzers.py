import glob
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from magpie.analyzer import AnalysisError
from magpie.analyzers import AnalyzerRouter, LocalLLMAnalyzer, normalize, parse_json, rules_analysis
from magpie.config import Settings
from magpie.main import create_app
from magpie.ocr import Ocr, OcrLine, OcrResult, TesseractEngine, extract_signals


def png(size=(40, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


def ocr_result(*lines):
    """lines: (text, height)"""
    return OcrResult([OcrLine(text=t, height=h, top=i * 50) for i, (t, h) in enumerate(lines)], "fake")


class FakeOcr:
    def __init__(self, result):
        self.result = result

    async def read(self, image):
        return self.result


class FakeBackend:
    def __init__(self, result=None, error=None, label="local:fake"):
        self.result, self.error, self.label = result, error, label
        self.calls = []

    async def analyze(self, image, media_type, note=None, correction=None, hints=""):
        self.calls.append(hints)
        if self.error:
            raise self.error
        return dict(self.result)


INSTAGRAM_POST = ocr_result(
    ("Instagram", 55), ("filmclub.tlv", 39), ("Sponsored", 30), ("PAST LIVES", 98), ("A24 · 2023", 46),
    ("1,204 likes", 35), ("filmclub.tlv Best film of the year. Directed by Celine Song", 36),
    ("View all 87 comments", 33),
)


# ---- settings -------------------------------------------------------------------


@pytest.mark.parametrize("claude,local,expected", [
    ("k", None, "claude"), (None, "http://ollama:11434/v1", "local"), ("k", "http://x/v1", "hybrid"), (None, None, "ocr"),
])
def test_auto_mode_picks_what_is_configured(claude, local, expected, tmp_path):
    s = Settings(data_dir=tmp_path, analyzer="auto", anthropic_api_key=claude, local_llm_url=local)
    assert s.resolved_analyzer() == expected


# ---- OCR signals ----------------------------------------------------------------


def test_signals_from_a_social_post():
    s = extract_signals(INSTAGRAM_POST)
    assert s.platform == "instagram"
    assert s.category == "movie"
    assert s.title_candidates[0] == "PAST LIVES"
    assert "Instagram" not in s.title_candidates and "Sponsored" not in s.title_candidates


def test_signals_find_links_and_ids():
    s = extract_signals(ocr_result(
        ("Check this out → github.com/astral-sh/uv.", 20), ("also imdb.com/title/tt1375666 and @dana_k", 20)))
    assert s.github_repos == ["astral-sh/uv"]
    assert s.imdb_ids == ["tt1375666"]
    assert s.handles == ["dana_k"]
    assert s.category == "github_repo"


def test_recipe_vocabulary():
    s = extract_signals(ocr_result(("Shakshuka", 80), ("Ingredients", 30), ("2 tbsp olive oil", 20),
                                   ("1 cup tomatoes", 20), ("Preheat the oven", 20)))
    assert s.category == "recipe" and s.title_candidates[0] == "Shakshuka"


def test_tesseract_tsv_is_grouped_into_lines(monkeypatch):
    tsv = "\n".join([
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "5\t1\t1\t1\t1\t1\t10\t10\t50\t40\t96\tPast",
        "5\t1\t1\t1\t1\t2\t70\t10\t50\t42\t94\tLives",
        "5\t1\t1\t1\t2\t1\t10\t80\t50\t12\t90\tשלום",
        "5\t1\t1\t1\t2\t2\t70\t80\t50\t12\t-1\t ",
    ])
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: type("R", (), {"stdout": tsv})())
    result = TesseractEngine("eng+heb").read(png())
    assert [l.text for l in result.lines] == ["Past Lives", "שלום"]
    assert result.lines[0].height == 42


def _font(size):
    fonts = glob.glob("/usr/share/fonts/**/DejaVuSans*.ttf", recursive=True)
    if not fonts:
        pytest.skip("no TTF font available to render test text")
    return ImageFont.truetype(sorted(fonts)[0], size)


async def test_real_ocr_engine_reads_a_screenshot():
    pytest.importorskip("rapidocr_onnxruntime")
    img = Image.new("RGB", (900, 500), "white")
    d = ImageDraw.Draw(img)
    d.text((30, 30), "Instagram", font=_font(44), fill="black")
    d.text((30, 150), "SHAKSHUKA", font=_font(90), fill="black")
    d.text((30, 320), "Recipe: github.com/astral-sh/uv", font=_font(30), fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    result = await Ocr("rapidocr").read(buf.getvalue())
    assert result and "github.com/astral-sh/uv" in result.text.replace(" ", "")
    assert extract_signals(result).platform == "instagram"


async def test_missing_ocr_engine_degrades_gracefully():
    assert await Ocr("tesseract", "eng").read(png()) is None or True  # never raises
    assert await Ocr("off").read(png()) is None


# ---- local LLM (OpenAI-compatible) ----------------------------------------------


GOOD = {
    "category": "Film", "source_platform": "Instagram", "title": "Past Lives", "year": "2023",
    "summary": "Two childhood friends reunite.", "confidence": 0.82, "tags": ["drama"],
    "details": {"directors": "Celine Song"},
}


def llm_server(replies):
    """Mock /chat/completions. replies: list of (status, content-or-body)."""
    requests = []

    def handler(request: httpx.Request):
        requests.append(json.loads(request.content))
        status, payload = replies.pop(0)
        if status != 200:
            return httpx.Response(status, text=payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": payload}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


def local_settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path, local_llm_url="http://ollama:11434/v1", local_llm_model="qwen2.5vl:7b", **kw)


async def test_local_llm_request_and_normalized_result(tmp_path):
    http, requests = llm_server([(200, json.dumps(GOOD))])
    out = await LocalLLMAnalyzer(local_settings(tmp_path), http).analyze(png(), "image/png", hints="\n<ocr>PAST LIVES</ocr>")
    req = requests[0]
    assert req["model"] == "qwen2.5vl:7b"
    assert req["response_format"]["type"] == "json_schema"
    user = req["messages"][1]["content"]
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "<ocr>PAST LIVES</ocr>" in user[0]["text"]
    assert "web access" in req["messages"][0]["content"]
    # sloppy local output is coerced into the standard shape
    assert out["category"] == "movie" and out["source_platform"] == "instagram"
    assert out["year"] == 2023 and out["confidence"] == 82
    assert out["details"]["directors"] == ["Celine Song"]
    assert out["details"]["imdb_id"] is None and out["alternatives"] == []


async def test_local_llm_falls_back_when_json_schema_unsupported(tmp_path):
    http, requests = llm_server([
        (400, '{"error": "response_format json_schema not supported"}'),
        (200, "Sure! ```json\n" + json.dumps(GOOD) + "\n```"),
        (200, json.dumps(GOOD)),
    ])
    llm = LocalLLMAnalyzer(local_settings(tmp_path), http)
    assert (await llm.analyze(png(), "image/png"))["title"] == "Past Lives"
    assert [r["response_format"]["type"] for r in requests] == ["json_schema", "json_object"]
    await llm.analyze(png(), "image/png")
    assert requests[2]["response_format"]["type"] == "json_object"  # remembered


async def test_text_only_local_model_gets_no_image(tmp_path):
    http, requests = llm_server([(200, json.dumps(GOOD))])
    await LocalLLMAnalyzer(local_settings(tmp_path, local_llm_vision=False), http).analyze(png(), "image/png", hints="x")
    assert [c["type"] for c in requests[0]["messages"][1]["content"]] == ["text"]


async def test_local_llm_errors_are_reported(tmp_path):
    def down(request):
        raise httpx.ConnectError("refused")
    llm = LocalLLMAnalyzer(local_settings(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(AnalysisError, match="Can't reach"):
        await llm.analyze(png(), "image/png")
    http, _ = llm_server([(404, "model 'qwen2.5vl:7b' not found")])
    with pytest.raises(AnalysisError, match="pulled"):
        await LocalLLMAnalyzer(local_settings(tmp_path), http).analyze(png(), "image/png")
    http, _ = llm_server([(200, "I think it's a movie")])
    with pytest.raises(AnalysisError, match="JSON"):
        await LocalLLMAnalyzer(local_settings(tmp_path), http).analyze(png(), "image/png")


def test_normalize_and_parse_helpers():
    assert parse_json('blah {"a": 1} blah') == {"a": 1}
    n = normalize({"category": "series", "confidence": "high", "alternatives": [{"title": "X", "category": "nope"}, "junk"]})
    assert n["category"] == "tv_show" and n["confidence"] == 90
    assert n["alternatives"] == [{"title": "X", "category": "other", "year": None, "canonical_url": None, "why": ""}]


# ---- router ---------------------------------------------------------------------


def router(tmp_path, mode, local=None, claude=None, ocr=INSTAGRAM_POST, batch=False):
    s = Settings(data_dir=tmp_path, analyzer=mode, escalate_below=70, claude_batch=batch)
    return AnalyzerRouter(s, httpx.AsyncClient(), ocr=FakeOcr(ocr), claude=claude, local=local)


async def test_hybrid_keeps_confident_local_answers(tmp_path):
    local, claude = FakeBackend({**GOOD, "confidence": 88}), FakeBackend({"title": "C"}, label="claude")
    out = await router(tmp_path, "hybrid", local, claude).analyze(png(), "image/png")
    assert out["_analyzer"] == ["ocr", "local:fake"] and not claude.calls
    assert "PAST LIVES" in local.calls[0] and "instagram" in local.calls[0]   # OCR hints were passed
    assert out["_ocr_text"].startswith("Instagram")


async def test_hybrid_escalates_unsure_or_failed_local_answers(tmp_path):
    claude = FakeBackend({"title": "Past Lives", "confidence": 95}, label="claude")
    out = await router(tmp_path, "hybrid", FakeBackend({**GOOD, "confidence": 40}), claude).analyze(png(), "image/png")
    assert out["_analyzer"] == ["ocr", "local:fake", "claude"] and out["confidence"] == 95

    claude.calls.clear()
    out = await router(tmp_path, "hybrid", FakeBackend(error=AnalysisError("down")), claude).analyze(png(), "image/png")
    assert out["_analyzer"] == ["ocr", "claude"] and claude.calls


def test_rules_only_mode_is_always_marked_uncertain():
    out = rules_analysis(INSTAGRAM_POST, extract_signals(INSTAGRAM_POST))
    assert out["title"] == "PAST LIVES" and out["category"] == "movie" and out["source_platform"] == "instagram"
    assert out["confidence"] < 60 and "no AI model" in out["confidence_reason"]
    assert rules_analysis(None, None)["confidence"] == 0


def test_ocr_mode_end_to_end_with_source_confirmation(tmp_path):
    """No LLM at all: OCR finds a GitHub link, the GitHub API confirms it, confidence goes up."""
    repo = {"full_name": "astral-sh/uv", "html_url": "https://github.com/astral-sh/uv", "description": "fast",
            "stargazers_count": 1, "topics": [], "language": "Rust"}
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=repo) if "api.github.com" in str(r.url) else httpx.Response(404)))
    settings = Settings(data_dir=tmp_path, analyzer="ocr", anthropic_api_key=None, api_token=None)
    app = create_app(settings, http=http)
    with TestClient(app) as client:
        app.state.pipeline.analyzer.ocr = FakeOcr(ocr_result(("Tool of the week", 60), ("github.com/astral-sh/uv", 20)))
        item_id = client.post("/api/items", files={"file": ("s.png", png(), "image/png")}).json()["id"]
        item = client.get(f"/api/items/{item_id}").json()
        assert client.get("/api/health").json()["analyzer"] == "ocr"
        found = client.get("/api/items", params={"q": "tool week"}).json()
    assert item["category"] == "github_repo" and item["title"] == "astral-sh/uv"
    assert item["confidence"] == 85 and "Confirmed by github" in item["confidence_reason"]
    assert item["metadata"]["sources"] == ["ocr", "rules", "github"]
    assert [i["id"] for i in found] == [item_id]   # OCR text is searchable


def test_misconfigured_analyzer_reports_on_items(tmp_path):
    settings = Settings(data_dir=tmp_path, analyzer="claude", anthropic_api_key=None, api_token=None)
    with TestClient(create_app(settings, http=httpx.AsyncClient())) as client:
        item_id = client.post("/api/items", files={"file": ("s.png", png(), "image/png")}).json()["id"]
        item = client.get(f"/api/items/{item_id}").json()
    assert item["status"] == "error" and "ANTHROPIC_API_KEY" in item["error"]
