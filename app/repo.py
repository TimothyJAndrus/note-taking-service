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
    # Bumps the version too, so the delete is its own entry in the revision history.
    conn.execute("UPDATE notes SET deleted_at = now(), version = version + 1, updated_at = now() "
                 "WHERE id = %s AND deleted_at IS NULL", (note_id,))


# ---- revisions (written by the notes_revision trigger; read-only here) ----------------

REVISION_COLS = "version, action, title, body, tags, visibility, changed_by, changed_at"


def list_revisions(conn: psycopg.Connection, note_id: UUID) -> list[dict]:
    return conn.execute(f"SELECT {REVISION_COLS} FROM note_revisions WHERE note_id = %s ORDER BY version DESC",
                        (note_id,)).fetchall()


def get_revision(conn: psycopg.Connection, note_id: UUID, version: int) -> dict | None:
    return conn.execute(f"SELECT {REVISION_COLS} FROM note_revisions WHERE note_id = %s AND version = %s",
                        (note_id, version)).fetchone()


# ---- attachments ---------------------------------------------------------------------

ATTACHMENT_COLS = "id, note_id, uploader_id, filename, content_type, size_bytes, status, created_at"


def create_attachment(conn: psycopg.Connection, attachment_id: UUID, note_id: UUID, team_id: UUID,
                      uploader_id: UUID, data: dict, s3_key: str) -> dict:
    return conn.execute(
        "INSERT INTO attachments (id, note_id, team_id, uploader_id, filename, content_type, size_bytes, s3_key) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {ATTACHMENT_COLS}",
        (attachment_id, note_id, team_id, uploader_id, data["filename"], data["content_type"],
         data["size_bytes"], s3_key)).fetchone()


def get_attachment(conn: psycopg.Connection, note_id: UUID, attachment_id: UUID) -> dict | None:
    return conn.execute(f"SELECT {ATTACHMENT_COLS}, s3_key FROM attachments WHERE id = %s AND note_id = %s",
                        (attachment_id, note_id)).fetchone()


def mark_attachment_ready(conn: psycopg.Connection, attachment_id: UUID) -> dict:
    return conn.execute(f"UPDATE attachments SET status = 'ready' WHERE id = %s RETURNING {ATTACHMENT_COLS}",
                        (attachment_id,)).fetchone()


def list_attachments(conn: psycopg.Connection, note_id: UUID) -> list[dict]:
    return conn.execute(f"SELECT {ATTACHMENT_COLS} FROM attachments WHERE note_id = %s AND status = 'ready' "
                        "ORDER BY created_at", (note_id,)).fetchall()


# ---- retrieval index for /ask (note_chunks) -------------------------------------------
# A chunk is only "live" while its version equals the note's current version and the note
# isn't deleted. Every read below joins on that, so stale chunks are invisible even before
# they're cleaned up.

def notes_needing_index(conn: psycopg.Connection, team_id: UUID | None, limit: int) -> list[dict]:
    """Live notes whose current version has no chunks yet. team_id=None means every team (CLI)."""
    where = "n.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM note_chunks c " \
            "WHERE c.note_id = n.id AND c.version = n.version)"
    args: list = []
    if team_id is not None:
        where = "n.team_id = %s AND " + where
        args.append(team_id)
    return conn.execute(f"SELECT n.id, n.team_id, n.title, n.body, n.version FROM notes n WHERE {where} "
                        "ORDER BY n.updated_at DESC LIMIT %s", [*args, limit]).fetchall()


def count_notes_needing_index(conn: psycopg.Connection, team_id: UUID) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM notes n WHERE n.team_id = %s AND n.deleted_at IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM note_chunks c WHERE c.note_id = n.id AND c.version = n.version)",
        (team_id,)).fetchone()["n"]


def replace_chunks(conn: psycopg.Connection, note: dict, chunks: list[tuple[str, str]], model: str) -> bool:
    """Store chunks [(content, pgvector literal)] for note['version'], dropping older versions' chunks.

    Returns False (and writes nothing) if the note moved on to a newer version meanwhile,
    so a slow indexer can never overwrite fresh chunks with stale ones.
    """
    with conn.transaction():
        current = conn.execute("SELECT version FROM notes WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                               (note["id"],)).fetchone()
        if current is None or current["version"] != note["version"]:
            return False
        conn.execute("DELETE FROM note_chunks WHERE note_id = %s", (note["id"],))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO note_chunks (note_id, team_id, version, chunk_index, title, content, embedding, model) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)",
                [(note["id"], note["team_id"], note["version"], i, note["title"], content, vec, model)
                 for i, (content, vec) in enumerate(chunks)])
    return True


def search_chunks(conn: psycopg.Connection, team_id: UUID, viewer_id: UUID, *, query: str,
                  query_vec: str, k: int, candidates: int = 40) -> list[dict]:
    """Hybrid retrieval: vector similarity and full-text rank, fused with reciprocal rank fusion.

    Only chunks the viewer may read: their team (also enforced by RLS), live notes,
    current versions, and private notes only for their author.
    """
    eligible = ("FROM note_chunks c JOIN notes n ON n.id = c.note_id AND n.version = c.version "
                "WHERE c.team_id = %(team)s AND n.team_id = %(team)s AND n.deleted_at IS NULL "
                "AND (n.visibility = 'team' OR n.author_id = %(viewer)s)")
    sql = f"""
        WITH vec AS (
            SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> %(v)s::vector) AS r
            {eligible}
            ORDER BY c.embedding <=> %(v)s::vector LIMIT %(cand)s),
        fts AS (
            SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.search, websearch_to_tsquery('english', %(q)s))
                                            DESC) AS r
            {eligible} AND c.search @@ websearch_to_tsquery('english', %(q)s)
            ORDER BY ts_rank_cd(c.search, websearch_to_tsquery('english', %(q)s)) DESC LIMIT %(cand)s),
        fused AS (
            SELECT id, sum(1.0 / (60 + r)) AS score FROM (SELECT * FROM vec UNION ALL SELECT * FROM fts) u
            GROUP BY id)
        SELECT c.note_id, c.version, c.chunk_index, c.title, c.content, f.score
        FROM fused f JOIN note_chunks c ON c.id = f.id
        ORDER BY f.score DESC, c.note_id, c.chunk_index LIMIT %(k)s"""
    with conn.transaction():
        # pgvector >= 0.8: keep scanning the HNSW graph until enough rows pass the filters.
        conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        return conn.execute(sql, {"team": team_id, "viewer": viewer_id, "v": query_vec, "q": query,
                                  "cand": candidates, "k": k}).fetchall()
