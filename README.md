# MediaTriage

Self-hosted watchlist for films, TV shows, and books. Add anything by title or description — metadata is pulled automatically from TMDb and Google Books. Single container, SQLite, no JavaScript framework.

## Features

- **Films & TV** — search TMDb by title or natural language description; poster, cast, director, ratings, runtime
- **Books** — search Google Books by title or author; cover, description, categories
- **Selection mode** — see top 5 matches before confirming, or use Fast mode to add the top result instantly
- **AI identification** — paste a plot description and OpenRouter identifies the title; useful when you can't remember the name
- **Library** — paginated grid with filter by status/genre/type, full-text search, sort by date/title/year
- **Detail modal** — cast photos, external links (TMDb, IMDb, Letterboxd for films; Google Books, Goodreads for books)
- **Export** — download your list as JSON or CSV, with active filters applied
- **Single-user auth** — password-protected with signed session cookie

## Running with Docker

**1. Clone and configure**

```bash
git clone https://github.com/ydap1/mediatriage.git
cd mediatriage
cp .env.example .env
```

Edit `.env`:

```env
TMDB_API_KEY=          # https://www.themoviedb.org/settings/api
GOOGLE_BOOKS_API_KEY=  # https://console.cloud.google.com → Books API
OPENROUTER_API_KEY=    # https://openrouter.ai/keys  (optional, for description search)
OPENROUTER_MODEL=xiaomi/mimo-v2.5

APP_PASSWORD=          # choose a strong password
SECRET_KEY=            # random string, e.g. openssl rand -hex 32

# Set true when behind an HTTPS reverse proxy
COOKIE_SECURE=false
```

**2. Start**

```bash
docker compose -f docker-compose.local.yml up --build
```

Open [http://localhost:5543](http://localhost:5543).

## Deploying with Dockge

Paste the contents of `docker-compose.yml` into Dockge. Create a `.env` file in the stack directory with the variables above, then start the stack. The database is persisted in `./data/mediatriage.db`.

When running behind a reverse proxy, also set:

```env
COOKIE_SECURE=true
```

**Nginx example:**

```nginx
location / {
    proxy_pass         http://127.0.0.1:5543;
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-For $remote_addr;
    proxy_set_header   X-Forwarded-Proto $scheme;
}
```

Using `$remote_addr` (not `$proxy_add_x_forwarded_for`) ensures the rate limiter sees the real client IP and can't be spoofed.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `TMDB_API_KEY` | — | TMDb v3 API key (required for films) |
| `GOOGLE_BOOKS_API_KEY` | — | Google Books API key (required for books) |
| `OPENROUTER_API_KEY` | — | OpenRouter key for description-based search |
| `OPENROUTER_MODEL` | `xiaomi/mimo-v2.5` | Model used for identification |
| `ENABLE_AI_TAGS` | `false` | Generate vibe tags per item (extra API call) |
| `APP_PASSWORD` | `changeme` | Login password |
| `SECRET_KEY` | — | Session cookie signing key |
| `SESSION_MAX_AGE` | `2592000` | Session lifetime in seconds (30 days) |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `LOGIN_RATE_LIMIT` | `10/minute` | Max login attempts per IP |
| `MAX_ADDS_PER_HOUR` | `20` | Rate limit on add endpoint |
| `DB_PATH` | `/data/mediatriage.db` | SQLite database path |

## Tech stack

- **Backend** — Python 3.12, FastAPI, SQLite (FTS5 full-text search)
- **Frontend** — Jinja2 + [htmx](https://htmx.org) — no build step, no JS framework
- **Auth** — `itsdangerous` signed cookies, `slowapi` rate limiting
- **APIs** — TMDb, Google Books, OpenRouter

## Data

All data lives in a single SQLite file at `DB_PATH`. Back it up by copying the file. The schema auto-migrates on startup.

## Attribution

Film and TV data provided by [TMDb](https://www.themoviedb.org). Book data from [Google Books](https://books.google.com).
