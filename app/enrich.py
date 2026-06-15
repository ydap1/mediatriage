import json
import asyncio
import logging
import httpx

from .config import settings
from .db import get_db, update_item
from . import tmdb

log = logging.getLogger("filmtriage.enrich")


async def _call_openrouter(raw_input: str) -> dict:
    """Ask OpenRouter to identify a film/show from a title or description."""
    prompt = (
        f'Identify the film or TV show being referenced or described. '
        f'The input may be an exact title, a partial title, or a plot description with no title. '
        f'Return ONLY valid JSON with these fields: '
        f'{{"title": "<exact title>", "media_type": "movie" or "tv", "year": <release year as integer or null>}}.\n\n'
        f'Input: {raw_input}'
    )
    log.info("OpenRouter request | model=%s | input=%r", settings.openrouter_model, raw_input)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "HTTP-Referer": "https://filmtriage",
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
        f'Generate 3-5 short vibe/mood tags for this film or show. '
        f'Return ONLY valid JSON: {{"tags": ["tag1", "tag2", ...]}}.\n\n'
        f'Title: {title}\nGenres: {", ".join(genres)}\nOverview: {overview or "N/A"}'
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "HTTP-Referer": "https://filmtriage",
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


async def enrich_item(item_id: int, raw_title: str, og_image: str | None) -> None:
    """Background task: call OpenRouter, re-query TMDb, update item row."""
    log.info("Enriching item %d | raw=%r", item_id, raw_title)
    try:
        extracted = await _call_openrouter(raw_title)
        clean_title = extracted.get("title") or None
        media_type = extracted.get("media_type")
        year = extracted.get("year")

        if not clean_title:
            log.warning("OpenRouter returned no title for item %d, marking failed", item_id)
            with get_db() as conn:
                update_item(conn, item_id, {"title": raw_title[:200], "status": "failed"})
            return

        log.info("TMDb search | title=%r media_type=%r year=%r", clean_title, media_type, year)
        match = await tmdb.search(clean_title, media_type, year=year)
        log.info("TMDb result | %s", match)

        if match:
            ai_tags = await _ai_tags(
                match["title"], match.get("overview", ""), match.get("genres", [])
            )
            update_data = {**match, "status": "to_watch", "ai_tags": ai_tags}
        else:
            update_data = {
                "title": clean_title,
                "media_type": media_type,
                "og_image": og_image,
                "status": "failed",
            }

        with get_db() as conn:
            update_item(conn, item_id, update_data)

    except Exception as e:
        log.exception("Enrichment failed for item %d: %s", item_id, e)
        with get_db() as conn:
            update_item(conn, item_id, {"status": "failed"})
