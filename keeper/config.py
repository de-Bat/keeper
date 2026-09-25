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


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KEEPER_DATA_DIR", "data")).resolve())
    model: str = field(default_factory=lambda: os.environ.get("KEEPER_MODEL", "claude-opus-5"))
    tmdb_api_key: str | None = field(default_factory=lambda: os.environ.get("TMDB_API_KEY") or None)
    omdb_api_key: str | None = field(default_factory=lambda: os.environ.get("OMDB_API_KEY") or None)
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "keeper.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"
