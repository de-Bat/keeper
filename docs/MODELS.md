# Choosing models and OCR

Keeper uses three kinds of model, and each one is swappable:

1. **A cloud LLM (Claude)**: the most accurate, because it searches the web to confirm what it sees.
2. **A local or alternative LLM**: anything that speaks the OpenAI-compatible `/chat/completions` API.
3. **An OCR engine**: reads the text in every screenshot, for search and for hints to the model.

This page recommends options for each, with the settings to use. The open-model landscape moves fast: check sizes, tags and licenses against the model's own page before you commit hardware to it.

## Recommended setups

| Setup | When | Settings |
|---|---|---|
| **Balanced (default)** | You have a GPU with ≥8 GB, or an Apple Silicon Mac with ≥16 GB | `KEEPER_ANALYZER=hybrid`, `LOCAL_LLM_MODEL=qwen3-vl:8b`, `KEEPER_MODEL=claude-opus-5`, batch on. Around 70–80% of screenshots never leave your network; hard cases still get Claude's web research. ~$0.05 per screenshot. |
| **Best quality** | Cost doesn't matter much | `KEEPER_ANALYZER=claude`. Optionally set `KEEPER_EFFORT=high` and `KEEPER_CLAUDE_BATCH=false` for instant results. |
| **Cheapest cloud** | No local GPU | `KEEPER_ANALYZER=claude`, `KEEPER_MODEL=claude-sonnet-5`, batch on. ~$0.09 per typical screenshot. |
| **Fully private** | Nothing may leave your network | `KEEPER_ANALYZER=local` with a 24 GB+ model (`qwen3-vl:30b` or `:32b`, Gemma 4 26B/31B). Add `KEEPER_ENRICH=off` for air-gapped: no TMDB/GitHub lookups at all. |
| **Minimal hardware** | CPU-only server, Raspberry Pi class | `KEEPER_ANALYZER=local` with a small vision model (`qwen3-vl:2b` or `:4b`), or a text-only model with `LOCAL_LLM_VISION=false`, or `KEEPER_ANALYZER=ocr` (no model; everything is flagged for review) |

## 1. Claude models

| Model | `KEEPER_MODEL` | $/M in / out | Notes |
|---|---|---|---|
| **Claude Opus 5** (default) | `claude-opus-5` | $5 / $25 | The best balance for this job: reliable web research, honest confidence scores |
| Claude Sonnet 5 | `claude-sonnet-5` | $2 / $10 | 60% cheaper. Very capable at reading screenshots; may give up sooner on obscure items. Try it and watch the *Needs review* rate. |
| Claude Haiku 4.5 | `claude-haiku-4-5` | $1 / $5 | Cheapest. Keeper automatically skips `effort` and uses the basic web search/fetch tools for it. Best used when most screenshots are easy (legible titles). |
| Claude Opus 5.5 | `claude-opus-5-5` | $4 / $20 | Newer Opus at a lower price. Its default effort is lower; Keeper sets `KEEPER_EFFORT` explicitly either way. |
| Claude Fable 5.1 | `claude-fable-5-1` | $10 / $50 | Anthropic's most capable model. Overkill for identifying screenshots. |

Batch processing halves all of these token prices. Keeper's cost report knows all these prices; see [COSTS.md](COSTS.md).

## 2. Local and alternative LLMs

Point `LOCAL_LLM_URL` at any OpenAI-compatible server. Keeper asks for schema-constrained JSON (`response_format: json_schema`), falls back to plain JSON mode if the server doesn't support it, and cleans up sloppy output from small models.

### Runtimes

| Runtime | Best for | `LOCAL_LLM_URL` |
|---|---|---|
| **Ollama** | Easiest; one command per model; NVIDIA, AMD, Apple Silicon, CPU. Included in `docker-compose.yml` (`--profile local`) | `http://ollama:11434/v1` (compose) or `http://localhost:11434/v1` |
| **vLLM** | Highest throughput on NVIDIA GPUs; strict JSON-schema decoding | `http://gpu-box:8000/v1` |
| **llama.cpp** (`llama-server`) | Minimal footprint; GGUF models; CPU or GPU | `http://host:8080/v1` |
| **LM Studio** | Desktop GUI, good on Macs (MLX) | `http://localhost:1234/v1` |

### Vision models (read the screenshot itself)

| Model | Sizes | VRAM (4-bit) | Why |
|---|---|---|---|
| **Qwen3-VL** (recommended) | 2B, 4B, **8B**, 30B (MoE), 32B, 235B | 8B ≈ 6–8 GB · 30B/32B ≈ 20–24 GB | Among the strongest open vision models; very good at reading text in images and UI screenshots; 128K context; Apache 2.0. `qwen3-vl:8b` is the default. |
| **Gemma 4** (Google) | E2B, E4B, 12B, 26B (MoE), 31B | E4B ≈ 4 GB · 12B ≈ 8–10 GB · 26B/31B ≈ 18–22 GB | Released April 2026; every size accepts images at variable resolution; the E-sizes are built for small devices. Check the Gemma license terms. |
| Qwen2.5-VL | 3B, 7B, 32B, 72B | 7B ≈ 6 GB | The previous generation; very widely supported; a safe fallback if a runtime lacks Qwen3-VL. |
| GLM-4.5V / GLM-4.1V-9B-Thinking | 9B, larger MoE | 9B ≈ 8 GB | Strong visual reasoning; check that your runtime supports it. |
| MiniCPM-V | ~8B | ~6 GB | Small and quick; good OCR for its size. |
| Llama 3.2 Vision | 11B, 90B | 11B ≈ 8 GB | Older; weaker at small text than the models above. |

**By hardware:**
- **CPU only:** `qwen3-vl:2b` / `:4b` (slow: minutes per screenshot), or a text model with OCR (below)
- **8 GB GPU / 16 GB Mac:** `qwen3-vl:8b` at 4-bit, or Gemma 4 E4B
- **12–16 GB:** `qwen3-vl:8b` at 8-bit, or Gemma 4 12B
- **24 GB:** `qwen3-vl:30b` (MoE: fast) or `:32b`, or Gemma 4 26B/31B at 4-bit
- **48 GB+:** the 32B models at 8-bit, or Qwen3-VL 235B on multi-GPU servers

Exact Ollama tags change; run `ollama search qwen3-vl` or `ollama search gemma4` for the current ones.

### Text-only models (with Keeper's OCR)

Set `LOCAL_LLM_VISION=false`. The model then gets the OCR text and rule-based clues instead of the image. It works well when the screenshot has the name in text (a caption, a tweet, a repo link). It works poorly when the only clue is a poster image. Good choices: Qwen3 8B / 14B / 30B-A3B, Gemma 4 (text), Llama 3.1 8B, Mistral Small.

### Hosted OpenAI-compatible APIs

The same setting works with hosted providers (OpenRouter, Together, Fireworks, Groq, and Google's and OpenAI's OpenAI-compatible endpoints). Set `LOCAL_LLM_URL`, `LOCAL_LLM_API_KEY` and `LOCAL_LLM_MODEL` to a vision model they serve. The screenshot then leaves your network, and these models don't get web search. Keeper's per-item usage report counts their tokens but prices them at $0 unless you add them to `KEEPER_PRICING`.

## 3. OCR engines

In Keeper, OCR is **support, not the main event**: the vision model (or Claude) reads the image anyway. OCR makes every word searchable, gives small models clues, and powers the no-LLM `ocr` mode. So pick for speed, language coverage and small size before raw benchmark accuracy.

| Engine | Status in Keeper | Strengths | Limits |
|---|---|---|---|
| **RapidOCR** (PP-OCRv4 on ONNX Runtime) | **Default** (`KEEPER_OCR=rapidocr`) | Installs with `pip`, models bundled (~16 MB), CPU, ~1 s per screenshot, very accurate on Latin and Chinese text | Drops spaces in very large headline text ("PASTLIVES"); no Hebrew or Arabic |
| **Tesseract 5** | Built in (`KEEPER_OCR=tesseract`, `KEEPER_OCR_LANGS=eng+heb`); the Docker image includes English and Hebrew | 100+ languages including **Hebrew, Arabic, Cyrillic**; mature | Weaker on stylized text, text over images, and low contrast |
| PaddleOCR-VL (0.9B) | Would need an adapter | Tops recent document-OCR benchmarks at ~0.9B parameters; multilingual; handles mixed scripts and layouts | Needs a GPU in practice; built for documents rather than social-media screenshots |
| DeepSeek-OCR / OCR 2, GLM-OCR, dots.ocr, MinerU | Would need an adapter | Strong document and layout extraction (Markdown, tables) | Built for PDFs and pages; heavier than Keeper needs |
| olmOCR (7B) | Would need an adapter | Excellent PDF-to-text in reading order | 7B GPU model; PDF-focused |
| Apple Vision (on-device, iOS/macOS) | Future idea | Free, fast, private OCR on the phone; the iOS app could send the text along with the screenshot (check Apple's supported-language list for your scripts) | iOS/macOS only |

**Recommendation:** keep **RapidOCR** unless your screenshots are often in Hebrew or another non-Latin script, in which case use **Tesseract** with `KEEPER_OCR_LANGS=eng+heb`. A strong vision LLM (Qwen3-VL, Gemma 4, Claude) reads Hebrew in the image itself either way. The OCR engine interface (`keeper/ocr.py`) is ~30 lines per engine, so adding PaddleOCR-VL or another engine is straightforward if you need it.

## How to tell whether a change helped

Change one thing at a time and use the app for a week, then compare:
- **Usage & cost panel:** per-screenshot cost, and in hybrid mode the *sent to Claude* share
- **The *Needs review* count and how often you use *Fix it*:** these measure accuracy
- **Average time per analyzer** (in the usage table): whether a bigger local model is worth the wait

## Sources

- Open vision-language models, 2026: [BentoML guide](https://www.bentoml.com/blog/multimodal-ai-a-guide-to-open-source-vision-language-models), [Labellerr](https://www.labellerr.com/blog/top-open-source-vision-language-models/), [SiliconFlow](https://www.siliconflow.com/articles/best-open-source-multimodal-models-2025), [Overshoot VLM survey](https://www.overshoot.ai/blogs/vlm-survey-2026)
- Qwen3-VL sizes and hardware: [local-llm.net](https://www.local-llm.net/models/qwen3-vl/), [InsiderLLM Qwen guide](https://insiderllm.com/guides/qwen-models-guide/), [LocalLLM.in Ollama VRAM guide](https://localllm.in/blog/ollama-vram-requirements-for-local-llms)
- Gemma 4: [Google AI for Developers](https://ai.google.dev/gemma/docs/core), [Hugging Face docs](https://huggingface.co/docs/transformers/model_doc/gemma4), [Edge AI and Vision Alliance](https://www.edge-ai-vision.com/2026/04/google-pushes-multimodal-ai-further-onto-edge-devices-with-gemma-4/)
- OCR, 2026: [Roboflow OCR ranking](https://blog.roboflow.com/best-open-source-ocr-models/), [Spheron comparison](https://www.spheron.network/blog/best-open-source-ocr-vlm-self-host-gpu-cloud-2026/), [Modal comparison](https://modal.com/blog/8-top-open-source-ocr-models-compared), [PaddleOCR-VL-1.5 paper](https://arxiv.org/pdf/2601.21957), [Koncile on Tesseract in 2026](https://www.koncile.ai/en/ressources/is-tesseract-still-the-best-open-source-ocr), [CodeSOTA PaddleOCR vs Tesseract](https://www.codesota.com/ocr/paddleocr-vs-tesseract)
