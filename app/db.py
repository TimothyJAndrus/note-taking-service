"""PostgreSQL storage: connection pool and schema.

PostgreSQL gives us real concurrent writers, UUIDs, timestamptz, array columns
for tags, and built-in full-text search (tsvector + GIN).
"""
import os
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_URL = "postgresql://notes:notes@localhost:5433/notes"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,              -- stored lower-cased
    name          text NOT NULL,
    api_key_hash  text NOT NULL UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS teams (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memberships (
    team_id  uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role     text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships (user_id);

CREATE TABLE IF NOT EXISTS notes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id     uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    author_id   uuid NOT NULL REFERENCES users(id),
    title       text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    body        text NOT NULL DEFAULT '',
    tags        text[] NOT NULL DEFAULT '{}',
    visibility  text NOT NULL DEFAULT 'team' CHECK (visibility IN ('team', 'private')),
    version     integer NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,
    -- Search vector maintained by PostgreSQL itself; titles weigh more than body text.
    search      tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', title), 'A') ||
                    setweight(to_tsvector('english', body), 'B')) STORED
);
CREATE INDEX IF NOT EXISTS idx_notes_team_updated
    ON notes (team_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_notes_search ON notes USING gin (search);
CREATE INDEX IF NOT EXISTS idx_notes_tags   ON notes USING gin (tags);
"""

_pool: ConnectionPool | None = None


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def open_pool() -> None:
    """Open the pool and apply the schema. Called once at startup."""
    global _pool
    pool = ConnectionPool(
        database_url(),
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=True,
    )
    pool.wait(timeout=30)
    with pool.connection() as conn, conn.transaction():
        # Advisory lock: if several instances start at once, only one applies the schema.
        conn.execute("SELECT pg_advisory_xact_lock(4242)")
        conn.execute(SCHEMA)
    _pool = pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_conn() -> Iterator[psycopg.Connection]:
    """FastAPI dependency: borrow a pooled connection for the request, then return it."""
    assert _pool is not None, "database pool not open"
    with _pool.connection() as conn:
        yield conn
