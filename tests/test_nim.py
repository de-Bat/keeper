"""NVIDIA NIM support (self-hosted containers and the hosted build.nvidia.com API)."""

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from keeper.analyzer import SAVE_TOOL, AnalysisError
from keeper.analyzers import LocalLLMAnalyzer
from keeper.config import Settings

HOSTED = "https://integrate.api.nvidia.com/v1"
ANSWER = {"category": "movie", "source_platform": "instagram", "title": "Past Lives", "year": 2023,
          "summary": "Two friends.", "confidence": 88, "tags": ["drama"], "details": {}}


def image(fmt="WEBP"):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "teal").save(buf, format=fmt)
    return buf.getvalue()


def server(replies):
    """replies: list of (status, body-or-content, headers)"""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        status, payload, headers = replies.pop(0)
        if status == 200:
            return httpx.Response(200, json={"choices": [{"message": {"content": payload}}],
                                             "usage": {"prompt_tokens": 1500, "completion_tokens": 300}})
        return httpx.Response(status, text=payload, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def nim(tmp_path, http, **kw):
    settings = Settings(data_dir=tmp_path, local_llm_url=kw.pop("url", HOSTED),
                        local_llm_model="meta/llama-3.2-90b-vision-instruct", local_llm_api_key="nvapi-test", **kw)
    llm = LocalLLMAnalyzer(settings, http)
    llm.slept = []

    async def no_wait(seconds):
        llm.slept.append(seconds)
    llm._sleep = no_wait
    return llm


@pytest.mark.parametrize("url,key,provider,expected", [
    (HOSTED, None, "auto", "nim"),
    ("http://nim:8000/v1", "nvapi-abc", "auto", "nim"),
    ("http://nim:8000/v1", None, "nim", "nim"),
    ("http://ollama:11434/v1", None, "auto", "openai"),
])
def test_provider_detection(tmp_path, url, key, provider, expected):
    s = Settings(data_dir=tmp_path, local_llm_url=url, local_llm_api_key=key, local_llm_provider=provider)
    assert s.resolved_llm_provider() == expected


def test_nvidia_api_key_env_alias(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-xyz")
    assert Settings(data_dir=tmp_path).local_llm_api_key == "nvapi-xyz"


async def test_nim_request_shape_and_label(tmp_path):
    http, seen = server([(200, json.dumps(ANSWER), {})])
    llm = nim(tmp_path, http)
    out = await llm.analyze(image("WEBP"), "image/webp")
    req = seen[0]
    assert req["model"] == "meta/llama-3.2-90b-vision-instruct"
    url = req["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")                       # WebP converted: NIM takes JPEG/PNG
    Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).verify()
    assert llm.label == "nim:meta/llama-3.2-90b-vision-instruct"
    [run] = out["_runs"]
    assert run["analyzer"] == "nim" and run["input_tokens"] == 1500 and out["title"] == "Past Lives"


async def test_nim_falls_back_to_guided_json(tmp_path):
    http, seen = server([
        (400, '{"detail": "response_format json_schema is not supported"}', {}),
        (200, json.dumps(ANSWER), {}),
        (200, json.dumps(ANSWER), {}),
    ])
    llm = nim(tmp_path, http)
    await llm.analyze(image("PNG"), "image/png")
    assert "response_format" in seen[0] and "nvext" not in seen[0]
    assert seen[1]["nvext"] == {"guided_json": SAVE_TOOL["input_schema"]} and "response_format" not in seen[1]
    await llm.analyze(image("PNG"), "image/png")
    assert "nvext" in seen[2]                                               # remembered


async def test_non_nim_servers_never_get_nvext(tmp_path):
    http, seen = server([(400, "no", {}), (200, json.dumps(ANSWER), {})])
    s = Settings(data_dir=tmp_path, local_llm_url="http://ollama:11434/v1", local_llm_api_key=None, local_llm_provider="auto")
    await LocalLLMAnalyzer(s, http).analyze(image("PNG"), "image/png")
    assert all("nvext" not in r for r in seen) and seen[1]["response_format"]["type"] == "json_object"


async def test_rate_limits_are_retried(tmp_path):
    http, seen = server([(429, "slow down", {"retry-after": "7"}), (503, "loading", {}), (200, json.dumps(ANSWER), {})])
    llm = nim(tmp_path, http)
    out = await llm.analyze(image("PNG"), "image/png")
    assert out["title"] == "Past Lives" and len(seen) == 3
    assert llm.slept == [7.0, 4.0]


async def test_rate_limit_that_never_clears_is_an_error(tmp_path):
    http, _ = server([(429, "slow down", {})] * 4)
    with pytest.raises(AnalysisError, match="429"):
        await nim(tmp_path, http).analyze(image("PNG"), "image/png")


async def test_bad_key_has_a_clear_error(tmp_path):
    http, _ = server([(401, "Unauthorized", {})])
    with pytest.raises(AnalysisError, match="NVIDIA_API_KEY"):
        await nim(tmp_path, http).analyze(image("PNG"), "image/png")


async def test_image_size_limit_is_configurable(tmp_path):
    http, seen = server([(200, json.dumps(ANSWER), {})])
    big = io.BytesIO()
    Image.new("RGB", (1200, 2600), "white").save(big, format="PNG")
    await nim(tmp_path, http, local_llm_max_image_edge=1024).analyze(big.getvalue(), "image/png")
    url = seen[0]["messages"][1]["content"][1]["image_url"]["url"]
    assert max(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size) == 1024
