import re
import difflib
import httpx

from .config import settings

_BASE = "https://www.googleapis.com/books/v1"
_OL_COVER = "https://covers.openlibrary.org/b/isbn"


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


def _isbn(info: dict) -> str | None:
    for id_type in ("ISBN_13", "ISBN_10"):
        for entry in (info.get("industryIdentifiers") or []):
            if entry.get("type") == id_type:
                return entry["identifier"]
    return None


def _poster(info: dict) -> str | None:
    """Use OL cover by ISBN (clean 404 on miss) before GB thumbnail (ugly placeholder on miss)."""
    isbn = _isbn(info)
    if isbn:
        # default=false → 404 instead of tiny stub image, so onerror works
        return f"{_OL_COVER}/{isbn}-M.jpg?default=false"
    links = info.get("imageLinks") or {}
    url = links.get("thumbnail") or links.get("smallThumbnail")
    if url:
        url = url.replace("http://", "https://")
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
        "ol_key": vol.get("id"),
        "media_type": "book",
        "section": "book",
        "genres": (info.get("categories") or [])[:6],
        "authors": (info.get("authors") or [])[:3],
        "poster_path": _poster(info),
        "overview": _strip_html(info.get("description")),
        "release_year": _year(info.get("publishedDate")),
    }


def _pick_best(items: list[dict], query: str) -> dict | None:
    """Prefer close title match and earlier publication date over relevance rank."""
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    q = query.lower().strip()

    def score(vol: dict) -> float:
        info = vol.get("volumeInfo", {})
        title = (info.get("title") or "").lower()

        # Penalise "Title by Author Name" reprint pattern
        penalty = 0.6 if re.search(r'\bby\s+\w', title) else 1.0
        sim = difflib.SequenceMatcher(None, title, q).ratio() * penalty

        # Small bonus for earlier (more canonical) editions
        year = _year(info.get("publishedDate")) or 9999
        year_bonus = max(0, (2020 - year)) / 2000

        return sim + year_bonus

    return max(items, key=score)


async def search(title: str, author: str | None = None) -> dict | None:
    q = f"{title} {author}".strip() if author else title
    async with httpx.AsyncClient() as client:
        data = await _get(client, "/volumes", q=q, maxResults=10, printType="books", orderBy="relevance")
        items = (data or {}).get("items") or []
        best = _pick_best(items, title)
        return _parse_volume(best) if best else None


async def search_multi(title: str, limit: int = 5) -> list[dict]:
    async with httpx.AsyncClient() as client:
        data = await _get(client, "/volumes", q=title, maxResults=limit * 2, printType="books", orderBy="relevance")
        items = (data or {}).get("items") or []
        # Filter reprint-pattern titles when cleaner results are available
        clean = [v for v in items if not re.search(r'\bby\s+\w', (v.get("volumeInfo", {}).get("title") or "").lower())]
        pool = clean if clean else items
        return [_parse_volume(v) for v in pool[:limit]]


async def fetch_description(gb_key: str) -> str | None:
    async with httpx.AsyncClient() as client:
        data = await _get(client, f"/volumes/{gb_key}")
        if not data:
            return None
        return _strip_html(data.get("volumeInfo", {}).get("description"))


async def get_details(gb_key: str, stored: dict) -> dict:
    return stored
