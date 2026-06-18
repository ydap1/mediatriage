import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime

import httpx

from .config import settings
from .db import get_db, update_item
from . import tmdb
from . import googlebooks

log = logging.getLogger("mediatriage.enrich")

ai_log: deque = deque(maxlen=100)


async def call_ai(raw_input: str, mode: str = "film") -> dict:
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
    t0 = time.monotonic()
    result = None
    error = None
    try:
        async with httpx.AsyncClient() as client:
            resp = await asyncio.wait_for(
                client.post(
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
                    timeout=12,
                ),
                timeout=15.0,
            )
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        log.info("OpenRouter response | %s", result)
        return result
    except Exception as e:
        error = str(e)
        raise
    finally:
        ai_log.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "mode": mode,
            "input": raw_input,
            "prompt": prompt,
            "response": result,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": error,
        })


async def call_ai_multi(raw_input: str, mode: str = "film") -> list[dict]:
    """Like call_ai but returns a list — supports 'movies by X' style queries."""
    if mode == "book":
        result = await call_ai(raw_input, mode="book")
        return [result]

    prompt = (
        'Identify the film(s) or TV show(s) being referenced or described. '
        'If the input is a single title or description, return one item. '
        'If the input asks for multiple (e.g. "movies by X", "best films about Y", a query in any language): '
        'return all relevant titles up to 8. '
        'Return ONLY valid JSON: '
        '{"results": [{"title": "<exact title>", "media_type": "movie" or "tv", "year": <integer or null>}, ...]}.\n\n'
        f'Input: {raw_input}'
    )

    t0 = time.monotonic()
    result = None
    error = None
    try:
        async with httpx.AsyncClient() as client:
            resp = await asyncio.wait_for(
                client.post(
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
                    timeout=20,
                ),
                timeout=25.0,
            )
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        result = parsed.get("results", [parsed] if "title" in parsed else [])
        log.info("OpenRouter multi | %d results", len(result))
        return result
    except Exception as e:
        error = str(e)
        raise
    finally:
        ai_log.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "mode": mode,
            "input": raw_input,
            "prompt": prompt,
            "response": result,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": error,
        })


async def _ai_tags(title: str, overview: str, genres: list[str]) -> list[str]:
    """Generate vibe tags via OpenRouter. Returns [] on failure."""
    if not settings.enable_ai_tags or not settings.openrouter_api_key:
        return []
    prompt = (
        f'Generate 3-5 short vibe/mood tags for this title. '
        f'Return ONLY valid JSON: {{"tags": ["tag1", "tag2", ...]}}.\n\n'
        f'Title: {title}\nGenres: {", ".join(genres)}\nOverview: {overview or "N/A"}'
    )
    t0 = time.monotonic()
    result = None
    error = None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
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
        result = json.loads(content).get("tags", [])
        return result
    except Exception as e:
        error = str(e)
        return []
    finally:
        ai_log.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "mode": "tags",
            "input": title,
            "prompt": prompt,
            "response": {"tags": result},
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "error": error,
        })


async def enrich_item(item_id: int, raw_input: str, og_image: str | None, section: str = "film", use_ai: bool = True) -> None:
    """Background task: identify via AI (optional), re-query data source, update row."""
    log.info("Enriching item %d | section=%s | use_ai=%s | raw=%r", item_id, section, use_ai, raw_input)
    try:
        if section == "book":
            await _enrich_book(item_id, raw_input, og_image, use_ai)
        else:
            await _enrich_film(item_id, raw_input, og_image, use_ai)
    except Exception as e:
        log.exception("Enrichment failed for item %d: %s", item_id, e)
        with get_db() as conn:
            update_item(conn, item_id, {"status": "failed"})


async def _enrich_film(item_id: int, raw_input: str, og_image: str | None, use_ai: bool = True) -> None:
    if use_ai:
        extracted = await call_ai(raw_input, mode="film")
        clean_title = extracted.get("title") or raw_input
        media_type = extracted.get("media_type")
        year = extracted.get("year")
    else:
        clean_title = raw_input
        media_type = None
        year = None

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


async def _enrich_book(item_id: int, raw_input: str, og_image: str | None, use_ai: bool = True) -> None:
    if use_ai:
        extracted = await call_ai(raw_input, mode="book")
        clean_title = extracted.get("title") or raw_input
        author = extracted.get("author")
    else:
        clean_title = raw_input
        author = None

    log.info("GB search | title=%r author=%r", clean_title, author)
    match = await googlebooks.search(clean_title, author)
    log.info("GB result | %s", match)

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
