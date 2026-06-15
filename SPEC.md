# FilmTriage — Project Spec

## Overview

Lightweight, self-hosted watchlist service. Save a film or TV show by name or link; the service identifies, tags, and categorises it using TMDb and OpenRouter, storing it in a personal watchlist you can browse and search.

## Priorities

- Lightweight and fast: minimal dependencies, single container, instant page loads.
- Single-user: no multi-tenancy.
- Simple to run: one `docker-compose up` with a `.env` file for API keys.

## Tech Stack

- **Backend:** Python 3.12 + FastAPI
- **Database:** SQLite with FTS5, stored in a mounted volume
- **Frontend:** Jinja2 server-rendered HTML + htmx — no JS framework, no build step
- **Background work:** `asyncio` background tasks (no Celery/Redis)
- **Container:** single Dockerfile, `python:3.12-slim` base, port 5543

## Core Features

### 1. Add Item

Input accepts either a plain title ("The Bear") or a URL (streaming page, IMDb, Letterboxd, YouTube, social post).

**On submit — synchronous-first flow:**

- **Plain title:** query TMDb `/search/multi`, return best match synchronously. Done — no async needed.
- **URL:** scrape page for `og:title`, `og:description`, `og:image` (httpx + BeautifulSoup). Use extracted title to query TMDb. If TMDb hits, store synchronously with TMDb poster. If no match, `og:image` is used as fallback poster.
- **OpenRouter fallback (async):** only triggered when TMDb returns no confident match for the extracted/raw title. Insert a placeholder row immediately (status: `pending`), then run enrichment in the background:
  - Call OpenRouter (cheap/fast model, configurable) to extract a clean title + media type.
  - Re-query TMDb with the clean title.
  - Update the row on success; mark `failed` if enrichment errors out.
  - UI polls the pending card every 2s; stops after 30s and shows a "couldn't identify" state with whatever partial data is available.

**Stored fields:** title, TMDb ID, media type (movie/tv), genres (JSON), poster path, overview, release/first-air date, original source URL, `og:image` URL (fallback), status (`to_watch`/`watched`/`pending`/`failed`), date added.

If TMDb or OpenRouter calls fail, store the item with whatever info we have and mark it `failed` rather than erroring the whole add.

### 2. Library View

Paginated grid/list, server-rendered, htmx for pagination and filtering without full reload.

- Display: poster, title, year, type (movie/tv), genres, status.
- Filters: status, media type, genre.
- Search: FTS5 full-text search on title + overview (single `CREATE VIRTUAL TABLE items_fts USING fts5(...)` + trigger to keep in sync — no extra dependency).

### 3. Item Actions

- Toggle watched / to-watch (htmx swap, no reload).
- Delete item.
- Manual edit of title/genres if enrichment got it wrong.

### 4. Tagging

- TMDb genres as base taxonomy, stored as JSON.
- Optional: OpenRouter "vibe" tags (mood, themes). Gated behind `ENABLE_AI_TAGS=true` — off by default since it's an extra paid call per item.

### 5. Posters

- Store `poster_path` from TMDb (not the full URL). Construct full URL at render time: `{TMDB_IMAGE_BASE}{poster_path}`.
- Fallback priority: TMDb poster → `og:image` from scrape → CSS placeholder (title text in a grey box).
- `TMDB_IMAGE_BASE` defaults to `https://image.tmdb.org/t/p/w342`.
- Local poster cache (download to `/data/posters/{tmdb_id}.jpg`, serve locally, fall back to CDN) is **deferred to v2** — adds complexity for marginal benefit in v1.

## API Endpoints

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/add` | Accepts title or URL. Returns htmx fragment: item card (complete or pending). |
| `GET` | `/library` | Paginated HTML fragment. Query params: `status`, `type`, `genre`, `q` (search), `page`. |
| `POST` | `/items/{id}/toggle-watched` | Returns updated item card fragment. |
| `DELETE` | `/items/{id}` | Returns empty 200 for htmx swap. |
| `GET` | `/items/{id}/status` | Polling endpoint. Returns updated fragment when enrichment completes; 204 while still pending. Client stops polling after 30s. |
| `GET` | `/login` | Login form (no session required). |
| `POST` | `/login` | Validates `APP_PASSWORD`, sets signed session cookie. |
| `POST` | `/logout` | Clears session cookie. |

App binds to `0.0.0.0:5543`.

## Configuration (`.env`)

```
TMDB_API_KEY=
TMDB_IMAGE_BASE=https://image.tmdb.org/t/p/w342

OPENROUTER_API_KEY=
OPENROUTER_MODEL=google/gemini-flash-1.5   # cheap/fast, change as needed

ENABLE_AI_TAGS=false

DB_PATH=/data/filmtriage.db

APP_PASSWORD=                # required before public exposure
SECRET_KEY=                  # session cookie signing key, required before public exposure
SESSION_MAX_AGE=2592000      # 30 days in seconds

MAX_ADDS_PER_HOUR=20
```

## Auth & Security

- Single shared-password auth via signed session cookie (`itsdangerous` + `SECRET_KEY`).
- All routes except `/login` and static assets require a valid session.
- Cookie flags: `HttpOnly`, `SameSite=Lax`, `Secure` (TLS terminated at proxy).
- Security headers middleware: CSP, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`.
- Rate limiting on `/add` via `slowapi` (`MAX_ADDS_PER_HOUR`).
- SSRF protection in `scraper.py`: allow only `http`/`https` schemes, block requests to private/loopback IP ranges.
- Input sanitisation on all user-supplied fields.

## Reverse Proxy Compatibility

- Respect `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host` — use uvicorn's `ProxyHeadersMiddleware`.
- All URLs in templates are relative — no hardcoded `http://` or `localhost`.
- Container binds `0.0.0.0:5543` so proxy can reach it across Docker networks.

## Docker Compose

```yaml
services:
  app:
    build: .
    expose:
      - "5543"        # expose to proxy network only; publish to host if no proxy
    volumes:
      - ./data:/data
    env_file: .env
```

(Use `ports: ["5543:5543"]` instead of `expose` if running without a reverse proxy.)

## Project Structure

```
filmtriage/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── requirements.txt
├── app/
│   ├── main.py          # FastAPI app, routes, middleware
│   ├── db.py            # SQLite setup, schema, FTS5, queries, schema_version check
│   ├── tmdb.py          # TMDb API client
│   ├── enrich.py        # OpenRouter integration + async enrichment logic
│   ├── scraper.py       # URL → og:title/og:description/og:image extraction, SSRF guard
│   └── templates/
│       ├── base.html
│       ├── library.html
│       ├── item_card.html
│       └── add_form.html
└── data/                # gitignored — SQLite db lives here
```

## Schema Versioning

`db.py` reads `PRAGMA user_version` at startup. If it doesn't match the expected version, log a clear error and exit rather than silently operating on a mismatched schema. Increment `user_version` whenever the schema changes.

## Build Order

1. Scaffold structure, Dockerfile, docker-compose.yml, requirements.txt
2. SQLite schema + `db.py` — items table, FTS5 virtual table + sync trigger, schema version check
3. Basic FastAPI app + `/library` rendering seeded test data
4. TMDb client — search by title, get details
5. `/add` — plain-title synchronous path, render item card with poster
6. URL scraper — extract og tags (title, description, image), SSRF protection
7. OpenRouter fallback — async enrichment, pending/failed states, htmx polling (2s interval, 30s timeout)
8. Item actions — toggle watched, delete, manual edit
9. Filters + FTS5 search on `/library`
10. Auth — login page, session cookie middleware, rate limiting
11. Polish — minimal CSS, poster grid, placeholders, security headers
