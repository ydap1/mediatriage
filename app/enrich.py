import json
import logging
import httpx

from .config import settings
from .db import get_db, update_item
from . import tmdb
from . import openlibrary

log = logging.getLogger("mediatriage.enrich")


async def _call_openrouter(raw_input: str, mode: str = "film") -> dict:
    """Ask OpenRouter to identify a film/show or book from a title or description."""
    if mode == "book":
        prompt = (
            f'Identify the book being referenced or described. '
            f'The input may be a title, partial title, or plot/description with no title. '
            f'Return ONLY valid JSON: {{"title": "<exact title>", "author": "<author name or null>", "year": <year or null>}}.\n\n'
            f'Input: {raw_input}'
        )
    else:
        prompt = (
            f'Identify the film or TV show being referenced or described. '
            f'The input may be an exact title, a partial title, or a plot description with no title. '
            f'Return ONLY valid JSON: {{"title": "<exact title>", "media_type": "movie" or "tv", "year": <release year as integer or null>}}.\n\n'
            f'Input: {raw_input}'
        )

    log.info("OpenRouter request | model=%s | mode=%s | input=%r", settings.openrouter_model, mode, raw_input)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "HTTP-Referer": "https://mediatriage",
            },
            json={
                "model": settings.openrouter_model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    result = json.loads(content)
    log.info("OpenRouter response | %s", result)
    return result


async def _ai_tags(title: str, overview: str, genres: list[str]) -> list[str]:
    """Generate vibe tags via OpenRouter. Returns [] on failure."""
    if not settings.enable_ai_tags or not settings.openrouter_api_key:
        return []
    prompt = (
        f'Generate 3-5 short vibe/mood tags for this title. '
        f'Return ONLY valid JSON: {{"tags": ["tag1", "tag2", ...]}}.\n\n'
        f'Title: {title}\nGenres: {", ".join(genres)}\nOverview: {overview or "N/A"}'
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "HTTP-Referer": "https://mediatriage",
                },
                json={
                    "model": settings.openrouter_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content).get("tags", [])
    except Exception:
        return []


async def enrich_item(item_id: int, raw_input: str, og_image: str | None, section: str = "film") -> None:
    """Background task: identify via OpenRouter, re-query data source, update row."""
    log.info("Enriching item %d | section=%s | raw=%r", item_id, section, raw_input)
    try:
        if section == "book":
            await _enrich_book(item_id, raw_input, og_image)
        else:
            await _enrich_film(item_id, raw_input, og_image)
    except Exception as e:
        log.exception("Enrichment failed for item %d: %s", item_id, e)
        with get_db() as conn:
            update_item(conn, item_id, {"status": "failed"})


async def _enrich_film(item_id: int, raw_input: str, og_image: str | None) -> None:
    extracted = await _call_openrouter(raw_input, mode="film")
    clean_title = extracted.get("title") or None

    if not clean_title:
        log.warning("OpenRouter returned no title for item %d, marking failed", item_id)
        with get_db() as conn:
            update_item(conn, item_id, {"title": raw_input[:200], "status": "failed"})
        return

    media_type = extracted.get("media_type")
    year = extracted.get("year")
    log.info("TMDb search | title=%r media_type=%r year=%r", clean_title, media_type, year)
    match = await tmdb.search(clean_title, media_type, year=year)
    log.info("TMDb result | %s", match)

    if match:
        ai_tags = await _ai_tags(match["title"], match.get("overview", ""), match.get("genres", []))
        update_data = {**match, "status": "to_watch", "ai_tags": ai_tags}
    else:
        update_data = {"title": clean_title, "media_type": media_type, "og_image": og_image, "status": "failed"}

    with get_db() as conn:
        update_item(conn, item_id, update_data)


async def _enrich_book(item_id: int, raw_input: str, og_image: str | None) -> None:
    extracted = await _call_openrouter(raw_input, mode="book")
    clean_title = extracted.get("title") or None

    if not clean_title:
        log.warning("OpenRouter returned no title for book %d, marking failed", item_id)
        with get_db() as conn:
            update_item(conn, item_id, {"title": raw_input[:200], "status": "failed"})
        return

    author = extracted.get("author")
    log.info("OL search | title=%r author=%r", clean_title, author)
    match = await openlibrary.search(clean_title, author)
    log.info("OL result | %s", match)

    if match:
        ai_tags = await _ai_tags(match["title"], match.get("overview", ""), match.get("genres", []))
        update_data = {**match, "status": "to_watch", "ai_tags": ai_tags}
    else:
        update_data = {
            "title": clean_title,
            "media_type": "book",
            "section": "book",
            "og_image": og_image,
            "status": "failed",
        }

    with get_db() as conn:
        update_item(conn, item_id, update_data)
