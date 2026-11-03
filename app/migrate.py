"""Versioned SQL migrations: `python -m app.migrate`.

Plain numbered .sql files in migrations/, each applied once, in order, inside its own
transaction, and recorded in schema_migrations. An advisory lock means concurrent runs
(two deploys, or several instances starting at once) apply each file exactly once.
"""
import logging
import sys
from pathlib import Path

import psycopg

from app import config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 4242
log = logging.getLogger("migrate")


def pending(conn: psycopg.Connection) -> list[Path]:
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.stem not in done]


def migrate(url: str | None = None) -> list[str]:
    applied = []
    with psycopg.connect(url or config.database_url(), autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                         "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            for path in pending(conn):
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                log.info("applied migration %s", path.stem)
                applied.append(path.stem)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    done = migrate()
    print(f"applied {len(done)} migration(s): {', '.join(done) or 'none pending'}")
    sys.exit(0)
