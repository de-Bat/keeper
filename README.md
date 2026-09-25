<p align="center"><img src="assets/magpie-logo.png" alt="Magpie logo: a magpie whose wing feathers carry icons for images, code, TV, food and shopping" width="200"></p>

<h1 align="center">Magpie</h1>

<p align="center"><i>Grab the shiny things you see online. Magpie works out what they are and files them for later.</i></p>

Self-hosted, single-user capture for things people recommend. **Share a screenshot or a link** (a Facebook post about a TV show, an Instagram reel with a recipe, a GitHub repo, an IMDb page, an article). Magpie works out **what it actually is**, finds the **original source**, adds **metadata for that type of thing**, and files it by **category and tags** so you can find it later.

- **Links** are recognized from the URL and the page's structured data when possible (GitHub, IMDb, TMDB, Letterboxd, Goodreads, Spotify, app stores, and any page with schema.org data such as recipes, films, books, products and events). That takes no AI model, costs nothing, and is near-instant. Other pages get a **reader view** (main text, excerpt, author, reading time, lead image) and, if a model is configured, a text-only identification.
- **Screenshots** are read by a vision model (Claude, or your own), helped by OCR.

| Category | What Magpie finds |
|---|---|
| 🎬 Movie / 📺 TV show | IMDb link, IMDb / Rotten Tomatoes / Metacritic / TMDB scores, poster, overview, genres, director/creators, cast, runtime, seasons, where to stream, trailer |
| 💻 GitHub repo | Repository URL, stars, forks, language, topics, license, homepage, last push, preview image |
| 🍳 Recipe | Original recipe page, photo, ingredients, step-by-step instructions, prep/cook/total time, servings, cuisine, rating |
| 📚 Book | Author, cover, first published, pages, ISBN, Open Library link |
| 📰 Article, 🛍️ product, 📍 place, 🎵 music, … | Canonical URL, preview image, site, author, date, description |

It also records **where you saw it** (Facebook, Instagram, web…), who posted it, and the text in the screenshot, and all of that is searchable too.

Every identification comes with a **confidence score** and, when the model isn't sure, a list of **alternatives**. If it got something wrong you can **correct it**: pick an alternative, fix the title/type/year/link, or describe it in words and let Claude look again.

**Cost:** about **$0.05 per screenshot** with the default hybrid setup, and every analysis's real cost is measured and shown in the app. See [docs/COSTS.md](docs/COSTS.md). **Model and OCR choices:** [docs/MODELS.md](docs/MODELS.md).

**Coming next:** text selections and notes as items, and sharing links/text straight from other apps. See [docs/ROADMAP.md](docs/ROADMAP.md).

**Clients:** an installable **PWA** (Add to Home Screen on iPhone) and a native **iOS app** with a Share Extension. Both work **offline** and sync when they can reach your self-hosted server.

## How it works

```
screenshot ──► Claude (vision + web search) ──► identification ──► enrichers ──► SQLite (+ full-text index)
               "this Instagram post is about                       GitHub API
                The Bear (2022), IMDb tt14452776"                  TMDB + OMDb
                                                                   Open Library
                                                                   recipe page JSON-LD / OpenGraph
```

1. **Identify.** `magpie/analyzer.py` sends the screenshot to Claude (`claude-opus-5` by default) with the web search and web fetch tools. Claude reads the post, finds the thing being recommended, confirms it online, and returns a strict, schema-validated result: category, source platform, title, canonical URL, summary, tags, and type-specific details.
2. **Enrich.** `magpie/enrich.py` asks authoritative sources for the facts: the GitHub API for repos, TMDB and OMDb for film and TV, Open Library for books, and the recipe page's schema.org data for recipes. For anything else it reads the page's OpenGraph tags. Each enricher is best-effort: without an API key you still get what Claude found through web search.
3. **Store & retrieve.** `magpie/db.py` keeps items, tags, and an FTS5 full-text index over titles, summaries, tags, metadata and the screenshot's text. You can filter by category and tags and edit anything.

## Saving links

Paste a link into **Save link** in the web app, paste it anywhere on the page, or drop it onto the page. In the iOS app, copy the link and tap the clipboard button. Offline, links are queued like screenshots.

What happens to a link, in order:

1. **Clean-up.** Tracking parameters (`utm_*`, `fbclid`, `igshid`, X's `s`/`t`…) and `www.` are removed, so the same link is never saved twice. Saving a duplicate returns the existing item and adds any new tags.
2. **Recognize it from the URL:** `github.com/owner/repo`, `imdb.com/title/tt…`, `themoviedb.org/movie|tv/…`, Letterboxd films, Goodreads books, Spotify, app stores, YouTube.
3. **…or from the page's structured data.** schema.org JSON-LD `@type` (Recipe, Movie, TVSeries, Book, Product, Event, Restaurant, SoftwareApplication, Course, NewsArticle…) or OpenGraph `og:type`.
4. **Enrich it** with the same sources as screenshots (GitHub stars and topics, TMDB poster and cast, recipe ingredients…).
5. **Unrecognized pages** get a reader view (via [trafilatura](https://trafilatura.readthedocs.io/)): the main text without menus and footers, an excerpt, author, date, reading time and lead image. The full text is searchable.
   - If a model is configured, it identifies the page from that text. That's a cheap, text-only request without web search, unless the page couldn't be read.
   - In `ocr` mode, the reader view becomes a generic article card.
   - Pages behind a login (many Instagram and Facebook posts) get a basic card, flagged for review.

Links recognized in steps 2–3 never call a model: **$0 and about a second**.

**Network safety:** Magpie fetches links you share and URLs a model read off a screenshot. To stop those requests from reaching machines on your own network, it refuses any URL (or redirect) that resolves to a private, loopback or link-local address. Set `MAGPIE_ALLOW_PRIVATE_URLS=true` if you want to save pages from your LAN.

## Choosing the AI: Claude, on-prem LLM, or no LLM

Set `MAGPIE_ANALYZER` in `.env`:

| Mode | What runs | Accuracy | Privacy / cost |
|---|---|---|---|
| `claude` | Claude with web search | Best: reads the screenshot, then checks online and finds the IMDb page, repo, recipe… | Screenshot is sent to Anthropic; per-request cost |
| `local` | Your LLM (Ollama, vLLM, LM Studio, llama.cpp, **NVIDIA NIM**: anything OpenAI-compatible) | Good with a 7B+ vision model on well-known titles; no web search, so it relies on what the model knows. The metadata lookups still confirm and fill in the details | Screenshots stay on your network, unless you point it at a hosted API such as NVIDIA's build.nvidia.com |
| **`hybrid`** (recommended; the default in `.env.example`) | Local first; Claude only when the local model's confidence is below `MAGPIE_ESCALATE_BELOW` (default 70) or it fails. If the local server is down, Claude is used and the local server is retried after 5 minutes | Close to `claude` | Only the hard cases leave your network; ~$0.05/screenshot |
| `ocr` | No LLM: OCR + rules | Rough: finds GitHub/IMDb links, the platform, the poster; flags everything for review | Fully local, ~1 s on CPU |
| `auto` (default) | `hybrid` if both are configured, else whichever is, else `ocr` | | |

**On-prem LLM quick start (Ollama):**

```bash
docker compose --profile local up -d                      # starts Magpie + Ollama (GPU block in docker-compose.yml)
docker compose exec ollama ollama pull qwen3-vl:8b        # ~6 GB; a vision model that reads screenshots well
# .env:
#   LOCAL_LLM_URL=http://ollama:11434/v1
#   LOCAL_LLM_MODEL=qwen3-vl:8b
#   MAGPIE_ANALYZER=hybrid         # with ANTHROPIC_API_KEY set; or local for no cloud at all
docker compose up -d magpie
```

Recommended vision models: **Qwen3-VL** (`qwen3-vl:8b` for 8–16 GB, `:30b` / `:32b` for 24 GB) and **Gemma 4** (E4B, 12B, 26B, 31B). An 8B model needs ~6–8 GB of VRAM; on CPU expect 30 s to several minutes per screenshot (`LOCAL_LLM_TIMEOUT`). Text-only models also work with `LOCAL_LLM_VISION=false`: they get the OCR text instead of the image. Hardware tiers, runtimes (Ollama, vLLM, llama.cpp, LM Studio), hosted alternatives and OCR engines are compared in **[docs/MODELS.md](docs/MODELS.md)**.

Magpie asks the server for schema-constrained JSON and falls back to plain JSON mode for servers that don't support it. Small models' sloppy output (wrong category names, `0.8` instead of `80`, missing fields) is normalized.

### OCR + rules pre-pass (runs in every mode)

Before any model sees a screenshot, Magpie runs OCR locally and applies simple rules. You get:

- **Full-text search over everything in the screenshot**, even when identification fails.
- **Clues passed to the model**: GitHub and IMDb links, URLs, @handles, the app the screenshot came from (Instagram "likes" and "View all N comments", Twitter "Reposts", Reddit "r/…"), vocabulary that suggests a recipe, film, TV show or repo, and the biggest text on screen as a title candidate. Small local models gain the most, because they read small print poorly.
- **A second opinion for non-Claude analyzers.** If GitHub, TMDB, Open Library or the recipe page confirms the title the model found, confidence goes up to at least 85 ("Confirmed by tmdb").

Engines (`MAGPIE_OCR`):
- `rapidocr` (default): PaddleOCR models on ONNX Runtime. Installed by `pip`, models included (~16 MB), CPU only, ~1 s per screenshot. Very accurate on Latin and Chinese text. It can drop the spaces in large headline text ("PASTLIVES"); the model and the title check tolerate this.
- `tesseract`: install the binary (the Docker image includes English and Hebrew) and set `MAGPIE_OCR_LANGS=eng+heb`. Use it for Hebrew, Arabic, Cyrillic and other scripts.
- `off`

For a fully **air-gapped** install, combine `MAGPIE_ANALYZER=local` (or `ocr`) with `MAGPIE_ENRICH=off`, so TMDB, GitHub and recipe pages are never contacted.

## Cost

| Mode | Per screenshot |
|---|---|
| `hybrid` (default) | ~$0.05 average (~25% go to Claude) |
| `claude`, batched | ~$0.06 easy · ~$0.21 typical · up to ~$0.70 |
| `local` / `ocr` | electricity only |

These cost controls are on by default:
- **Batches:** new screenshots go to Claude through the Message Batches API, 50% off, with results usually within the hour. Re-analyses and corrections always run immediately.
- **`MAGPIE_EFFORT=medium`:** Claude thinks less, so fewer output tokens.
- **`MAGPIE_FETCH_MAX_TOKENS=8000`:** caps how much of each fetched web page Claude reads.
- **Hybrid mode:** only screenshots the local model is unsure about go to Claude.

**Real costs are measured.** Magpie records the tokens, web searches, time and cost of every analysis. See them under **$ Usage & cost** in the web app, in Settings in the iOS app, per item in its details, or via `GET /api/usage`. The full breakdown, assumptions and tuning advice are in **[docs/COSTS.md](docs/COSTS.md)**.

## Confidence & corrections

- **Confidence (0–100).** Claude scores its identification based on the evidence: a legible title confirmed by a matching source scores 90+, while a guess from a blurry poster or an ambiguous title (remakes, a book and its film) scores lower. The score comes with a one-line reason.
- **Needs review.** Anything under 60% gets a "Not sure?" badge and shows up in the **Needs review** filter.
- **Did you mean…** Claude lists up to 3 alternatives. Tap one to switch to it.
- **Fix it.** Open an item → **Wrong? Fix it**:
  - Change the **title, type, year or link**. Magpie keeps the facts about the post itself (who shared it, where, the screenshot text), throws away the wrong item's poster, scores and details, and looks up the right ones. The model isn't called again.
  - Or **describe it** ("it's the 2019 remake, not the original"). Claude looks at the screenshot again with your correction as authoritative context.
  - Corrected items show **Corrected by you** and are never flagged for review again. Tags you added yourself are kept; auto-generated tags are replaced.
- Corrections made offline are queued and synced like any other change.

## Self-hosting the server

### Docker (recommended)

```bash
cp .env.example .env              # add ANTHROPIC_API_KEY and/or LOCAL_LLM_URL, and ideally MAGPIE_API_TOKEN
docker compose up -d --build
# → http://<your-server>:8000
```

Data (the SQLite database and uploaded screenshots) lives in `./data`, mounted at `/data`. Back up that folder.

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add ANTHROPIC_API_KEY and/or LOCAL_LLM_URL
uvicorn magpie.main:create_app --factory --host 0.0.0.0 --port 8000
```

### Configuration (`.env`)

> Renamed from **Keeper**: existing installs keep working. Old `KEEPER_*` settings are still read, an existing `data/keeper.db` is used, and the old login cookie is accepted.

| Variable | Required | Purpose |
|---|---|---|
| `MAGPIE_ANALYZER` | optional | `auto` (default), `claude`, `local`, `hybrid`, `ocr`. See [Choosing the AI](#choosing-the-ai-claude-on-prem-llm-or-no-llm) |
| `ANTHROPIC_API_KEY` | for `claude`/`hybrid` | Identifies screenshots with Claude |
| `LOCAL_LLM_URL`, `LOCAL_LLM_MODEL` | for `local`/`hybrid` | Your OpenAI-compatible LLM server and model |
| `LOCAL_LLM_PROVIDER`, `NVIDIA_API_KEY` | optional | `nim` for NVIDIA NIM (auto-detected from NVIDIA's API URL or an `nvapi-` key); the key for build.nvidia.com |
| `MAGPIE_OCR`, `MAGPIE_OCR_LANGS` | optional | `rapidocr` (default), `tesseract` (+ languages), `off` |
| `MAGPIE_ENRICH` | optional | `off` disables all online metadata lookups |
| `MAGPIE_ALLOW_PRIVATE_URLS` | optional | `true` lets Magpie fetch links on private/LAN addresses (blocked by default) |
| `MAGPIE_CLAUDE_BATCH` | optional | `true` (default): new screenshots use the 50%-off Batches API; `false`: real time |
| `MAGPIE_EFFORT`, `MAGPIE_FETCH_MAX_TOKENS` | optional | Claude cost controls (defaults `medium`, `8000`) |
| `MAGPIE_LOCAL_COST_PER_HOUR`, `MAGPIE_PRICING` | optional | For the usage report: your local box's running cost; price overrides |
| `MAGPIE_API_TOKEN` | recommended | Shared secret for all API and media requests. Set it whenever the server can be reached from outside localhost. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TMDB_API_KEY` | optional | Posters, overview, cast, genres, streaming providers (v3 key or v4 read token) |
| `OMDB_API_KEY` | optional | IMDb rating, Rotten Tomatoes, Metacritic |
| `GITHUB_TOKEN` | optional | Raises the GitHub API limit from 60 to 5000 requests/hour |
| `MAGPIE_REGION` | optional | Region for "where to watch" (default `US`) |
| `MAGPIE_MODEL` | optional | Claude model (default `claude-opus-5`) |
| `MAGPIE_DATA_DIR` | optional | Where data is stored (default `./data`, `/data` in Docker) |

**Reaching the server from your phone.** Use the server's LAN IP (`http://192.168.1.20:8000`), a [Tailscale](https://tailscale.com) address, or a reverse proxy with HTTPS (Caddy, Traefik, nginx) in front of port 8000. With `MAGPIE_API_TOKEN` set, the web UI asks for the token once and keeps it in a cookie.

## iPhone: install as an app (PWA)

The web UI is a Progressive Web App. On iPhone, open your server's address in **Safari**, tap **Share → Add to Home Screen**, and Magpie opens full-screen like a native app.

- **Works offline.** The app itself is cached by a service worker, and your library, queued screenshots and edits are stored in the browser (IndexedDB). You can browse, search, add screenshots (📷 → photo library), edit tags and notes, and correct identifications with no connection.
- **Syncs when the server is back.** Queued changes are replayed in order, then changes made on other devices are pulled down. The status pill in the header shows *Synced*, *Offline · N waiting*, or *Syncing*. iOS doesn't let web apps sync in the background, so syncing happens whenever the app is open.
- **Needs HTTPS.** Service workers (and therefore offline mode) only run on `https://` or `localhost`. Over plain `http://` on your LAN the app still works but needs the server. Easy ways to get HTTPS for a home server: `tailscale serve --bg 8000` (gives a `https://<machine>.<tailnet>.ts.net` address), or Caddy/Traefik with a domain.
- **Limitations vs. the native app:** iOS doesn't support the Web Share Target API, so you can't share into the PWA from other apps; pick screenshots with the ➕ button instead. The native app below has a Share Extension. On Android and desktop Chrome, the installed PWA *does* appear in the share sheet.

## Native iOS app

A SwiftUI app (iOS 17+) in [`ios/`](ios/). It works like the PWA, but adds a Share Extension and background refresh.

- **Share → Magpie** from Photos, the screenshot editor, Instagram, Safari, or any other app, with an optional note ("Dana recommended").
- Add screenshots from your photo library or paste them from the clipboard.
- Browse by category and tag, search offline, see scores and posters, open the IMDb, GitHub or recipe page, edit tags, notes and category, re-analyze, or delete.

### Offline mode & sync

- **Local first.** The library (items, metadata, your screenshots, cached posters) is stored on the phone, so browsing and searching work with no connection.
- **Queued changes.** New screenshots, tag, note and category edits, re-analysis and deletions are applied locally and put in a queue. The queue is replayed in order once the server is reachable.
- **When it syncs:** on launch and return to the foreground, when the network comes back, after each change, on pull-to-refresh, and from an iOS background refresh task.
- **Safe retries.** Each screenshot gets its ID on the phone, so re-sending an upload after a dropped connection never creates a duplicate.
- **Delta sync.** `GET /api/sync?since=<cursor>` returns only the items changed since the last sync, plus deletions (the server keeps tombstones), so other devices' edits and deletions come down too. Items still being analyzed are checked again every few seconds.
- **Conflicts:** the last write wins. Local items with unsent changes are not overwritten by server data until those changes have gone out.

### Building it

The Xcode project is generated from `ios/project.yml` with [XcodeGen](https://github.com/yonaskolb/XcodeGen):

```bash
brew install xcodegen
cd ios && xcodegen
open Magpie.xcodeproj
```

Before running on a device:
1. Set your team (`DEVELOPMENT_TEAM`) and change `bundleIdPrefix` in `project.yml`.
2. Change the App Group `group.com.example.magpie` to one you own. It appears in `project.yml`, `Shared/AppGroup.swift`, and both `.entitlements` files. The app and the Share Extension use it to hand over screenshots.
3. Run the app, open **Settings** (top-left icon), and enter your server URL and API token. **Test connection** checks both.

`NSAllowsArbitraryLoads` is enabled so plain-`http` LAN/Tailscale servers work. If your server is behind HTTPS you can remove it from `project.yml`.

## API

| Method | Path | |
|---|---|---|
| `GET` | `/api/health` | Reachability check (no auth) |
| `POST` | `/api/items` | Multipart form with **either** `file` (PNG/JPEG/WebP/GIF screenshot) **or** `url` (a link), plus optional `note`, `tags` (comma-separated), `id` (client-generated, idempotent), `created_at`. Returns `202`; identification runs in the background. A link that is already saved returns the existing item with `"duplicate": true` |
| `GET` | `/api/items` | `?q=` full-text, `?category=`, `?tag=` (repeatable), `?needs_review=true` |
| `GET` / `PATCH` / `DELETE` | `/api/items/{id}` | PATCH accepts `title`, `subtitle`, `summary`, `category`, `note`, `canonical_url`, `tags` |
| `POST` | `/api/items/{id}/reanalyze` | Run identification again |
| `POST` | `/api/items/{id}/correct` | JSON with any of `title`, `category`, `year`, `canonical_url` (re-enrich with these facts) and/or `hint` (Claude looks again with your description). Returns `202` |
| `GET` | `/api/sync?since=` | Delta sync: `{server_time, items, deleted}`. Pass `server_time` back as the next `since` |
| `GET` | `/api/tags`, `/api/categories` | Counts for filters |
| `GET` | `/api/usage?days=30` | Measured cost: totals, per screenshot, share sent to Claude, per analyzer, per day |
| `GET` | `/media/{file}` | Original screenshots |

Items include `kind` (`screenshot` or `url`), `source_url` (for links), `confidence` (0–100), `confidence_reason`, `alternatives`, `corrected`, `needs_review`, `batch_pending` and `usage` (`cost_usd`, `runs`, `web_searches`, `via`).

When `MAGPIE_API_TOKEN` is set, send `Authorization: Bearer <token>`.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock Claude and every external API, so they need no keys or network.
