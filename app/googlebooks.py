import httpx

from .config import settings

_BASE = "https://www.googleapis.com/books/v1"


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict | None:
    if settings.google_books_api_key:
        params["key"] = settings.google_books_api_key
    try:
        resp = await client.get(f"{_BASE}{path}", params=params, timeout=8)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _poster(image_links: dict) -> str | None:
    url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
    if url:
        # Google returns http; force https and strip zoom cruft
        url = url.replace("http://", "https://")
        if "&zoom=" in url:
            url = url.split("&zoom=")[0]
    return url or None


def _year(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4:
        try:
            return int(date_str[:4])
        except ValueError:
            pass
    return None


def _parse_volume(vol: dict) -> dict:
    info = vol.get("volumeInfo", {})
    return {
        "title": info.get("title", ""),
        "ol_key": vol.get("id"),          # stored in ol_key column (generic book key)
        "media_type": "book",
        "section": "book",
        "genres": (info.get("categories") or [])[:6],
        "authors": (info.get("authors") or [])[:3],
        "poster_path": _poster(info.get("imageLinks") or {}),
        "overview": info.get("description"),
        "release_year": _year(info.get("publishedDate")),
    }


async def search(title: str, author: str | None = None) -> dict | None:
    """Search Google Books, return best match or None."""
    q = title
    if author:
        q += f" {author}"
    async with httpx.AsyncClient() as client:
        data = await _get(client, "/volumes", q=q, maxResults=5, printType="books", orderBy="relevance")
        items = (data or {}).get("items") or []
        return _parse_volume(items[0]) if items else None


async def search_multi(title: str, limit: int = 5) -> list[dict]:
    """Return top N candidates for the selection UI."""
    async with httpx.AsyncClient() as client:
        data = await _get(client, "/volumes", q=title, maxResults=limit, printType="books", orderBy="relevance")
        items = (data or {}).get("items") or []
        return [_parse_volume(v) for v in items[:limit]]


async def fetch_description(gb_key: str) -> str | None:
    """Fetch full description from volume detail endpoint."""
    async with httpx.AsyncClient() as client:
        data = await _get(client, f"/volumes/{gb_key}")
        if not data:
            return None
        return data.get("volumeInfo", {}).get("description")


async def get_details(gb_key: str, stored: dict) -> dict:
    """Return detail data for the modal — uses stored DB data, no extra HTTP request."""
    return stored
