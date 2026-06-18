import asyncio
import csv
import io
import json
import logging
import math
import urllib.parse
from datetime import date
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .auth import clear_session_cookie, is_authenticated, make_session_cookie
from .config import settings
from .db import (
    delete_all_items,
    delete_item,
    get_all_genres,
    get_all_items,
    get_item,
    init_db,
    insert_item,
    list_items,
    update_item,
    upsert_item,
    get_db,
)
from .enrich import enrich_item, _ai_tags, call_ai, call_ai_multi, ai_log
from . import tmdb, googlebooks, openlibrary
from .scraper import scrape_url

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="MediaTriage")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["urlencode"] = lambda s: urllib.parse.quote_plus(s or "")
templates.env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)

PAGE_SIZE = 24


def _grid_ctx(request, items, total, page, all_genres, filters, sort_by, section):
    return {
        "request": request,
        "items": items,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / PAGE_SIZE)),
        "all_genres": all_genres,
        "filters": filters,
        "sort_by": sort_by,
        "section": section,
        "image_base": settings.tmdb_image_base,
        "fast_mode": request.cookies.get("mt_fast") != "0",
        "ai_mode": request.cookies.get("mt_ai") != "0",
        "zen_mode": request.cookies.get("mt_zen") == "1",
        "view_mode": request.cookies.get("mt_view", "grid"),
        "book_api": request.cookies.get("mt_book_api", "gb"),
        "zen_date": date.today().strftime("%B %Y"),
    }


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path not in ("/login", "/logout"):
        if not is_authenticated(request):
            return RedirectResponse("/login", status_code=302)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@app.get("/robots.txt")
async def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.on_event("startup")
async def startup():
    init_db()


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.post("/login")
@limiter.limit(settings.login_rate_limit)
async def login(request: Request, password: Annotated[str, Form()]):
    if password != settings.app_password:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Incorrect password."},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=302)
    make_session_cookie(response)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    clear_session_cookie(response)
    return response


# ── Shared helpers ────────────────────────────────────────────────────────────

def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _is_fast_mode(request: Request) -> bool:
    return request.cookies.get("mt_fast") != "0"


def _is_ai_mode(request: Request) -> bool:
    return request.cookies.get("mt_ai") != "0"


def _is_zen_mode(request: Request) -> bool:
    return request.cookies.get("mt_zen") == "1"


def _empty_filters():
    return {"status": "", "media_type": "", "genre": "", "q": ""}


async def _render_grid(request: Request, section: str) -> HTMLResponse:
    with get_db() as conn:
        items, total = list_items(conn, section=section, page=1, page_size=PAGE_SIZE)
        all_genres = get_all_genres(conn, section=section)
    ctx = _grid_ctx(request, items, total, 1, all_genres, _empty_filters(), "date_added", section)
    response = templates.TemplateResponse("partials/item_grid.html", ctx)
    response.headers["HX-Retarget"] = "#grid-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _search_result_response(request: Request, candidates: list, query: str, section: str) -> HTMLResponse:
    response = templates.TemplateResponse(
        "partials/search_results.html",
        {"request": request, "candidates": candidates, "query": query,
         "section": section, "image_base": settings.tmdb_image_base},
    )
    response.headers["HX-Retarget"] = "#grid-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _card_ctx(request, item, section):
    return {"request": request, "item": item, "section": section, "image_base": settings.tmdb_image_base}


# ── Film & TV library ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def film_library(
    request: Request,
    status: str = "",
    media_type: str = "",
    genre: str = "",
    q: str = "",
    page: int = 1,
    sort_by: str = "date_added",
):
    section = "film"
    filters = {"status": status, "media_type": media_type, "genre": genre, "q": q}
    with get_db() as conn:
        items, total = list_items(
            conn, section=section, status=status or None, media_type=media_type or None,
            genre=genre or None, query=q or None, page=page, page_size=PAGE_SIZE, sort_by=sort_by,
        )
        all_genres = get_all_genres(conn, section=section)
    ctx = _grid_ctx(request, items, total, page, all_genres, filters, sort_by, section)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/item_grid.html", ctx)
    return templates.TemplateResponse("library.html", ctx)


@app.post("/add", response_class=HTMLResponse)
@limiter.limit(f"{settings.max_adds_per_hour}/hour")
async def add_film(request: Request, background_tasks: BackgroundTasks, input: Annotated[str, Form()]):
    input = input.strip()
    if not input:
        return HTMLResponse("", status_code=422)

    og_image = None
    if _is_url(input):
        scraped = await scrape_url(input)
        raw_title = scraped.get("title") or input
        og_image = scraped.get("og_image")
    else:
        raw_title = input

    fast = _is_fast_mode(request)
    ai = _is_ai_mode(request)

    if not fast:
        candidates = []
        if ai:
            try:
                ai_results = await call_ai_multi(raw_title, mode="film")
                if len(ai_results) > 1:
                    matches = await asyncio.gather(*[
                        tmdb.search_one(r["title"], r.get("media_type"), year=r.get("year"))
                        for r in ai_results[:8] if r.get("title")
                    ])
                    candidates = [m for m in matches if m]
                elif ai_results:
                    candidates = await tmdb.search_multi(ai_results[0].get("title") or raw_title)
            except Exception:
                pass
        if not candidates:
            candidates = await tmdb.search_multi(raw_title)
        return _search_result_response(request, candidates, input, "film")

    match = await tmdb.search(raw_title)
    if match:
        ai_tags = await _ai_tags(match["title"], match.get("overview", ""), match.get("genres", [])) if ai else []
        data = {**match, "section": "film", "source_url": input if _is_url(input) else None,
                "og_image": og_image, "status": "to_watch", "ai_tags": ai_tags}
        with get_db() as conn:
            upsert_item(conn, data)
    elif ai:
        data = {"title": raw_title, "section": "film", "source_url": input if _is_url(input) else None,
                "og_image": og_image, "status": "pending"}
        with get_db() as conn:
            item_id = insert_item(conn, data)
        background_tasks.add_task(enrich_item, item_id, raw_title, og_image, "film", use_ai=True)
    else:
        data = {"title": raw_title, "section": "film", "source_url": input if _is_url(input) else None,
                "og_image": og_image, "status": "failed"}
        with get_db() as conn:
            insert_item(conn, data)

    return await _render_grid(request, "film")


# ── Books library ─────────────────────────────────────────────────────────────

@app.get("/books/", response_class=HTMLResponse)
async def books_library(
    request: Request,
    status: str = "",
    genre: str = "",
    q: str = "",
    page: int = 1,
    sort_by: str = "date_added",
):
    section = "book"
    filters = {"status": status, "media_type": "", "genre": genre, "q": q}
    with get_db() as conn:
        items, total = list_items(
            conn, section=section, status=status or None,
            genre=genre or None, query=q or None, page=page, page_size=PAGE_SIZE, sort_by=sort_by,
        )
        all_genres = get_all_genres(conn, section=section)
    ctx = _grid_ctx(request, items, total, page, all_genres, filters, sort_by, section)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/item_grid.html", ctx)
    return templates.TemplateResponse("library.html", ctx)


@app.post("/books/add", response_class=HTMLResponse)
@limiter.limit(f"{settings.max_adds_per_hour}/hour")
async def add_book(request: Request, background_tasks: BackgroundTasks, input: Annotated[str, Form()]):
    input = input.strip()
    if not input:
        return HTMLResponse("", status_code=422)

    og_image = None
    if _is_url(input):
        scraped = await scrape_url(input)
        raw_title = scraped.get("title") or input
        og_image = scraped.get("og_image")
    else:
        raw_title = input

    api = request.cookies.get("mt_book_api", "gb")
    fast = _is_fast_mode(request)
    ai = _is_ai_mode(request)

    if not fast:
        search_title = raw_title
        if ai:
            try:
                extracted = await call_ai(raw_title, mode="book")
                search_title = extracted.get("title") or raw_title
            except Exception:
                pass
        candidates = await (openlibrary.search_multi if api == "ol" else googlebooks.search_multi)(search_title)
        return _search_result_response(request, candidates, input, "book")

    match = await (openlibrary.search if api == "ol" else googlebooks.search)(raw_title)
    if match:
        ai_tags = await _ai_tags(match["title"], match.get("overview", ""), match.get("genres", [])) if ai else []
        data = {**match, "section": "book", "source_url": input if _is_url(input) else None,
                "og_image": og_image, "status": "to_watch", "ai_tags": ai_tags}
        with get_db() as conn:
            upsert_item(conn, data)
    elif ai:
        data = {"title": raw_title, "section": "book", "media_type": "book",
                "source_url": input if _is_url(input) else None, "og_image": og_image, "status": "pending"}
        with get_db() as conn:
            item_id = insert_item(conn, data)
        background_tasks.add_task(enrich_item, item_id, raw_title, og_image, "book", use_ai=True)
    else:
        data = {"title": raw_title, "section": "book", "media_type": "book",
                "source_url": input if _is_url(input) else None, "og_image": og_image, "status": "failed"}
        with get_db() as conn:
            insert_item(conn, data)

    return await _render_grid(request, "book")


# ── Confirm selection & fast-mode toggle ─────────────────────────────────────

@app.post("/confirm", response_class=HTMLResponse)
async def confirm_item(
    request: Request,
    section: Annotated[str, Form()] = "film",
    title: Annotated[str, Form()] = "",
    tmdb_id: Annotated[str, Form()] = "",
    media_type: Annotated[str, Form()] = "",
    ol_key: Annotated[str, Form()] = "",
    poster_path: Annotated[str, Form()] = "",
    release_year: Annotated[str, Form()] = "",
    genres: Annotated[str, Form()] = "",
    authors: Annotated[str, Form()] = "",
    overview: Annotated[str, Form()] = "",
):
    genre_list = [g.strip() for g in genres.split(",") if g.strip()]
    author_list = [a.strip() for a in authors.split(",") if a.strip()]
    year = int(release_year) if release_year.isdigit() else None

    data: dict = {
        "title": title,
        "section": section,
        "status": "to_watch",
        "poster_path": poster_path or None,
        "release_year": year,
        "genres": genre_list,
        "overview": overview or None,
    }
    if section == "book":
        data["ol_key"] = ol_key or None
        data["authors"] = author_list
        data["media_type"] = "book"
        # Fetch full description; OL keys start with "OL", GB keys are alphanumeric
        if ol_key:
            if ol_key.startswith("OL"):
                full_desc = await openlibrary.fetch_description(ol_key)
            else:
                full_desc = await googlebooks.fetch_description(ol_key)
            if full_desc:
                data["overview"] = full_desc
    else:
        data["tmdb_id"] = int(tmdb_id) if tmdb_id.isdigit() else None
        data["media_type"] = media_type or "movie"
        if data["tmdb_id"] and data["media_type"]:
            data["directors"] = await tmdb.fetch_directors(data["tmdb_id"], data["media_type"])

    with get_db() as conn:
        upsert_item(conn, data)

    return await _render_grid(request, section)


@app.post("/fast-mode", response_class=HTMLResponse)
async def toggle_fast_mode(request: Request):
    new_fast = not _is_fast_mode(request)
    label = "Fast" if new_fast else "Select"
    cls = "btn btn-sm btn-accent" if new_fast else "btn btn-sm"
    html = (
        f'<button id="fast-toggle" class="{cls}" '
        f'hx-post="/fast-mode" hx-target="#fast-toggle" hx-swap="outerHTML" '
        f'title="{"Fast mode: adds top result immediately" if new_fast else "Select mode: pick from results"}">'
        f'{label}</button>'
    )
    response = HTMLResponse(html)
    if not new_fast:
        # Opt into selection mode
        response.set_cookie("mt_fast", "0", max_age=365 * 86400, httponly=True, samesite="lax")
    else:
        # Return to fast mode (default) — delete the opt-out cookie
        response.delete_cookie("mt_fast")
    return response


@app.post("/ai-mode", response_class=HTMLResponse)
async def toggle_ai_mode(request: Request):
    new_ai = not _is_ai_mode(request)
    label = "AI"
    cls = "btn btn-sm btn-accent" if new_ai else "btn btn-sm"
    html = (
        f'<button id="ai-toggle" class="{cls}" '
        f'hx-post="/ai-mode" hx-target="#ai-toggle" hx-swap="outerHTML" '
        f'title="{"AI mode: identifies titles and descriptions" if new_ai else "AI mode off: direct search only"}">'
        f'{label}</button>'
    )
    response = HTMLResponse(html)
    if not new_ai:
        response.set_cookie("mt_ai", "0", max_age=365 * 86400, httponly=True, samesite="lax")
    else:
        response.delete_cookie("mt_ai")
    return response


@app.post("/zen-mode", response_class=HTMLResponse)
async def toggle_zen_mode(request: Request):
    new_zen = not _is_zen_mode(request)
    response = HTMLResponse("")
    if new_zen:
        response.set_cookie("mt_zen", "1", max_age=365 * 86400, httponly=True, samesite="lax")
    else:
        response.delete_cookie("mt_zen")
    return response


@app.post("/book-api", response_class=HTMLResponse)
async def toggle_book_api(request: Request):
    new_api = "ol" if request.cookies.get("mt_book_api", "gb") == "gb" else "gb"
    response = HTMLResponse("")
    response.set_cookie("mt_book_api", new_api, max_age=365 * 86400, httponly=True, samesite="lax")
    return response


@app.post("/view-mode", response_class=HTMLResponse)
async def toggle_view_mode(request: Request):
    new_mode = "list" if request.cookies.get("mt_view", "grid") == "grid" else "grid"
    response = HTMLResponse("")
    response.set_cookie("mt_view", new_mode, max_age=365 * 86400, httponly=True, samesite="lax")
    return response


# ── Item detail ───────────────────────────────────────────────────────────────

@app.get("/items/{item_id}/detail", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    with get_db() as conn:
        item = get_item(conn, item_id)
    if not item:
        return HTMLResponse("", status_code=404)

    section = item.get("section", "film")

    if section == "book":
        details = item  # all needed data is already stored
    else:
        details = None
        if item.get("tmdb_id") and item.get("media_type"):
            details = await tmdb.get_details(item["tmdb_id"], item["media_type"])
        details = details or item

    return templates.TemplateResponse(
        "partials/item_detail.html",
        {"request": request, "item": item, "details": details, "section": section,
         "image_base": settings.tmdb_image_base},
    )


# ── Item actions (shared) ─────────────────────────────────────────────────────

@app.post("/items/{item_id}/cancel", response_class=HTMLResponse)
async def cancel_item(request: Request, item_id: int):
    with get_db() as conn:
        item = get_item(conn, item_id)
        if not item or item["status"] != "pending":
            return HTMLResponse("", status_code=404)
        update_item(conn, item_id, {"status": "failed"})
        item = get_item(conn, item_id)
    return templates.TemplateResponse(
        "partials/item_card.html",
        _card_ctx(request, item, item.get("section", "film")),
    )


@app.post("/items/{item_id}/toggle-watched", response_class=HTMLResponse)
async def toggle_watched(request: Request, item_id: int):
    with get_db() as conn:
        item = get_item(conn, item_id)
        if not item:
            return HTMLResponse("", status_code=404)
        new_status = "watched" if item["status"] == "to_watch" else "to_watch"
        update_item(conn, item_id, {"status": new_status})
        item = get_item(conn, item_id)
    return templates.TemplateResponse(
        "partials/item_card.html",
        _card_ctx(request, item, item.get("section", "film")),
    )


@app.delete("/items/{item_id}")
async def remove_item(item_id: int):
    with get_db() as conn:
        delete_item(conn, item_id)
    return Response(status_code=200)


@app.get("/items/{item_id}/status", response_class=HTMLResponse)
async def item_status(request: Request, item_id: int):
    with get_db() as conn:
        item = get_item(conn, item_id)
    if not item:
        return Response(status_code=404)
    if item["status"] == "pending":
        return Response(status_code=204)
    return templates.TemplateResponse(
        "partials/item_card.html",
        _card_ctx(request, item, item.get("section", "film")),
    )


@app.post("/items/{item_id}/edit", response_class=HTMLResponse)
async def edit_item(
    request: Request, item_id: int,
    title: Annotated[str, Form()],
    genres: Annotated[str, Form()] = "",
):
    genre_list = [g.strip() for g in genres.split(",") if g.strip()]
    with get_db() as conn:
        update_item(conn, item_id, {"title": title, "genres": genre_list})
        item = get_item(conn, item_id)
    return templates.TemplateResponse(
        "partials/item_card.html",
        _card_ctx(request, item, item.get("section", "film")),
    )


# ── Delete all ────────────────────────────────────────────────────────────────

@app.delete("/items", response_class=HTMLResponse)
async def remove_all_items(request: Request, section: str = "film"):
    with get_db() as conn:
        delete_all_items(conn, section=section)
    return await _render_grid(request, section)


# ── AI log ────────────────────────────────────────────────────────────────────

@app.get("/ai-log/entries", response_class=HTMLResponse)
async def ai_log_entries_compact(request: Request):
    entries = list(reversed(ai_log))
    return templates.TemplateResponse(
        "partials/ai_log_compact.html",
        {"request": request, "entries": entries},
    )


@app.get("/ai-log", response_class=HTMLResponse)
async def view_ai_log(request: Request):
    entries = list(reversed(ai_log))
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/ai_log_entries.html",
            {"request": request, "entries": entries},
        )
    return templates.TemplateResponse(
        "ai_log.html",
        {"request": request, "entries": entries, "section": ""},
    )


# ── Export ────────────────────────────────────────────────────────────────────

_EXPORT_FIELDS = ["id", "title", "section", "media_type", "authors", "genres",
                  "release_year", "status", "date_added", "overview", "tmdb_id",
                  "ol_key", "source_url", "ai_tags"]


@app.get("/export")
async def export_items(
    format: str = "json",
    section: str = "",
    status: str = "",
    media_type: str = "",
    genre: str = "",
    q: str = "",
):
    with get_db() as conn:
        items = get_all_items(
            conn, section=section or None, status=status or None,
            media_type=media_type or None, genre=genre or None, query=q or None,
        )

    if format == "csv":
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=_EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow({
                **item,
                "genres": ", ".join(item.get("genres", [])),
                "authors": ", ".join(item.get("authors", [])),
                "ai_tags": ", ".join(item.get("ai_tags", [])),
            })
        return Response(
            content=out.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=mediatriage.csv"},
        )

    clean = [{k: item.get(k) for k in _EXPORT_FIELDS} for item in items]
    return Response(
        content=json.dumps(clean, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=mediatriage.json"},
    )
