import asyncio

import httpx

from .config import settings

_BASE = "https://api.themoviedb.org/3"
_GENRE_CACHE: dict[str, dict[int, str]] = {}


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict | None:
    try:
        resp = await client.get(
            f"{_BASE}{path}",
            params={"api_key": settings.tmdb_api_key, **params},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


async def _load_genres(client: httpx.AsyncClient) -> None:
    for kind in ("movie", "tv"):
        if kind not in _GENRE_CACHE:
            data = await _get(client, f"/genre/{kind}/list")
            if data:
                _GENRE_CACHE[kind] = {g["id"]: g["name"] for g in data.get("genres", [])}


def _resolve_genres(genre_ids: list[int], media_type: str) -> list[str]:
    cache = _GENRE_CACHE.get(media_type, {})
    return [cache[gid] for gid in genre_ids if gid in cache]


def _has_cyrillic(text: str) -> bool:
    return any('Ѐ' <= c <= 'ӿ' for c in text)


def _extract_year(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4:
        try:
            return int(date_str[:4])
        except ValueError:
            return None
    return None


def _parse_result(result: dict) -> dict:
    media_type = result.get("media_type", "movie")
    title = result.get("title") or result.get("name") or ""
    date = result.get("release_date") or result.get("first_air_date")
    genres = _resolve_genres(result.get("genre_ids", []), media_type)
    return {
        "title": title,
        "tmdb_id": result.get("id"),
        "media_type": media_type,
        "genres": genres,
        "poster_path": result.get("poster_path"),
        "overview": result.get("overview"),
        "release_year": _extract_year(date),
    }


async def _fetch_directors(client: httpx.AsyncClient, tmdb_id: int, media_type: str) -> list[str]:
    if media_type == "movie":
        data = await _get(client, f"/movie/{tmdb_id}/credits")
        return [c["name"] for c in (data or {}).get("crew", []) if c.get("job") == "Director"]
    else:
        data = await _get(client, f"/tv/{tmdb_id}")
        return [c["name"] for c in (data or {}).get("created_by", [])]


async def _fetch_watch_providers(client: httpx.AsyncClient, tmdb_id: int, media_type: str) -> list[dict]:
    data = await _get(client, f"/{media_type}/{tmdb_id}/watch/providers")
    region_data = (data or {}).get("results", {}).get(settings.watch_region, {})
    providers = []
    seen = set()
    for ptype in ("flatrate", "rent", "buy"):
        for p in region_data.get(ptype, []):
            pid = p["provider_id"]
            if pid not in seen:
                seen.add(pid)
                providers.append({
                    "provider_id": pid,
                    "provider_name": p["provider_name"],
                    "logo_path": p.get("logo_path", ""),
                    "type": ptype,
                })
    return providers


async def get_details(tmdb_id: int, media_type: str) -> dict | None:
    """Fetch full details + credits for a known TMDb item."""
    async with httpx.AsyncClient() as client:
        await _load_genres(client)
        data = await _get(client, f"/{media_type}/{tmdb_id}", append_to_response="credits")
        if not data:
            return None

        title = data.get("title") or data.get("name") or ""
        genres = [g["name"] for g in data.get("genres", [])]
        date = data.get("release_date") or data.get("first_air_date")
        credits = data.get("credits", {})

        cast = [
            {
                "name": c["name"],
                "character": c.get("character", ""),
                "profile_path": c.get("profile_path"),
            }
            for c in sorted(credits.get("cast", []), key=lambda x: x.get("order", 999))[:10]
        ]

        if media_type == "movie":
            directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
        else:
            directors = [c["name"] for c in data.get("created_by", [])]

        vote = data.get("vote_average")
        return {
            "title": title,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "genres": genres,
            "poster_path": data.get("poster_path"),
            "overview": data.get("overview"),
            "release_year": _extract_year(date),
            "tagline": data.get("tagline"),
            "vote_average": round(vote, 1) if vote else None,
            "runtime": data.get("runtime"),
            "seasons": data.get("number_of_seasons"),
            "episodes": data.get("number_of_episodes"),
            "cast": cast,
            "directors": directors,
            "imdb_id": data.get("imdb_id"),
        }


async def search(title: str, media_type: str | None = None, year: int | None = None) -> dict | None:
    """Return best TMDb match for a title, or None if no confident match."""
    async with httpx.AsyncClient() as client:
        await _load_genres(client)

        if media_type in ("movie", "tv"):
            lang = {"language": "ru"} if _has_cyrillic(title) else {}
            extra = dict(lang)
            if year:
                extra.update({"primary_release_year": year} if media_type == "movie" else {"first_air_date_year": year})
            data = await _get(client, f"/search/{media_type}", query=title, **extra)
            results = (data or {}).get("results", [])
            if not results and year:
                data = await _get(client, f"/search/{media_type}", query=title, **lang)
                results = (data or {}).get("results", [])
            if results:
                r = results[0]
                r["media_type"] = media_type
                parsed = _parse_result(r)
                parsed["directors"] = await _fetch_directors(client, parsed["tmdb_id"], media_type)
                return parsed

        data = await _get(client, "/search/multi", query=title)
        results = [
            r for r in (data or {}).get("results", [])
            if r.get("media_type") in ("movie", "tv")
        ]
        if not results:
            return None

        best = results[0]
        parsed = _parse_result(best)
        parsed["directors"] = await _fetch_directors(client, parsed["tmdb_id"], parsed["media_type"])
        return parsed


async def search_one(title: str, media_type: str | None = None, year: int | None = None) -> dict | None:
    """Best TMDb match without fetching directors — for building candidate lists."""
    async with httpx.AsyncClient() as client:
        await _load_genres(client)
        lang = {"language": "ru"} if _has_cyrillic(title) else {}

        if media_type in ("movie", "tv"):
            extra = dict(lang)
            if year:
                extra.update({"primary_release_year": year} if media_type == "movie" else {"first_air_date_year": year})
            data = await _get(client, f"/search/{media_type}", query=title, **extra)
            results = (data or {}).get("results", [])
            if not results and year:
                data = await _get(client, f"/search/{media_type}", query=title, **lang)
                results = (data or {}).get("results", [])
            if results:
                r = results[0]
                r["media_type"] = media_type
                return _parse_result(r)

        data = await _get(client, "/search/multi", query=title)
        results = [r for r in (data or {}).get("results", []) if r.get("media_type") in ("movie", "tv")]
        return _parse_result(results[0]) if results else None


async def search_multi(title: str, limit: int = 5) -> list[dict]:
    """Return top N candidates for the selection UI."""
    async with httpx.AsyncClient() as client:
        await _load_genres(client)
        data = await _get(client, "/search/multi", query=title)
        results = [
            _parse_result(r) for r in (data or {}).get("results", [])
            if r.get("media_type") in ("movie", "tv")
        ]
        return results[:limit]
