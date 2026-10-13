"""All SQL lives here. Routes call these functions; nothing else touches the database."""
import base64
from datetime import datetime
from uuid import UUID

import psycopg

NOTE_COLS = ("n.id, n.team_id, n.author_id, n.title, n.body, n.tags, n.visibility, "
             "n.version, n.created_at, n.updated_at")


class DuplicateEmail(Exception):
    pass


# ---- users & teams ----------------------------------------------------------

def create_user(conn: psycopg.Connection, email: str, name: str, key_hash: str) -> dict:
    try:
        return conn.execute(
            "INSERT INTO users (email, name, api_key_hash) VALUES (%s, %s, %s) "
            "RETURNING id, email, name, created_at",
            (email.lower(), name, key_hash)).fetchone()
    except psycopg.errors.UniqueViolation:
        # The UNIQUE constraint is the source of truth, so two simultaneous sign-ups can't both win.
        raise DuplicateEmail from None


def get_user_by_email(conn: psycopg.Connection, email: str) -> dict | None:
    return conn.execute("SELECT id, email, name FROM users WHERE email = %s", (email.lower(),)).fetchone()


def create_team(conn: psycopg.Connection, name: str, owner_id: UUID) -> dict:
    with conn.transaction():  # the team and its first owner are created atomically
        team = conn.execute("INSERT INTO teams (name) VALUES (%s) RETURNING id, name, created_at",
                            (name,)).fetchone()
        conn.execute("INSERT INTO memberships (team_id, user_id, role) VALUES (%s, %s, 'owner')",
                     (team["id"], owner_id))
    return {**team, "role": "owner"}


def list_teams(conn: psycopg.Connection, user_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT t.id, t.name, m.role, t.created_at FROM teams t "
        "JOIN memberships m ON m.team_id = t.id WHERE m.user_id = %s ORDER BY t.name",
        (user_id,)).fetchall()


def upsert_member(conn: psycopg.Connection, team_id: UUID, user_id: UUID, role: str) -> None:
    conn.execute(
        "INSERT INTO memberships (team_id, user_id, role) VALUES (%s, %s, %s) "
        "ON CONFLICT (team_id, user_id) DO UPDATE SET role = EXCLUDED.role",
        (team_id, user_id, role))


def list_members(conn: psycopg.Connection, team_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT u.id AS user_id, u.email, u.name, m.role FROM memberships m "
        "JOIN users u ON u.id = m.user_id WHERE m.team_id = %s ORDER BY u.email",
        (team_id,)).fetchall()


# ---- notes ------------------------------------------------------------------

def create_note(conn: psycopg.Connection, team_id: UUID, author_id: UUID, data: dict) -> dict:
    return conn.execute(
        "INSERT INTO notes AS n (team_id, author_id, title, body, tags, visibility) "
        f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {NOTE_COLS}",
        (team_id, author_id, data["title"], data["body"], data["tags"], data["visibility"])).fetchone()


def get_note(conn: psycopg.Connection, team_id: UUID, note_id: UUID, viewer_id: UUID) -> dict | None:
    """Return the note only if this viewer may see it; private notes are author-only."""
    return conn.execute(
        f"SELECT {NOTE_COLS} FROM notes n WHERE n.id = %s AND n.team_id = %s AND n.deleted_at IS NULL "
        "AND (n.visibility = 'team' OR n.author_id = %s)",
        (note_id, team_id, viewer_id)).fetchone()


def encode_cursor(updated_at: datetime, note_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{updated_at.isoformat()}|{note_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    updated_at, note_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
    return datetime.fromisoformat(updated_at), UUID(note_id)


def list_notes(conn: psycopg.Connection, team_id: UUID, viewer_id: UUID, *, q: str | None,
               tag: str | None, author_id: UUID | None, limit: int,
               cursor: str | None) -> tuple[list[dict], str | None]:
    where = ["n.team_id = %s", "n.deleted_at IS NULL", "(n.visibility = 'team' OR n.author_id = %s)"]
    args: list = [team_id, viewer_id]
    if q:
        # websearch_to_tsquery accepts any user text ("phrases", -exclusions, or) and never raises.
        where.append("n.search @@ websearch_to_tsquery('english', %s)")
        args.append(q)
    if tag:
        where.append("n.tags @> ARRAY[%s]::text[]")  # served by the GIN index on tags
        args.append(tag.strip().lower())
    if author_id:
        where.append("n.author_id = %s")
        args.append(author_id)
    if cursor:
        # Keyset pagination: stable under concurrent inserts, an index seek at any depth.
        c_updated, c_id = decode_cursor(cursor)
        where.append("(n.updated_at, n.id) < (%s, %s)")
        args += [c_updated, c_id]
    sql = (f"SELECT {NOTE_COLS} FROM notes n WHERE {' AND '.join(where)} "
           "ORDER BY n.updated_at DESC, n.id DESC LIMIT %s")
    rows = conn.execute(sql, [*args, limit + 1]).fetchall()  # one extra row tells us if there's a next page
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1]["updated_at"], rows[-1]["id"])
    return rows, next_cursor


def update_note(conn: psycopg.Connection, note_id: UUID, expected_version: int, changes: dict) -> bool:
    """Optimistic concurrency: the UPDATE only applies if nobody changed the note since the client read it.

    A single statement, so it's atomic without an explicit transaction.
    """
    fields = {k: v for k, v in changes.items() if k in ("title", "body", "visibility", "tags") and v is not None}
    sets = [f"{k} = %s" for k in fields] + ["version = version + 1", "updated_at = now()"]
    cur = conn.execute(
        f"UPDATE notes SET {', '.join(sets)} WHERE id = %s AND version = %s AND deleted_at IS NULL",
        [*fields.values(), note_id, expected_version])
    return cur.rowcount == 1


def soft_delete_note(conn: psycopg.Connection, note_id: UUID) -> None:
    conn.execute("UPDATE notes SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL", (note_id,))
