# 📌 Keeper

Save screenshots of things people recommend: a Facebook post about a TV show, an Instagram reel with a recipe, a tweet about a GitHub repo, a web page reviewing a movie. Keeper works out **what is actually being recommended**, finds the **original source**, adds **metadata for that type of thing**, and files it by **category and tags** so you can find it later.

| Category | What Keeper finds |
|---|---|
| 🎬 Movie / 📺 TV show | IMDb link, IMDb / Rotten Tomatoes / Metacritic / TMDB scores, poster, overview, genres, director/creators, cast, runtime, seasons, where to stream, trailer |
| 💻 GitHub repo | Repository URL, stars, forks, language, topics, license, homepage, last push, preview image |
| 🍳 Recipe | Original recipe page, photo, ingredients, step-by-step instructions, prep/cook/total time, servings, cuisine, rating |
| 📚 Book | Author, cover, first published, pages, ISBN, Open Library link |
| 📰 Article, 🛍️ product, 📍 place, 🎵 music, … | Canonical URL, preview image, site, author, date, description |

It also records **where you saw it** (Facebook, Instagram, web…), who posted it, and the text in the screenshot, and all of that is searchable too.

Every identification comes with a **confidence score** and, when the model isn't sure, a list of **alternatives**. If it got something wrong you can **correct it**: pick an alternative, fix the title/type/year/link, or describe it in words and let Claude look again.

**Clients:** an installable **PWA** (Add to Home Screen on iPhone) and a native **iOS app** with a Share Extension. Both work **offline** and sync when they can reach your self-hosted server.

## How it works

```
screenshot ──► Claude (vision + web search) ──► identification ──► enrichers ──► SQLite (+ full-text index)
               "this Instagram post is about                       GitHub API
                The Bear (2022), IMDb tt14452776"                  TMDB + OMDb
                                                                   Open Library
                                                                   recipe page JSON-LD / OpenGraph
```

1. **Identify.** `keeper/analyzer.py` sends the screenshot to Claude (`claude-opus-5` by default) with the web search and web fetch tools. Claude reads the post, finds the thing being recommended, confirms it online, and returns a strict, schema-validated result: category, source platform, title, canonical URL, summary, tags, and type-specific details.
2. **Enrich.** `keeper/enrich.py` asks authoritative sources for the facts: the GitHub API for repos, TMDB and OMDb for film and TV, Open Library for books, and the recipe page's schema.org data for recipes. For anything else it reads the page's OpenGraph tags. Each enricher is best-effort: without an API key you still get what Claude found through web search.
3. **Store & retrieve.** `keeper/db.py` keeps items, tags, and an FTS5 full-text index over titles, summaries, tags, metadata and the screenshot's text. You can filter by category and tags and edit anything.

## Confidence & corrections

- **Confidence (0–100).** Claude scores its identification based on the evidence: a legible title confirmed by a matching source scores 90+, while a guess from a blurry poster or an ambiguous title (remakes, a book and its film) scores lower. The score comes with a one-line reason.
- **Needs review.** Anything under 60% gets a "Not sure?" badge and shows up in the **Needs review** filter.
- **Did you mean…** Claude lists up to 3 alternatives. Tap one to switch to it.
- **Fix it.** Open an item → **Wrong? Fix it**:
  - Change the **title, type, year or link**. Keeper keeps the facts about the post itself (who shared it, where, the screenshot text), throws away the wrong item's poster, scores and details, and looks up the right ones. The model isn't called again.
  - Or **describe it** ("it's the 2019 remake, not the original"). Claude looks at the screenshot again with your correction as authoritative context.
  - Corrected items show **Corrected by you** and are never flagged for review again. Tags you added yourself are kept; auto-generated tags are replaced.
- Corrections made offline are queued and synced like any other change.

## Self-hosting the server

### Docker (recommended)

```bash
cp .env.example .env              # add ANTHROPIC_API_KEY, and ideally KEEPER_API_TOKEN
docker compose up -d --build
# → http://<your-server>:8000
```

Data (the SQLite database and uploaded screenshots) lives in `./data`, mounted at `/data`. Back up that folder.

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add ANTHROPIC_API_KEY
uvicorn keeper.main:create_app --factory --host 0.0.0.0 --port 8000
```

### Configuration (`.env`)

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Identifies screenshots |
| `KEEPER_API_TOKEN` | recommended | Shared secret for all API and media requests. Set it whenever the server can be reached from outside localhost. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TMDB_API_KEY` | optional | Posters, overview, cast, genres, streaming providers (v3 key or v4 read token) |
| `OMDB_API_KEY` | optional | IMDb rating, Rotten Tomatoes, Metacritic |
| `GITHUB_TOKEN` | optional | Raises the GitHub API limit from 60 to 5000 requests/hour |
| `KEEPER_REGION` | optional | Region for "where to watch" (default `US`) |
| `KEEPER_MODEL` | optional | Claude model (default `claude-opus-5`) |
| `KEEPER_DATA_DIR` | optional | Where data is stored (default `./data`, `/data` in Docker) |

**Reaching the server from your phone.** Use the server's LAN IP (`http://192.168.1.20:8000`), a [Tailscale](https://tailscale.com) address, or a reverse proxy with HTTPS (Caddy, Traefik, nginx) in front of port 8000. With `KEEPER_API_TOKEN` set, the web UI asks for the token once and keeps it in a cookie.

## iPhone: install as an app (PWA)

The web UI is a Progressive Web App. On iPhone, open your server's address in **Safari**, tap **Share → Add to Home Screen**, and Keeper opens full-screen like a native app.

- **Works offline.** The app itself is cached by a service worker, and your library, queued screenshots and edits are stored in the browser (IndexedDB). You can browse, search, add screenshots (📷 → photo library), edit tags and notes, and correct identifications with no connection.
- **Syncs when the server is back.** Queued changes are replayed in order, then changes made on other devices are pulled down. The status pill in the header shows *Synced*, *Offline · N waiting*, or *Syncing*. iOS doesn't let web apps sync in the background, so syncing happens whenever the app is open.
- **Needs HTTPS.** Service workers (and therefore offline mode) only run on `https://` or `localhost`. Over plain `http://` on your LAN the app still works but needs the server. Easy ways to get HTTPS for a home server: `tailscale serve --bg 8000` (gives a `https://<machine>.<tailnet>.ts.net` address), or Caddy/Traefik with a domain.
- **Limitations vs. the native app:** iOS doesn't support the Web Share Target API, so you can't share into the PWA from other apps; pick screenshots with the ➕ button instead. The native app below has a Share Extension. On Android and desktop Chrome, the installed PWA *does* appear in the share sheet.

## Native iOS app

A SwiftUI app (iOS 17+) in [`ios/`](ios/). It works like the PWA, but adds a Share Extension and background refresh.

- **Share → Keeper** from Photos, the screenshot editor, Instagram, Safari, or any other app, with an optional note ("Dana recommended").
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
open Keeper.xcodeproj
```

Before running on a device:
1. Set your team (`DEVELOPMENT_TEAM`) and change `bundleIdPrefix` in `project.yml`.
2. Change the App Group `group.com.example.keeper` to one you own. It appears in `project.yml`, `Shared/AppGroup.swift`, and both `.entitlements` files. The app and the Share Extension use it to hand over screenshots.
3. Run the app, open **Settings** (top-left icon), and enter your server URL and API token. **Test connection** checks both.

`NSAllowsArbitraryLoads` is enabled so plain-`http` LAN/Tailscale servers work. If your server is behind HTTPS you can remove it from `project.yml`.

## API

| Method | Path | |
|---|---|---|
| `GET` | `/api/health` | Reachability check (no auth) |
| `POST` | `/api/items` | Multipart: `file` (PNG/JPEG/WebP/GIF), optional `note`, `tags` (comma-separated), `id` (client-generated, idempotent), `created_at`. Returns `202`; analysis runs in the background |
| `GET` | `/api/items` | `?q=` full-text, `?category=`, `?tag=` (repeatable), `?needs_review=true` |
| `GET` / `PATCH` / `DELETE` | `/api/items/{id}` | PATCH accepts `title`, `subtitle`, `summary`, `category`, `note`, `canonical_url`, `tags` |
| `POST` | `/api/items/{id}/reanalyze` | Run identification again |
| `POST` | `/api/items/{id}/correct` | JSON with any of `title`, `category`, `year`, `canonical_url` (re-enrich with these facts) and/or `hint` (Claude looks again with your description). Returns `202` |
| `GET` | `/api/sync?since=` | Delta sync: `{server_time, items, deleted}`. Pass `server_time` back as the next `since` |
| `GET` | `/api/tags`, `/api/categories` | Counts for filters |
| `GET` | `/media/{file}` | Original screenshots |

Items include `confidence` (0–100), `confidence_reason`, `alternatives`, `corrected` and `needs_review`.

When `KEEPER_API_TOKEN` is set, send `Authorization: Bearer <token>`.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock Claude and every external API, so they need no keys or network.
