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

**Clients:** a web UI served by the backend and a native **iOS app** with a Share Extension. The iOS app works **offline** and syncs when it can reach your server.

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

## iOS app

A SwiftUI app (iOS 17+) in [`ios/`](ios/).

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
| `GET` | `/api/items` | `?q=` full-text, `?category=`, `?tag=` (repeatable) |
| `GET` / `PATCH` / `DELETE` | `/api/items/{id}` | PATCH accepts `title`, `subtitle`, `summary`, `category`, `note`, `canonical_url`, `tags` |
| `POST` | `/api/items/{id}/reanalyze` | Run identification again |
| `GET` | `/api/sync?since=` | Delta sync: `{server_time, items, deleted}`. Pass `server_time` back as the next `since` |
| `GET` | `/api/tags`, `/api/categories` | Counts for filters |
| `GET` | `/media/{file}` | Original screenshots |

When `KEEPER_API_TOKEN` is set, send `Authorization: Bearer <token>`.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock Claude and every external API, so they need no keys or network.
