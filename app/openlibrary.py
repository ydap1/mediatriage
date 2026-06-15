import re
import httpx

_BASE = "https://openlibrary.org"
_COVER = "https://covers.openlibrary.org/b/id"
_SEARCH_FIELDS = "title,author_name,cover_i,first_publish_year,subject,key,number_of_pages_median"


async def _get(client: httpx.AsyncClient, url: str, **params) -> dict | None:
    try:
        resp = await client.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _cover_url(cover_i, size="M") -> str | None:
    return f"{_COVER}/{cover_i}-{size}.jpg" if cover_i else None


def _clean_description(text: str | None) -> str | None:
    if not text:
        return None
    # Strip "See also:" and everything after (including preceding divider)
    text = re.sub(r'\n[-_]{3,}.*', '', text, flags=re.DOTALL)
    text = re.sub(r'\bSee also:.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Strip reference definition lines: [1]: https://...
    text = re.sub(r'^\[.*?\]:.*$', '', text, flags=re.MULTILINE)
    # Strip "From [source][n]: " prefix
    text = re.sub(r'^From \[.*?\]\[\d+\]:\s*', '', text, flags=re.IGNORECASE)
    # Replace markdown reference links [text][n] → text
    text = re.sub(r'\[([^\]]+)\]\[\d+\]', r'\1', text)
    # Replace inline markdown links [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove bare numerical refs [1]
    text = re.sub(r'\[\d+\]', '', text)
    # Collapse excess whitespace
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def _parse_doc(doc: dict) -> dict:
    ol_key = doc.get("key", "").removeprefix("/works/")
    return {
        "title": doc.get("title", ""),
        "ol_key": ol_key,
        "media_type": "book",
        "section": "book",
        "genres": (doc.get("subject") or [])[:6],
        "authors": (doc.get("author_name") or [])[:3],
        "poster_path": _cover_url(doc.get("cover_i")),
        "overview": None,
        "release_year": doc.get("first_publish_year"),
    }


def _best_doc(docs: list[dict], pool: int = 8) -> dict | None:
    """Among the top `pool` docs, prefer ones with a cover image."""
    if not docs:
        return None
    top = docs[:pool]
    return next((d for d in top if d.get("cover_i")), top[0])


async def search(title: str, author: str | None = None) -> dict | None:
    """Search Open Library. Prefers results with covers. Returns best match or None."""
    async with httpx.AsyncClient() as client:
        params: dict = {"q": title, "limit": 10, "fields": _SEARCH_FIELDS}
        if author:
            params["author"] = author
        data = await _get(client, f"{_BASE}/search.json", **params)
        docs = (data or {}).get("docs", [])
        doc = _best_doc(docs)
        if not doc:
            return None

        result = _parse_doc(doc)

        if result["ol_key"]:
            work = await _get(client, f"{_BASE}/works/{result['ol_key']}.json")
            if work:
                desc = work.get("description")
                if isinstance(desc, dict):
                    desc = desc.get("value")
                result["overview"] = _clean_description(desc)

        return result


async def search_multi(title: str, limit: int = 5) -> list[dict]:
    """Return top candidates for the selection UI. Covers-first ordering."""
    async with httpx.AsyncClient() as client:
        params: dict = {"q": title, "limit": limit * 3, "fields": _SEARCH_FIELDS}
        data = await _get(client, f"{_BASE}/search.json", **params)
        docs = (data or {}).get("docs", [])
        # Put docs with covers first
        with_cover = [d for d in docs if d.get("cover_i")]
        without_cover = [d for d in docs if not d.get("cover_i")]
        ordered = (with_cover + without_cover)[:limit]
        return [_parse_doc(d) for d in ordered]


async def fetch_description(ol_key: str) -> str | None:
    """Fetch and clean the description for a work."""
    async with httpx.AsyncClient() as client:
        work = await _get(client, f"{_BASE}/works/{ol_key}.json")
        if not work:
            return None
        desc = work.get("description")
        if isinstance(desc, dict):
            desc = desc.get("value")
        return _clean_description(desc)


async def get_details(ol_key: str, stored: dict) -> dict:
    """Return detail data for the modal — uses stored DB data, no extra HTTP request."""
    return stored
