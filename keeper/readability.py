"""Main-content extraction for web pages (a "reader view" of the page).

Uses trafilatura, which strips navigation, ads and footers and returns the article text
plus metadata (title, author, site, date, lead image). Gives unrecognized pages a useful
card and makes their full text searchable.
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

log = logging.getLogger(__name__)

WORDS_PER_MINUTE = 230
MAX_TEXT_CHARS = 20_000


@dataclass
class Article:
    title: str | None = None
    author: str | None = None
    site_name: str | None = None
    published_date: str | None = None
    excerpt: str | None = None
    image: str | None = None
    language: str | None = None
    text: str = ""
    word_count: int = 0

    @property
    def reading_minutes(self) -> int:
        return max(1, round(self.word_count / WORDS_PER_MINUTE)) if self.word_count else 0

    @property
    def is_article(self) -> bool:
        """Enough running text to read as an article, not a landing page or app shell."""
        return self.word_count >= 250


def extract(html: str, url: str) -> Article | None:
    if not html:
        return None
    try:
        import trafilatura
    except ImportError:  # optional dependency: fall back to OpenGraph only
        log.warning("trafilatura is not installed; skipping readability extraction")
        return None
    try:
        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False,
                                          include_tables=False, favor_precision=True)
    except Exception:
        log.exception("Readability extraction failed for %s", url)
        return None
    if doc is None:
        return None
    d = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
    text = (d.get("text") or "").strip()
    words = len(re.findall(r"\w+", text))
    excerpt = d.get("description") or _first_sentences(text)
    return Article(
        title=d.get("title") or None,
        author=d.get("author") or None,
        site_name=d.get("sitename") or None,
        published_date=d.get("date") or None,
        excerpt=excerpt or None,
        image=urljoin(url, d["image"]) if d.get("image") else None,
        language=d.get("language") or None,
        text=text[:MAX_TEXT_CHARS],
        word_count=words,
    )


def _first_sentences(text: str, limit: int = 280) -> str:
    paragraph = next((p.strip() for p in text.split("\n") if len(p.strip()) > 60), text.strip())
    if len(paragraph) <= limit:
        return paragraph
    cut = paragraph[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "…").strip()
