"""All SQL lives here. Routes call these functions; nothing else touches the database."""
import base64
import sqlite3
import uuid
from datetime import datetime, timezone


def now() -> str:
    # Microsecond ISO-8601 UTC strings sort correctly as text.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return str(uuid.uuid4())


# ---- users & teams ----------------------------------------------------------

def create_user(conn: sqlite3.Connection, email: str, name: str, key_hash: str) -> dict:
    user = {"id": new_id(), "email": email.lower(), "name": name, "created_at": now()}
    with conn:
        conn.execute("INSERT INTO users (id, email, name, api_key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                     (user["id"], user["email"], name, key_hash, user["created_at"]))
    return user


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    row = conn.execute("SELECT id, email, name FROM users WHERE email = ?", (email.lower(),)).fetchone()
    return dict(row) if row else None


def create_team(conn: sqlite3.Connection, name: str, owner_id: str) -> dict:
    team = {"id": new_id(), "name": name, "role": "owner", "created_at": now()}
    with conn:  # team and its first owner are created atomically
        conn.execute("INSERT INTO teams (id, name, created_at) VALUES (?, ?, ?)",
                     (team["id"], name, team["created_at"]))
        conn.execute("INSERT INTO memberships (team_id, user_id, role) VALUES (?, ?, 'owner')",
                     (team["id"], owner_id))
    return team


def list_teams(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.id, t.name, m.role, t.created_at FROM teams t "
        "JOIN memberships m ON m.team_id = t.id WHERE m.user_id = ? ORDER BY t.name",
        (user_id,)).fetchall()
    return [dict(r) for r in rows]


def upsert_member(conn: sqlite3.Connection, team_id: str, user_id: str, role: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO memberships (team_id, user_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT (team_id, user_id) DO UPDATE SET role = excluded.role",
            (team_id, user_id, role))


def list_members(conn: sqlite3.Connection, team_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT u.id AS user_id, u.email, u.name, m.role FROM memberships m "
        "JOIN users u ON u.id = m.user_id WHERE m.team_id = ? ORDER BY u.email",
        (team_id,)).fetchall()
    return [dict(r) for r in rows]


# ---- notes ------------------------------------------------------------------

NOTE_COLS = ("n.id, n.team_id, n.author_id, n.title, n.body, n.visibility, "
             "n.version, n.created_at, n.updated_at")


def _attach_tags(conn: sqlite3.Connection, notes: list[dict]) -> list[dict]:
    if not notes:
        return notes
    ids = [n["id"] for n in notes]
    marks = ",".join("?" * len(ids))
    tags: dict[str, list[str]] = {i: [] for i in ids}
    for r in conn.execute(f"SELECT note_id, tag FROM note_tags WHERE note_id IN ({marks}) ORDER BY tag", ids):
        tags[r["note_id"]].append(r["tag"])
    for n in notes:
        n["tags"] = tags[n["id"]]
    return notes


def create_note(conn: sqlite3.Connection, team_id: str, author_id: str, data: dict) -> dict:
    note_id, ts = new_id(), now()
    with conn:
        conn.execute(
            "INSERT INTO notes (id, team_id, author_id, title, body, visibility, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (note_id, team_id, author_id, data["title"], data["body"], data["visibility"], ts, ts))
        conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                         [(note_id, t) for t in data["tags"]])
    note = get_note(conn, team_id, note_id, author_id)
    assert note is not None
    return note


def get_note(conn: sqlite3.Connection, team_id: str, note_id: str, viewer_id: str) -> dict | None:
    """Return the note only if this viewer may see it; private notes are author-only."""
    row = conn.execute(
        f"SELECT {NOTE_COLS} FROM notes n WHERE n.id = ? AND n.team_id = ? AND n.deleted_at IS NULL "
        "AND (n.visibility = 'team' OR n.author_id = ?)",
        (note_id, team_id, viewer_id)).fetchone()
    return _attach_tags(conn, [dict(row)])[0] if row else None


def _fts_query(q: str) -> str:
    # Quote every term so user input is treated as words, never as FTS syntax.
    return " ".join('"' + term.replace('"', '""') + '"' for term in q.split())


def encode_cursor(updated_at: str, note_id: str) -> str:
    return base64.urlsafe_b64encode(f"{updated_at}|{note_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str]:
    updated_at, note_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
    return updated_at, note_id


def list_notes(conn: sqlite3.Connection, team_id: str, viewer_id: str, *, q: str | None,
               tag: str | None, author_id: str | None, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    sql = [f"SELECT {NOTE_COLS} FROM notes n"]
    where = ["n.team_id = ?", "n.deleted_at IS NULL", "(n.visibility = 'team' OR n.author_id = ?)"]
    args: list = [team_id, viewer_id]
    if q:
        sql.append("JOIN notes_fts f ON f.rowid = n.rowid")
        where.append("notes_fts MATCH ?")
        args.append(_fts_query(q))
    if tag:
        where.append("EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = n.id AND t.tag = ?)")
        args.append(tag.strip().lower())
    if author_id:
        where.append("n.author_id = ?")
        args.append(author_id)
    if cursor:
        # Keyset pagination: stable under concurrent inserts, O(log n) per page.
        c_updated, c_id = decode_cursor(cursor)
        where.append("(n.updated_at < ? OR (n.updated_at = ? AND n.id < ?))")
        args += [c_updated, c_updated, c_id]
    sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY n.updated_at DESC, n.id DESC LIMIT ?")
    args.append(limit + 1)  # fetch one extra to know whether another page exists
    rows = [dict(r) for r in conn.execute(" ".join(sql), args).fetchall()]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1]["updated_at"], rows[-1]["id"])
    return _attach_tags(conn, rows), next_cursor


def update_note(conn: sqlite3.Connection, note_id: str, expected_version: int, changes: dict) -> bool:
    """Optimistic concurrency: the UPDATE only applies if nobody changed the note since the client read it."""
    fields = {k: v for k, v in changes.items() if k in ("title", "body", "visibility") and v is not None}
    sets = ", ".join(f"{k} = ?" for k in fields)
    sets = (sets + ", " if sets else "") + "version = version + 1, updated_at = ?"
    with conn:
        cur = conn.execute(f"UPDATE notes SET {sets} WHERE id = ? AND version = ? AND deleted_at IS NULL",
                           [*fields.values(), now(), note_id, expected_version])
        if cur.rowcount == 0:
            return False
        if changes.get("tags") is not None:
            conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            conn.executemany("INSERT INTO note_tags (note_id, tag) VALUES (?, ?)",
                             [(note_id, t) for t in changes["tags"]])
    return True


def soft_delete_note(conn: sqlite3.Connection, note_id: str) -> None:
    with conn:
        conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now(), note_id))
