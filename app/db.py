import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA_VERSION = 4

_SORT_MAP = {
    "date_added": "i.date_added DESC",
    "title": "i.title ASC",
    "release_year": "i.release_year DESC",
    "status": "CASE i.status WHEN 'to_watch' THEN 0 WHEN 'watched' THEN 1 ELSE 2 END, i.date_added DESC",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            section      TEXT NOT NULL DEFAULT 'film',
            tmdb_id      INTEGER,
            ol_key       TEXT,
            media_type   TEXT,
            genres       TEXT DEFAULT '[]',
            authors      TEXT DEFAULT '[]',
            directors    TEXT DEFAULT '[]',
            poster_path  TEXT,
            og_image     TEXT,
            overview     TEXT,
            release_year INTEGER,
            source_url   TEXT,
            status       TEXT NOT NULL DEFAULT 'to_watch',
            date_added   TEXT NOT NULL DEFAULT (datetime('now')),
            ai_tags      TEXT DEFAULT '[]'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts
            USING fts5(title, overview, content=items, content_rowid=id);

        CREATE TRIGGER IF NOT EXISTS items_fts_insert
            AFTER INSERT ON items BEGIN
                INSERT INTO items_fts(rowid, title, overview)
                VALUES (new.id, new.title, new.overview);
            END;

        CREATE TRIGGER IF NOT EXISTS items_fts_update
            AFTER UPDATE ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, title, overview)
                VALUES ('delete', old.id, old.title, old.overview);
                INSERT INTO items_fts(rowid, title, overview)
                VALUES (new.id, new.title, new.overview);
            END;

        CREATE TRIGGER IF NOT EXISTS items_fts_delete
            AFTER DELETE ON items BEGIN
                INSERT INTO items_fts(items_fts, rowid, title, overview)
                VALUES ('delete', old.id, old.title, old.overview);
            END;
    """)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE items ADD COLUMN section TEXT NOT NULL DEFAULT 'film'")
    conn.execute("ALTER TABLE items ADD COLUMN ol_key TEXT")
    conn.execute("ALTER TABLE items ADD COLUMN authors TEXT DEFAULT '[]'")


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE items ADD COLUMN watch_providers TEXT DEFAULT '[]'")


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE items ADD COLUMN directors TEXT DEFAULT '[]'")


def init_db() -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            _create_schema(conn)
            _migrate_v2_to_v3(conn)
            _migrate_v3_to_v4(conn)
        elif version == 1:
            _migrate_v1_to_v2(conn)
            _migrate_v2_to_v3(conn)
            _migrate_v3_to_v4(conn)
        elif version == 2:
            _migrate_v2_to_v3(conn)
            _migrate_v3_to_v4(conn)
        elif version == 3:
            _migrate_v3_to_v4(conn)
        elif version == 4:
            pass
        else:
            raise RuntimeError(
                f"DB schema version {version} does not match expected {SCHEMA_VERSION}. "
                "Run migrations before starting the app."
            )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["genres"] = json.loads(d.get("genres") or "[]")
    d["ai_tags"] = json.loads(d.get("ai_tags") or "[]")
    d["authors"] = json.loads(d.get("authors") or "[]")
    d["directors"] = json.loads(d.get("directors") or "[]")
    d["watch_providers"] = json.loads(d.get("watch_providers") or "[]")
    return d


def insert_item(conn: sqlite3.Connection, data: dict) -> int:
    cur = conn.execute(
        """INSERT INTO items
           (title, section, tmdb_id, ol_key, media_type, genres, authors, directors,
            poster_path, og_image, overview, release_year, source_url, status, ai_tags, watch_providers)
           VALUES
           (:title, :section, :tmdb_id, :ol_key, :media_type, :genres, :authors, :directors,
            :poster_path, :og_image, :overview, :release_year, :source_url, :status, :ai_tags, :watch_providers)""",
        {
            "title": data.get("title", ""),
            "section": data.get("section", "film"),
            "tmdb_id": data.get("tmdb_id"),
            "ol_key": data.get("ol_key"),
            "media_type": data.get("media_type"),
            "genres": json.dumps(data.get("genres", [])),
            "authors": json.dumps(data.get("authors", [])),
            "directors": json.dumps(data.get("directors", [])),
            "poster_path": data.get("poster_path"),
            "og_image": data.get("og_image"),
            "overview": data.get("overview"),
            "release_year": data.get("release_year"),
            "source_url": data.get("source_url"),
            "status": data.get("status", "to_watch"),
            "ai_tags": json.dumps(data.get("ai_tags", [])),
            "watch_providers": json.dumps(data.get("watch_providers", [])),
        },
    )
    return cur.lastrowid


def upsert_item(conn: sqlite3.Connection, data: dict) -> tuple[int, bool]:
    """Insert or update by tmdb_id (films) or ol_key (books). Returns (id, is_duplicate)."""
    tmdb_id = data.get("tmdb_id")
    ol_key = data.get("ol_key")

    existing = None
    if tmdb_id:
        existing = conn.execute(
            "SELECT id, status FROM items WHERE tmdb_id = ?", (tmdb_id,)
        ).fetchone()
    elif ol_key:
        existing = conn.execute(
            "SELECT id, status FROM items WHERE ol_key = ?", (ol_key,)
        ).fetchone()

    if existing:
        item_id = existing["id"]
        update_item(conn, item_id, {**data, "status": existing["status"]})
        conn.execute("UPDATE items SET date_added = datetime('now') WHERE id = ?", (item_id,))
        return item_id, True

    return insert_item(conn, data), False


def update_item(conn: sqlite3.Connection, item_id: int, data: dict) -> None:
    fields = {k: v for k, v in data.items() if k != "id"}
    for list_field in ("genres", "ai_tags", "authors", "directors", "watch_providers"):
        if list_field in fields and isinstance(fields[list_field], list):
            fields[list_field] = json.dumps(fields[list_field])
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["_id"] = item_id
    conn.execute(f"UPDATE items SET {set_clause} WHERE id = :_id", fields)


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return row_to_dict(row) if row else None


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


def delete_all_items(conn: sqlite3.Connection, section: str | None = None) -> None:
    if section:
        conn.execute("DELETE FROM items WHERE section = ?", (section,))
    else:
        conn.execute("DELETE FROM items")


def _build_where(
    params: list,
    section: str | None,
    status: str | None,
    media_type: str | None,
    genre: str | None,
    query: str | None,
    conn: sqlite3.Connection,
) -> tuple[str, list, bool]:
    where: list[str] = []
    if section:
        where.append("i.section = ?")
        params.append(section)
    if status:
        where.append("i.status = ?")
        params.append(status)
    if media_type:
        where.append("i.media_type = ?")
        params.append(media_type)
    if genre:
        where.append("i.genres LIKE ?")
        params.append(f'%"{genre}"%')
    if query:
        fts_ids = [
            r[0]
            for r in conn.execute(
                "SELECT rowid FROM items_fts WHERE items_fts MATCH ? ORDER BY rank",
                (query,),
            ).fetchall()
        ]
        if not fts_ids:
            return "", params, True
        placeholders = ",".join("?" * len(fts_ids))
        where.append(f"i.id IN ({placeholders})")
        params.extend(fts_ids)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params, False


def list_items(
    conn: sqlite3.Connection,
    section: str | None = None,
    status: str | None = None,
    media_type: str | None = None,
    genre: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 24,
    sort_by: str = "date_added",
) -> tuple[list[dict], int]:
    params: list[Any] = []
    where_sql, params, empty = _build_where(params, section, status, media_type, genre, query, conn)
    if empty:
        return [], 0
    total = conn.execute(f"SELECT COUNT(*) FROM items i {where_sql}", params).fetchone()[0]
    order = _SORT_MAP.get(sort_by, _SORT_MAP["date_added"])
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT i.* FROM items i {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return [row_to_dict(r) for r in rows], total


def get_all_items(
    conn: sqlite3.Connection,
    section: str | None = None,
    status: str | None = None,
    media_type: str | None = None,
    genre: str | None = None,
    query: str | None = None,
) -> list[dict]:
    params: list[Any] = []
    where_sql, params, empty = _build_where(params, section, status, media_type, genre, query, conn)
    if empty:
        return []
    rows = conn.execute(
        f"SELECT i.* FROM items i {where_sql} ORDER BY i.date_added DESC", params
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_all_genres(conn: sqlite3.Connection, section: str | None = None) -> list[str]:
    if section:
        rows = conn.execute(
            "SELECT genres FROM items WHERE genres != '[]' AND section = ?", (section,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT genres FROM items WHERE genres != '[]'").fetchall()
    genres: set[str] = set()
    for row in rows:
        genres.update(json.loads(row[0]))
    return sorted(genres)
