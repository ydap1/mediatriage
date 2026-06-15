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


def _cover_url(cover_i, size="M") -> str | None:
    return f"{_COVER}/{cover_i}-{size}.jpg" if cover_i else None


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


async def search(title: str, author: str | None = None) -> dict | None:
    """Search Open Library by title (+ optional author). Returns best match or None."""
    async with httpx.AsyncClient() as client:
        params: dict = {"q": title, "limit": 5, "fields": _SEARCH_FIELDS}
        if author:
            params["author"] = author
        data = await _get(client, f"{_BASE}/search.json", **params)
        docs = (data or {}).get("docs", [])
        if not docs:
            return None

        result = _parse_doc(docs[0])

        # Fetch description from work endpoint
        if result["ol_key"]:
            work = await _get(client, f"{_BASE}/works/{result['ol_key']}.json")
            if work:
                desc = work.get("description")
                if isinstance(desc, dict):
                    desc = desc.get("value")
                result["overview"] = desc

        return result


async def get_details(ol_key: str, stored: dict) -> dict:
    """Return detail data for the modal, merging stored DB data with a fresh work fetch."""
    async with httpx.AsyncClient() as client:
        work = await _get(client, f"{_BASE}/works/{ol_key}.json")
        if not work:
            return stored

        desc = work.get("description")
        if isinstance(desc, dict):
            desc = desc.get("value")

        subjects = (work.get("subjects") or stored.get("genres") or [])[:8]

        return {
            **stored,
            "overview": desc or stored.get("overview"),
            "genres": subjects,
            "ol_key": ol_key,
        }
