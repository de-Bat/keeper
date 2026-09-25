import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the app runs without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KEEPER_DATA_DIR", "data")).resolve())
    model: str = field(default_factory=lambda: os.environ.get("KEEPER_MODEL", "claude-opus-5"))
    tmdb_api_key: str | None = field(default_factory=lambda: os.environ.get("TMDB_API_KEY") or None)
    omdb_api_key: str | None = field(default_factory=lambda: os.environ.get("OMDB_API_KEY") or None)
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None)
    # When set, every API/media request must present this token (clients: Bearer header).
    api_token: str | None = field(default_factory=lambda: os.environ.get("KEEPER_API_TOKEN") or None)

    # Which analyzer identifies screenshots: auto | claude | local | hybrid | ocr
    analyzer: str = field(default_factory=lambda: _env("KEEPER_ANALYZER", "auto").lower())
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY") or None)
    # On-prem LLM: any OpenAI-compatible server (Ollama, vLLM, LM Studio, llama.cpp server)
    local_llm_url: str | None = field(default_factory=lambda: os.environ.get("LOCAL_LLM_URL") or None)
    local_llm_model: str = field(default_factory=lambda: _env("LOCAL_LLM_MODEL", "qwen3-vl:8b"))
    local_llm_api_key: str | None = field(default_factory=lambda: os.environ.get("LOCAL_LLM_API_KEY") or None)
    # Set to false for text-only models: they then get the OCR text instead of the image.
    local_llm_vision: bool = field(default_factory=lambda: _env("LOCAL_LLM_VISION", "true").lower() not in ("0", "false", "no"))
    local_llm_timeout: float = field(default_factory=lambda: float(_env("LOCAL_LLM_TIMEOUT", "300")))
    # hybrid mode: ask Claude when the local model's confidence is below this
    escalate_below: int = field(default_factory=lambda: int(_env("KEEPER_ESCALATE_BELOW", "70")))
    # OCR pre-pass: rapidocr (bundled, CPU) | tesseract (needs the binary; better for Hebrew/Arabic/...) | off
    ocr_engine: str = field(default_factory=lambda: _env("KEEPER_OCR", "rapidocr").lower())
    ocr_langs: str = field(default_factory=lambda: _env("KEEPER_OCR_LANGS", "eng"))  # tesseract only, e.g. eng+heb
    # Cost controls for Claude (see docs/COSTS.md)
    effort: str = field(default_factory=lambda: _env("KEEPER_EFFORT", "medium").lower())         # low|medium|high|xhigh|max
    fetch_max_tokens: int = field(default_factory=lambda: int(_env("KEEPER_FETCH_MAX_TOKENS", "8000")))  # 0 = no cap
    # Send new screenshots to Claude through the Message Batches API (50% cheaper; results in minutes, max 24 h)
    claude_batch: bool = field(default_factory=lambda: _env("KEEPER_CLAUDE_BATCH", "true").lower() not in ("0", "false", "no", "off"))
    batch_poll_seconds: int = field(default_factory=lambda: int(_env("KEEPER_BATCH_POLL_SECONDS", "60")))
    # Running cost of your on-prem inference box, for the usage report (e.g. 350 W at $0.20/kWh = 0.07)
    local_cost_per_hour: float = field(default_factory=lambda: float(_env("KEEPER_LOCAL_COST_PER_HOUR", "0")))

    # Online metadata lookups (TMDB, GitHub, recipe pages...). Turn off for air-gapped installs.
    enrich: bool = field(default_factory=lambda: _env("KEEPER_ENRICH", "on").lower() not in ("0", "off", "false", "no"))

    def resolved_analyzer(self) -> str:
        """`auto` picks the best configured option: Claude, else the local LLM, else OCR rules."""
        if self.analyzer != "auto":
            return self.analyzer
        if self.anthropic_api_key and self.local_llm_url:
            return "hybrid"
        if self.anthropic_api_key:
            return "claude"
        if self.local_llm_url:
            return "local"
        return "ocr"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "keeper.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"
