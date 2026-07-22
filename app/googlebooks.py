import re
import httpx

from .config import settings

_BASE = "https://www.googleapis.com/books/v1"


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text or None


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict | None:
    if settings.google_books_api_key:
        params["key"] = settings.google_books_api_key
    try:
        resp = await client.get(f"{_BASE}{path}", params=params, timeout=8)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _year(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4:
        try:
            return int(date_str[:4])
        except ValueError:
            pass
    return None


def _parse_volume(vol: dict) -> dict:
    info = vol.get("volumeInfo", {})
    links = info.get("imageLinks") or {}
    thumb = links.get("thumbnail") or links.get("smallThumbnail")
    if thumb:
        thumb = thumb.replace("http://", "https://")
    return {
        "title": info.get("title", ""),
        "ol_key": vol.get("id"),
        "media_type": "book",
        "section": "book",
        "genres": (info.get("categories") or [])[:12],
        "authors": (info.get("authors") or [])[:5],
        "poster_path": thumb,
        "overview": _strip_html(info.get("description")),
        "release_year": _year(info.get("publishedDate")),
    }


async def raw_search(query: str, limit: int = 10) -> list[dict]:
    """Search Google Books, returning parsed volume-level candidates (unscored, undeduped)."""
    async with httpx.AsyncClient() as client:
        data = await _get(client, "/volumes", q=query, maxResults=limit, printType="books", orderBy="relevance")
        items = (data or {}).get("items") or []
        return [_parse_volume(v) for v in items]


async def fetch_description(gb_key: str) -> str | None:
    async with httpx.AsyncClient() as client:
        data = await _get(client, f"/volumes/{gb_key}")
        if not data:
            return None
        return _strip_html(data.get("volumeInfo", {}).get("description"))


async def get_details(gb_key: str, stored: dict) -> dict:
    return stored
