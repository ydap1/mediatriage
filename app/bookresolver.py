"""Hybrid book lookup: Open Library for canonical title/author/year, Google Books
for description + cover fallback. See app/bookresolver.py design notes in the repo
plan history — OL's work-level search aggregates editions (so first_publish_year is
the real original publication year and author_name has no translator/editor noise),
while Google Books has broader coverage of recent titles and better blurbs.
"""
import asyncio
import difflib
import re

from . import googlebooks
from . import openlibrary

_JUNK_RE = re.compile(
    r'\b(summary(?:\s+(?:and|&)\s+analysis)?|study guide|sparknotes|cliffs?\s?notes|'
    r'workbook|teacher.?s guide|discussion guide|book club kit|unofficial guide|'
    r'companion(?:\s+guide)?|box(?:ed)?\s?set|omnibus)\b',
    re.IGNORECASE,
)
_GB_MATCH_THRESHOLD = 0.4


def _normalize(text: str | None) -> str:
    text = re.sub(r'[^a-z0-9 ]', ' ', (text or '').lower())
    return re.sub(r'\s+', ' ', text).strip()


def _title_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


_JUNK_AUTHOR_RE = re.compile(
    r'\b(book\s?summary|summary\s?guide|instaread|bookrags|everest\s?media|'
    r'ic0nic\s?knowledge|sparknotes|briefbooks|hyper\s?summary)\b',
    re.IGNORECASE,
)


def _is_junk(title: str, authors: list[str] | None = None) -> bool:
    if _JUNK_RE.search(title or ''):
        return True
    return any(_JUNK_AUTHOR_RE.search(a or '') for a in (authors or []))


def _clean_genres(subjects: list[str]) -> list[str]:
    # OL subjects sometimes embed commas in one entry (e.g. "Fiction, general") —
    # split those out first so no cleaned tag contains a comma. That also matters
    # downstream: the candidate-selection form transports genres as a comma-joined
    # hidden field, which would otherwise silently fragment a comma-bearing tag.
    out: list[str] = []
    seen: set[str] = set()
    for raw in subjects:
        for s in (raw or '').split(','):
            s = s.strip()
            if not s or len(s) > 40 or ':' in s or '=' in s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
            if len(out) == 6:
                return out
    return out


def _score_ol(doc: dict, query_title: str, query_author: str | None) -> float:
    if _is_junk(doc.get("title", ""), doc.get("authors")):
        return -1.0
    sim = _title_sim(doc.get("title", ""), query_title)
    author_bonus = 0.0
    if query_author:
        qa = _normalize(query_author)
        if any(qa in _normalize(a) or _normalize(a) in qa for a in doc.get("authors", [])):
            author_bonus = 0.2
    cover_bonus = 0.05 if doc.get("cover_i") else 0.0
    year_bonus = 0.03 if doc.get("release_year") else 0.0
    return sim + author_bonus + cover_bonus + year_bonus


def _best_gb_match(title: str, gb_items: list[dict]) -> dict | None:
    if not gb_items:
        return None
    best = max(gb_items, key=lambda v: _title_sim(v.get("title", ""), title))
    return best if _title_sim(best.get("title", ""), title) >= _GB_MATCH_THRESHOLD else None


def _dedup(candidates: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out = []
    for c in candidates:
        authors = c.get("authors") or []
        key = (_normalize(c.get("title")), _normalize(authors[0]) if authors else "")
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _merge(ol_doc: dict, gb_match: dict | None) -> dict:
    poster = openlibrary.cover_url(ol_doc.get("cover_i")) or (gb_match or {}).get("poster_path")
    overview = (gb_match or {}).get("overview") or ol_doc.get("overview")
    return {
        "title": ol_doc["title"],
        "ol_key": ol_doc.get("ol_key") or None,
        "media_type": "book",
        "section": "book",
        "genres": _clean_genres(ol_doc.get("genres", [])),
        "authors": (ol_doc.get("authors") or [])[:3],
        "poster_path": poster,
        "overview": overview,
        "release_year": ol_doc.get("release_year"),
    }


def _finalize_gb(v: dict) -> dict:
    return {
        **v,
        "genres": _clean_genres(v.get("genres", [])),
        "authors": (v.get("authors") or [])[:3],
    }


async def _resolve(title: str, author: str | None, limit: int) -> list[dict]:
    query = f"{title} {author}".strip() if author else title
    ol_docs, gb_items = await asyncio.gather(
        openlibrary.raw_search(title, author=author, limit=limit * 3),
        googlebooks.raw_search(query, limit=limit * 2),
    )

    if not ol_docs:
        # Coverage fallback for titles Open Library hasn't indexed (e.g. very recent
        # releases) — Google Books only, using its own thumbnail directly rather
        # than the old broken ISBN-cover lookup.
        clean = [v for v in gb_items if not _is_junk(v.get("title", ""), v.get("authors"))]
        pool = clean or gb_items
        pool.sort(key=lambda v: _title_sim(v.get("title", ""), title), reverse=True)
        return _dedup([_finalize_gb(v) for v in pool])[:limit]

    scored = [(d, _score_ol(d, title, author)) for d in ol_docs]
    scored = [(d, s) for d, s in scored if s >= 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    merged = [_merge(doc, _best_gb_match(doc["title"], gb_items)) for doc, _ in scored]
    return _dedup(merged)[:limit]


async def search_multi(title: str, author: str | None = None, limit: int = 5) -> list[dict]:
    return await _resolve(title, author, limit)


async def search(title: str, author: str | None = None) -> dict | None:
    results = await _resolve(title, author, limit=1)
    return results[0] if results else None
