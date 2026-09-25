"""Usage accounting, Message Batches, and hybrid-mode behaviour."""

import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from keeper.analyzer import SAVE_TOOL, AnalysisError, ScreenshotAnalyzer
from keeper.analyzers import AnalyzerRouter, LocalLLMAnalyzer
from keeper.batch import BatchWorker
from keeper.config import Settings
from keeper.main import create_app
from keeper.ocr import OcrLine, OcrResult
from keeper.usage import Run, claude_cost, price_for


def png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(buf, format="PNG")
    return buf.getvalue()


def analysis(**kw):
    details = {k: ([] if v.get("type") == "array" else None)
               for k, v in SAVE_TOOL["input_schema"]["properties"]["details"]["properties"].items()}
    base = {"category": "movie", "source_platform": "instagram", "title": "Past Lives", "subtitle": None, "year": 2023,
            "summary": "Two friends.", "canonical_url": None, "image_url": None, "links": [], "tags": ["drama"],
            "screenshot_text": "", "confidence": 93, "confidence_reason": "Title is legible.", "alternatives": [],
            "details": details}
    base.update(kw)
    return base


def usage(inp, out, searches=0, fetches=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                           server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=fetches))


def saved_message(result, inp=20_000, out=1_000, searches=2):
    return SimpleNamespace(stop_reason="tool_use", model="claude-opus-5", usage=usage(inp, out, searches),
                           content=[SimpleNamespace(type="tool_use", id="t1", name="save_analysis", input=result)])


class FakeAnthropic:
    """Just enough of AsyncAnthropic: messages.create + messages.batches.*"""

    def __init__(self, realtime=None, batch_results=None):
        self.realtime = list(realtime or [])
        self.batch_results = batch_results or {}
        self.created, self.batches_created = [], []
        outer = self

        class Batches:
            async def create(self, requests):
                outer.batches_created.append(requests)
                return SimpleNamespace(id=f"msgbatch_{len(outer.batches_created)}")

            async def retrieve(self, batch_id):
                return SimpleNamespace(id=batch_id, processing_status="ended")

            async def results(self, batch_id):
                requests = outer.batches_created[int(batch_id.split("_")[1]) - 1]

                async def gen():
                    for r in requests:
                        yield SimpleNamespace(custom_id=r["custom_id"], result=outer.batch_results[r["custom_id"]])
                return gen()

        class Messages:
            batches = Batches()

            async def create(self, **kwargs):
                outer.created.append(kwargs)
                return outer.realtime.pop(0)

        self.messages = Messages()
        self.beta = SimpleNamespace(messages=self.messages)


class FakeOcr:
    async def read(self, image):
        return OcrResult([OcrLine("PAST LIVES", height=90), OcrLine("Instagram", height=40)], "fake")


class FakeLocal:
    label = "local:qwen"

    def __init__(self, confidence=40, error=None):
        self.confidence, self.error, self.calls = confidence, error, 0

    async def analyze(self, image, media_type, note=None, correction=None, hints=""):
        self.calls += 1
        if self.error:
            raise AnalysisError(self.error, runs=[Run("local", model="qwen", mode="local", requests=1, ok=False).to_dict()])
        out = analysis(confidence=self.confidence)
        out["_runs"] = [Run("local", model="qwen", mode="local", requests=1, input_tokens=3000, output_tokens=400,
                            duration_ms=4000).to_dict()]
        return out


def make(tmp_path, mode="hybrid", batch=True, local=None, anthropic=None):
    settings = Settings(data_dir=tmp_path, analyzer=mode, claude_batch=batch, api_token=None, enrich=False,
                        escalate_below=70, anthropic_api_key="k", local_llm_url="http://x/v1")
    claude = ScreenshotAnalyzer(client=anthropic or FakeAnthropic(), model="claude-opus-5")
    router = AnalyzerRouter(settings, httpx.AsyncClient(), ocr=FakeOcr(), claude=claude, local=local or FakeLocal())
    app = create_app(settings, analyzer=router, http=httpx.AsyncClient(), start_batch_worker=False)
    return app, router


def upload(client):
    return client.post("/api/items", files={"file": ("s.png", png(), "image/png")}).json()["id"]


# ---- pricing --------------------------------------------------------------------


def test_claude_cost_formula():
    run = Run("claude", model="claude-opus-5", input_tokens=65_000, output_tokens=3_000, web_searches=3)
    assert claude_cost(run) == pytest.approx(0.325 + 0.075 + 0.03)
    run.mode = "batch"  # tokens half price, searches unchanged
    assert claude_cost(run) == pytest.approx((0.325 + 0.075) / 2 + 0.03)
    assert price_for("claude-opus-5-20260101") == (5.0, 25.0)
    assert price_for("some-local-model") is None


def test_pricing_override(monkeypatch):
    monkeypatch.setenv("KEEPER_PRICING", '{"claude-opus-5": [4, 20]}')
    assert price_for("claude-opus-5") == (4, 20)


# ---- batch flow -----------------------------------------------------------------


async def test_unsure_local_answer_is_escalated_through_a_batch(tmp_path):
    anthropic = FakeAnthropic(batch_results={"job-1": SimpleNamespace(type="succeeded", message=saved_message(analysis()))})
    app, router = make(tmp_path, anthropic=anthropic)
    with TestClient(app) as client:
        item_id = upload(client)
        queued = client.get(f"/api/items/{item_id}").json()
        assert queued["status"] == "processing" and queued["batch_pending"] is True
        assert anthropic.created == []  # nothing sent in real time

        await BatchWorker(app.state.db, app.state.pipeline, anthropic, 0).tick()

        [requests] = anthropic.batches_created
        params = requests[0]["params"]
        assert "fallbacks" not in params and "betas" not in params      # not allowed in batches
        assert params["output_config"] == {"effort": "medium"}
        assert "PAST LIVES" in params["messages"][0]["content"][1]["text"]  # OCR hints travel with it
        json.dumps(params)                                              # storable

        item = client.get(f"/api/items/{item_id}").json()
        assert item["status"] == "ready" and item["batch_pending"] is False
        assert item["title"] == "Past Lives" and item["confidence"] == 93
        assert item["metadata"]["sources"] == ["ocr", "local:qwen", "claude"]
        # (20k x $5/M + 1k x $25/M) / 2 + 2 searches x $0.01
        assert item["usage"]["cost_usd"] == pytest.approx(0.0825, abs=1e-4)
        assert sorted(item["usage"]["via"]) == ["claude:batch", "local:local", "ocr:local"]

        report = client.get("/api/usage").json()
        assert report["totals"]["screenshots"] == 1 and report["totals"]["web_searches"] == 2
        assert report["per_screenshot_usd"] == pytest.approx(0.0825, abs=1e-4)
        assert report["claude_share"] == 1.0
        assert {r["analyzer"] for r in report["by_analyzer"]} == {"claude", "local", "ocr"}
        assert report["config"]["claude_batch"] is True


async def test_failed_batch_request_falls_back_to_realtime(tmp_path):
    anthropic = FakeAnthropic(realtime=[saved_message(analysis(title="Past Lives"), inp=10_000, out=500, searches=1)],
                              batch_results={"job-1": SimpleNamespace(type="expired")})
    app, _ = make(tmp_path, mode="claude", anthropic=anthropic)
    with TestClient(app) as client:
        item_id = upload(client)
        await BatchWorker(app.state.db, app.state.pipeline, anthropic, 0).tick()
        item = client.get(f"/api/items/{item_id}").json()
    assert item["status"] == "ready" and len(anthropic.created) == 1
    assert item["usage"]["via"] == ["claude:realtime", "ocr:local"]


async def test_item_deleted_while_queued_is_still_accounted(tmp_path):
    anthropic = FakeAnthropic(batch_results={"job-1": SimpleNamespace(type="succeeded", message=saved_message(analysis()))})
    app, _ = make(tmp_path, mode="claude", anthropic=anthropic)
    with TestClient(app) as client:
        item_id = upload(client)
        await BatchWorker(app.state.db, app.state.pipeline, anthropic, 0).submit()
        client.delete(f"/api/items/{item_id}")
        await BatchWorker(app.state.db, app.state.pipeline, anthropic, 0).collect()
        assert client.get("/api/usage").json()["totals"]["web_searches"] == 2
        assert app.state.db.open_batches() == []


def test_confident_local_answers_never_reach_claude(tmp_path):
    anthropic = FakeAnthropic()
    app, _ = make(tmp_path, local=FakeLocal(confidence=88), anthropic=anthropic)
    with TestClient(app) as client:
        item = client.get(f"/api/items/{upload(client)}").json()
    assert item["status"] == "ready" and item["batch_pending"] is False
    assert anthropic.created == [] and anthropic.batches_created == []
    assert item["metadata"]["sources"] == ["ocr", "local:qwen"]


def test_interactive_requests_skip_the_batch(tmp_path):
    anthropic = FakeAnthropic(realtime=[saved_message(analysis(title="Past Lives (2023)"))] * 2)
    app, _ = make(tmp_path, mode="claude", anthropic=anthropic)
    with TestClient(app) as client:
        item_id = upload(client)                                   # queued for batch
        client.post(f"/api/items/{item_id}/reanalyze")             # user is waiting: real time
        assert client.get(f"/api/items/{item_id}").json()["status"] == "ready"
        client.post(f"/api/items/{item_id}/correct", json={"hint": "the 2023 film"})
        assert len(anthropic.created) == 2


def test_unreachable_local_server_is_skipped_for_a_while(tmp_path):
    local = FakeLocal(error="Can't reach the local LLM at http://x/v1: ConnectError")
    anthropic = FakeAnthropic(realtime=[saved_message(analysis())] * 2)
    app, router = make(tmp_path, batch=False, local=local, anthropic=anthropic)
    with TestClient(app) as client:
        upload(client)
        upload(client)
        report = client.get("/api/usage").json()
    assert local.calls == 1 and len(anthropic.created) == 2
    failures = {r["analyzer"]: r["failures"] for r in report["by_analyzer"]}
    assert failures["local"] == 1


# ---- local LLM usage ------------------------------------------------------------


async def test_local_llm_usage_and_energy_cost(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(analysis())}}],
                                         "usage": {"prompt_tokens": 2800, "completion_tokens": 350}})
    settings = Settings(data_dir=tmp_path, local_llm_url="http://x/v1", local_cost_per_hour=3600.0)
    out = await LocalLLMAnalyzer(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler))).analyze(png(), "image/png")
    [run] = out["_runs"]
    assert run["input_tokens"] == 2800 and run["output_tokens"] == 350 and run["mode"] == "local"
    assert run["cost_usd"] == pytest.approx(run["duration_ms"] / 1000, abs=0.01)  # $3600/h == $1/s


def test_tool_versions_match_the_model():
    image = png()
    types = lambda model: [t.get("type") for t in ScreenshotAnalyzer(client=object(), model=model).build_params(image, "image/png")["tools"]][:2]  # noqa: E731
    assert types("claude-opus-5") == ["web_search_20260209", "web_fetch_20260209"]
    assert types("claude-haiku-4-5") == ["web_search_20250305", "web_fetch_20250910"]
    assert "output_config" not in ScreenshotAnalyzer(client=object(), model="claude-haiku-4-5").build_params(image, "image/png")
