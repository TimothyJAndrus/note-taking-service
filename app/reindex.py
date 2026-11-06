"""Backfill the /ask retrieval index: `python -m app.reindex [--limit N]`.

Embeds every live note whose current version isn't indexed yet (all teams). Runs as the
connecting (owner) role, like migrations, so it isn't limited to one user's teams. Safe to
re-run and to run alongside the API: already-indexed versions are skipped, and a note edited
mid-run is left for the newer write to index.
"""
import argparse
import logging
import sys

import psycopg
from psycopg.rows import dict_row

from app import ask, config


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill the /ask retrieval index")
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--batch", type=int, default=100)
    args = p.parse_args()
    if config.AI_MODE == "off":
        print("AI_MODE is off; nothing to do")
        return 0
    total = 0
    with psycopg.connect(config.database_url(), autocommit=True, row_factory=dict_row) as conn:
        while total < args.limit:
            n = ask.index_pending(conn, None, min(args.batch, args.limit - total))
            if n == 0:
                break
            total += n
            print(f"indexed {total}", flush=True)
    print(f"done: {total} note version(s) indexed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
