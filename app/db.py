"""PostgreSQL: connection pool and per-request identity for row-level security.

Every pooled connection runs as the restricted role notes_app (see migrations/0002), so
PostgreSQL's row-level security applies. Each request records who is calling with
set_actor(); the pool clears it when the connection is returned.
"""
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import config
from app.migrate import migrate

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("SET ROLE notes_app")


def _reset(conn: psycopg.Connection) -> None:
    conn.execute("SELECT set_config('app.user_id', '', false)")


def set_actor(conn: psycopg.Connection, user_id) -> None:
    """Tell PostgreSQL who this request is for (used by RLS policies and the audit trigger)."""
    conn.execute("SELECT set_config('app.user_id', %s, false)", (str(user_id),))


def open_pool() -> None:
    global _pool
    if config.RUN_MIGRATIONS_ON_STARTUP:
        migrate()
    pool = ConnectionPool(
        config.database_url(),
        min_size=1,
        max_size=config.DB_POOL_SIZE,
        kwargs={"row_factory": dict_row, "autocommit": True},
        configure=_configure,
        reset=_reset,
        open=True,
    )
    pool.wait(timeout=30)
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
