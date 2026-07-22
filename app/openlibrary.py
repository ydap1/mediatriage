import re
import httpx

_BASE = "https://openlibrary.org"
_COVER = "https://covers.openlibrary.org/b/id"
_SEARCH_FIELDS = "title,author_name,cover_i,first_publish_year,subject,key"


async def _get(client: httpx.AsyncClient, url: str, **params) -> dict | None:
    try:
        resp = await client.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def cover_url(cover_i: int | None, size: str = "M") -> str | None:
    return f"{_COVER}/{cover_i}-{size}.jpg" if cover_i else None


def _clean_description(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r'\n[-_]{3,}.*', '', text, flags=re.DOTALL)
    text = re.sub(r'\bSee also:.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'^\[.*?\]:.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^From \[.*?\]\[\d+\]:\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[([^\]]+)\]\[\d+\]', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def _parse_doc(doc: dict) -> dict:
    ol_key = doc.get("key", "").removeprefix("/works/")
    return {
        "title": doc.get("title", ""),
        "ol_key": ol_key,
        "media_type": "book",
        "section": "book",
        "genres": (doc.get("subject") or [])[:12],
        "authors": (doc.get("author_name") or [])[:5],
        "cover_i": doc.get("cover_i"),
        "overview": None,
        "release_year": doc.get("first_publish_year"),
    }


async def raw_search(title: str, author: str | None = None, limit: int = 10) -> list[dict]:
    """Search Open Library, returning parsed work-level candidates (unscored, undeduped)."""
    async with httpx.AsyncClient() as client:
        params: dict = {"q": title, "limit": limit, "fields": _SEARCH_FIELDS}
        if author:
            params["author"] = author
        data = await _get(client, f"{_BASE}/search.json", **params)
        docs = (data or {}).get("docs", [])
        return [_parse_doc(d) for d in docs]


async def fetch_description(ol_key: str) -> str | None:
    async with httpx.AsyncClient() as client:
        work = await _get(client, f"{_BASE}/works/{ol_key}.json")
        if not work:
            return None
        desc = work.get("description")
        if isinstance(desc, dict):
            desc = desc.get("value")
        return _clean_description(desc)


async def get_details(ol_key: str, stored: dict) -> dict:
    return stored
