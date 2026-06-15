import csv
import io
import json
import logging
import math
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request, Response
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
from .enrich import enrich_item, _ai_tags
from . import tmdb
from .scraper import scrape_url

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="FilmTriage")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 24


def _grid_ctx(request, items, total, page, all_genres, filters, sort_by="date_added"):
    return {
        "request": request,
        "items": items,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / PAGE_SIZE)),
        "all_genres": all_genres,
        "filters": filters,
        "sort_by": sort_by,
        "image_base": settings.tmdb_image_base,
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
    return response


@app.on_event("startup")
async def startup():
    init_db()


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.post("/login")
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


# ── Library ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def library(
    request: Request,
    status: str = "",
    media_type: str = "",
    genre: str = "",
    q: str = "",
    page: int = 1,
    sort_by: str = "date_added",
):
    filters = {"status": status, "media_type": media_type, "genre": genre, "q": q}
    with get_db() as conn:
        items, total = list_items(
            conn,
            status=status or None,
            media_type=media_type or None,
            genre=genre or None,
            query=q or None,
            page=page,
            page_size=PAGE_SIZE,
            sort_by=sort_by,
        )
        all_genres = get_all_genres(conn)

    ctx = _grid_ctx(request, items, total, page, all_genres, filters, sort_by)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/item_grid.html", ctx)
    return templates.TemplateResponse("library.html", ctx)


# ── Add item ──────────────────────────────────────────────────────────────────

def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


async def _render_grid(request: Request) -> HTMLResponse:
    """Re-render the full grid (used after add/delete-all to refresh state)."""
    with get_db() as conn:
        items, total = list_items(conn, page=1, page_size=PAGE_SIZE)
        all_genres = get_all_genres(conn)
    ctx = _grid_ctx(
        request, items, total, 1, all_genres,
        {"status": "", "media_type": "", "genre": "", "q": ""},
    )
    response = templates.TemplateResponse("partials/item_grid.html", ctx)
    response.headers["HX-Retarget"] = "#grid-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@app.post("/add", response_class=HTMLResponse)
@limiter.limit(f"{settings.max_adds_per_hour}/hour")
async def add_item(
    request: Request,
    background_tasks: BackgroundTasks,
    input: Annotated[str, Form()],
):
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

    match = await tmdb.search(raw_title)

    if match:
        ai_tags = await _ai_tags(match["title"], match.get("overview", ""), match.get("genres", []))
        data = {
            **match,
            "source_url": input if _is_url(input) else None,
            "og_image": og_image,
            "status": "to_watch",
            "ai_tags": ai_tags,
        }
        with get_db() as conn:
            item_id, is_duplicate = upsert_item(conn, data)

        return await _render_grid(request)
    else:
        data = {
            "title": raw_title,
            "source_url": input if _is_url(input) else None,
            "og_image": og_image,
            "status": "pending",
        }
        with get_db() as conn:
            item_id = insert_item(conn, data)
        background_tasks.add_task(enrich_item, item_id, raw_title, og_image)
        return await _render_grid(request)


# ── Item detail ───────────────────────────────────────────────────────────────

@app.get("/items/{item_id}/detail", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    with get_db() as conn:
        item = get_item(conn, item_id)
    if not item:
        return HTMLResponse("", status_code=404)

    details = None
    if item.get("tmdb_id") and item.get("media_type"):
        details = await tmdb.get_details(item["tmdb_id"], item["media_type"])

    return templates.TemplateResponse(
        "partials/item_detail.html",
        {
            "request": request,
            "item": item,
            "details": details or item,
            "image_base": settings.tmdb_image_base,
        },
    )


# ── Item actions ──────────────────────────────────────────────────────────────

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
        {"request": request, "item": item, "image_base": settings.tmdb_image_base},
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
        {"request": request, "item": item, "image_base": settings.tmdb_image_base},
    )


@app.post("/items/{item_id}/edit", response_class=HTMLResponse)
async def edit_item(
    request: Request,
    item_id: int,
    title: Annotated[str, Form()],
    genres: Annotated[str, Form()] = "",
):
    genre_list = [g.strip() for g in genres.split(",") if g.strip()]
    with get_db() as conn:
        update_item(conn, item_id, {"title": title, "genres": genre_list})
        item = get_item(conn, item_id)
    return templates.TemplateResponse(
        "partials/item_card.html",
        {"request": request, "item": item, "image_base": settings.tmdb_image_base},
    )


# ── Delete all ────────────────────────────────────────────────────────────────

@app.delete("/items", response_class=HTMLResponse)
async def remove_all_items(request: Request):
    with get_db() as conn:
        delete_all_items(conn)
    return await _render_grid(request)


# ── Export ────────────────────────────────────────────────────────────────────

_EXPORT_FIELDS = ["id", "title", "media_type", "genres", "release_year", "status",
                  "date_added", "overview", "tmdb_id", "source_url", "ai_tags"]


@app.get("/export")
async def export_items(
    format: str = "json",
    status: str = "",
    media_type: str = "",
    genre: str = "",
    q: str = "",
):
    with get_db() as conn:
        items = get_all_items(
            conn,
            status=status or None,
            media_type=media_type or None,
            genre=genre or None,
            query=q or None,
        )

    if format == "csv":
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=_EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow({**item, "genres": ", ".join(item.get("genres", [])),
                             "ai_tags": ", ".join(item.get("ai_tags", []))})
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=filmtriage.csv"},
        )

    clean = [{k: item.get(k) for k in _EXPORT_FIELDS} for item in items]
    return Response(
        content=json.dumps(clean, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=filmtriage.json"},
    )
