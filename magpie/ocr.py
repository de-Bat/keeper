"""OCR pre-pass and rule-based signals.

Every screenshot is OCR'd before any LLM sees it. The text is:
  * searchable, even when identification fails;
  * mined with plain rules for strong clues (GitHub/IMDb links, the social app it came from,
    recipe or film vocabulary, the most prominent line as a title candidate);
  * handed to the LLM as hints, which matters a lot for small on-prem models that read
    small print poorly, and is the whole input for text-only models.

Engines: RapidOCR (PaddleOCR models on ONNX Runtime, bundled with the pip package, CPU)
and Tesseract (system binary; pick it for Hebrew, Arabic, Cyrillic and other scripts).
"""

import asyncio
import csv
import io
import logging
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class OcrLine:
    text: str
    score: float = 1.0
    height: float = 0.0   # text height in pixels: big text is usually the title
    top: float = 0.0


@dataclass
class OcrResult:
    lines: list[OcrLine] = field(default_factory=list)
    engine: str = ""

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)


class RapidOcrEngine:
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR  # imported lazily: loads ONNX models
        self._engine = RapidOCR()

    def read(self, image: bytes) -> OcrResult:
        result, _ = self._engine(image)
        lines = []
        for box, text, score in result or []:
            ys = [p[1] for p in box]
            lines.append(OcrLine(text=text.strip(), score=float(score), height=max(ys) - min(ys), top=min(ys)))
        return OcrResult([l for l in lines if l.text], self.name)


class TesseractEngine:
    name = "tesseract"

    def __init__(self, langs: str = "eng") -> None:
        if not shutil.which("tesseract"):
            raise RuntimeError("tesseract binary not found (apt install tesseract-ocr tesseract-ocr-heb ...)")
        self.langs = langs

    def read(self, image: bytes) -> OcrResult:
        with tempfile.NamedTemporaryFile(suffix=".img") as f:
            f.write(image)
            f.flush()
            out = subprocess.run(
                ["tesseract", f.name, "stdout", "-l", self.langs, "--psm", "3", "tsv"],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout
        # TSV rows are words; group them back into lines.
        grouped: dict[tuple, list[dict]] = {}
        for row in csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE):
            if row.get("level") == "5" and (row.get("text") or "").strip() and float(row.get("conf") or -1) >= 0:
                key = (row["block_num"], row["par_num"], row["line_num"])
                grouped.setdefault(key, []).append(row)
        lines = []
        for words in grouped.values():
            lines.append(OcrLine(
                text=" ".join(w["text"] for w in words),
                score=sum(float(w["conf"]) for w in words) / len(words) / 100,
                height=max(int(w["height"]) for w in words),
                top=min(int(w["top"]) for w in words),
            ))
        lines.sort(key=lambda l: l.top)
        return OcrResult(lines, self.name)


class Ocr:
    """Runs the configured engine off the event loop; failures degrade to 'no OCR'."""

    def __init__(self, engine_name: str = "rapidocr", langs: str = "eng") -> None:
        self.engine_name, self.langs = engine_name, langs
        self._engine = None
        self._failed = False

    def _get(self):
        if self._engine is None and not self._failed and self.engine_name != "off":
            try:
                self._engine = TesseractEngine(self.langs) if self.engine_name == "tesseract" else RapidOcrEngine()
            except Exception as e:  # missing package/binary: keep working without OCR
                log.warning("OCR disabled: %s", e)
                self._failed = True
        return self._engine

    async def read(self, image: bytes) -> OcrResult | None:
        engine = self._get()
        if engine is None:
            return None
        try:
            return await asyncio.to_thread(engine.read, image)
        except Exception:
            log.exception("OCR failed")
            return None


# ---------------------------------------------------------------------------
# Rule-based signals


URL_RE = re.compile(r"\b((?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|co|app|me|tv|ly|gg|ai|il|uk|de|fr)(?:/[^\s)\]}>\"']*)?)", re.I)
GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)", re.I)
IMDB_RE = re.compile(r"\b(tt\d{7,9})\b")
HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.]{2,30})")

# Words that identify the app the screenshot was taken in (UI chrome).
PLATFORM_CUES = {
    "instagram": [r"\binstagram\b", r"\bliked by\b", r"\bview all \d+ comments\b", r"\breels?\b", r"\d[\d,.]*\s*likes\b"],
    "facebook": [r"\bfacebook\b", r"\blike\s+comment\s+share\b", r"\bwrite a comment\b", r"\bmost relevant\b", r"\bsuggested for you\b"],
    "twitter": [r"\bretweets?\b", r"\breposts?\b", r"\bquote\b", r"\bpost your reply\b", r"\btwitter\b", r"\bx\.com\b"],
    "reddit": [r"\br/\w+", r"\bupvotes?\b", r"\breddit\b", r"\bu/\w+"],
    "tiktok": [r"\btiktok\b", r"\bfor you\b", r"\boriginal sound\b"],
    "youtube": [r"\byoutube\b", r"\bsubscribe\b", r"\b[\d.,]+[km]? views\b"],
    "linkedin": [r"\blinkedin\b", r"\breactions?\b", r"\b\d+(st|nd|rd)\+?\b.*\bfollow\b", r"\brepost\b"],
    "threads": [r"\bthreads\b"],
    "whatsapp": [r"\bwhatsapp\b", r"\bforwarded\b"],
    "telegram": [r"\btelegram\b"],
}

CATEGORY_CUES = {
    "recipe": [r"\bingredients?\b", r"\btbsp\b", r"\btsp\b", r"\bcups?\b", r"\bpreheat\b", r"\boven\b", r"\bbake\b",
               r"\brecipe\b", r"\bservings?\b", r"\bgrams?\b|\b\d+\s?g\b", r"\bminutes?\b"],
    "movie": [r"\bfilm\b", r"\bmovie\b", r"\bdirected by\b", r"\bstarring\b", r"\bin theaters\b", r"\btrailer\b",
              r"\bimdb\b", r"\brotten tomatoes\b", r"\bbox office\b", r"\ba24\b", r"\bcinema\b"],
    "tv_show": [r"\bseason \d+\b", r"\bepisodes?\b", r"\bseries\b", r"\bnetflix\b", r"\bhbo\b", r"\bhulu\b",
                r"\bdisney\+", r"\bapple tv\b", r"\bprime video\b", r"\bbinge\b", r"\bshow\b"],
    "github_repo": [r"github\.com", r"\brepo(sitory)?\b", r"\bstars?\b", r"\bfork\b", r"\bpip install\b",
                    r"\bnpm (i|install)\b", r"\bopen[- ]source\b", r"\breadme\b", r"\bcli\b", r"\blibrary\b"],
    "book": [r"\bbook\b", r"\bnovel\b", r"\bauthor\b", r"\bpaperback\b", r"\bkindle\b", r"\bgoodreads\b", r"\bchapter\b"],
    "podcast": [r"\bpodcast\b", r"\bepisode\b", r"\bspotify\b", r"\blisten\b"],
    "music": [r"\balbum\b", r"\bsong\b", r"\bspotify\b", r"\bsingle\b", r"\bplaylist\b"],
    "product": [r"\$\d", r"₪\s?\d", r"€\s?\d", r"\bbuy\b", r"\bshop\b", r"\bprice\b", r"\bfree shipping\b", r"\bamazon\b"],
    "place": [r"\brestaurant\b", r"\bcafe\b", r"\bbar\b", r"\bopen now\b", r"\bdirections\b", r"\bgoogle maps\b"],
}

# Lines that are UI chrome, never a title.
CHROME_RE = re.compile(
    r"^(instagram|facebook|sponsored|follow(ing)?|like|comment|share|reply|send|more|see more|view all.*|"
    r"\d[\d,.]*\s*(likes?|views?|comments?|shares?)|liked by.*|suggested.*|\d{1,2}:\d{2}.*|\d+[hmdw])$",
    re.I,
)


@dataclass
class Signals:
    platform: str | None = None
    category: str | None = None
    category_score: int = 0
    title_candidates: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    github_repos: list[str] = field(default_factory=list)
    imdb_ids: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)

    def as_hint_text(self) -> str:
        parts = []
        if self.platform:
            parts.append(f"looks like a {self.platform} screenshot")
        if self.category:
            parts.append(f"vocabulary suggests: {self.category}")
        if self.title_candidates:
            parts.append("most prominent text: " + " | ".join(self.title_candidates[:3]))
        for label, values in (("links", self.urls), ("GitHub repos", self.github_repos),
                              ("IMDb ids", self.imdb_ids), ("handles", self.handles)):
            if values:
                parts.append(f"{label}: {', '.join(values[:5])}")
        return "; ".join(parts)


def extract_signals(ocr: OcrResult) -> Signals:
    text = ocr.text
    low = text.lower()
    s = Signals()

    s.github_repos = list(dict.fromkeys(f"{o}/{r.rstrip('.,;:')}" for o, r in GITHUB_RE.findall(text)))
    s.imdb_ids = list(dict.fromkeys(IMDB_RE.findall(text)))
    s.handles = list(dict.fromkeys(HANDLE_RE.findall(text)))
    s.urls = list(dict.fromkeys(u.rstrip(".,;:") for u in URL_RE.findall(text) if "." in u))

    platform_scores = Counter({p: sum(bool(re.search(c, low)) for c in cues) for p, cues in PLATFORM_CUES.items()})
    platform, score = platform_scores.most_common(1)[0]
    s.platform = platform if score else None

    cat_scores = Counter({c: sum(len(re.findall(cue, low)) for cue in cues) for c, cues in CATEGORY_CUES.items()})
    if s.github_repos:
        cat_scores["github_repo"] += 5
    if s.imdb_ids:
        cat_scores["movie"] += 3
    category, score = cat_scores.most_common(1)[0]
    if score >= 2:
        s.category, s.category_score = category, score

    # Title candidates: the tallest confident lines that aren't UI chrome, handles or links.
    candidates = [
        l for l in ocr.lines
        if l.score >= 0.6 and len(l.text) >= 2 and not CHROME_RE.match(l.text.strip())
        and not l.text.startswith("@") and not URL_RE.fullmatch(l.text.strip())
        and l.text.strip().lower() not in {h.lower() for h in s.handles}
    ]
    if candidates:
        median = sorted(l.height for l in ocr.lines)[len(ocr.lines) // 2] or 1
        big = sorted((l for l in candidates if l.height >= 1.3 * median), key=lambda l: -l.height)
        s.title_candidates = [l.text for l in big[:3]]
    return s
