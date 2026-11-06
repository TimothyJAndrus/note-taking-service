"""/ask: indexing notes for retrieval, and answering questions with citations.

Permission model: retrieval runs on the caller's own request connection (role notes_app,
app.user_id = caller), so row-level security and the private-note filter apply to every
chunk exactly as they apply to GET /notes. The model only ever sees chunks the caller
could already read, and citations can only point at those chunks.
"""
from __future__ import annotations

import logging
import re
import time
from uuid import UUID

import psycopg
from prometheus_client import Counter, Histogram

from app import ai, config, repo

log = logging.getLogger("notes.ask")

ASKS = Counter("ask_requests_total", "Questions answered by /ask", ["outcome"])
ASK_TOKENS = Counter("ask_model_tokens_total", "Model tokens used by /ask", ["direction"])
ASK_LATENCY = Histogram("ask_duration_seconds", "End-to-end /ask latency")
INDEXED = Counter("ask_notes_indexed_total", "Note versions embedded into the retrieval index", ["outcome"])

NOT_FOUND = "I can't find that in your notes."
_CITE = re.compile(r"\[(S\d{1,3})\]")


# ---- indexing -------------------------------------------------------------------------

def index_note(conn: psycopg.Connection, note: dict) -> bool:
    """Embed the note's current version. Returns False if it changed meanwhile (a newer index run wins)."""
    pieces = ai.chunk(note["title"], note["body"])
    vectors = [ai.to_pgvector(ai.embed(f"{note['title']}\n\n{p}")) for p in pieces]
    ok = repo.replace_chunks(conn, note, list(zip(pieces, vectors)), ai.embedding_model())
    INDEXED.labels("stored" if ok else "superseded").inc()
    return ok


def index_pending(conn: psycopg.Connection, team_id: UUID | None, limit: int) -> int:
    """Index up to `limit` notes whose current version isn't indexed. Returns how many were stored."""
    done = 0
    for note in repo.notes_needing_index(conn, team_id, limit):
        try:
            done += index_note(conn, note)
        except ai.ModelUnavailable:
            INDEXED.labels("failed").inc()
            log.warning("indexing failed; will retry on next ask", extra={"fields": {"note_id": str(note["id"])}})
            break  # the model is down; don't hammer it for every note
    return done


def index_after_write(team_id: UUID, note_id: UUID, user_id: UUID) -> None:
    """Background task after a create/update/restore. Best effort: /ask catches up on anything missed."""
    if config.AI_MODE == "off":
        return
    from app.db import pool_connection, set_actor
    try:
        with pool_connection() as conn:
            set_actor(conn, user_id)  # RLS: the writer's identity, so the insert passes WITH CHECK
            note = conn.execute("SELECT id, team_id, title, body, version FROM notes "
                                "WHERE id = %s AND team_id = %s AND deleted_at IS NULL",
                                (note_id, team_id)).fetchone()
            if note:
                index_note(conn, note)
    except Exception:  # never let indexing break anything; the next ask retries
        INDEXED.labels("failed").inc()
        log.warning("background indexing failed", exc_info=True, extra={"fields": {"note_id": str(note_id)}})


# ---- answering ------------------------------------------------------------------------

def _citations(answer: str, sources: list[dict]) -> tuple[str, list[dict], int]:
    """Keep only citations that point at a retrieved source; drop invented ones from the text."""
    by_sid = {s["sid"]: s for s in sources}
    invented = 0
    used: list[str] = []

    def keep(m: re.Match) -> str:
        nonlocal invented
        sid = m.group(1)
        if sid not in by_sid:
            invented += 1
            return ""
        if sid not in used:
            used.append(sid)
        return m.group(0)

    text = re.sub(r"[ \t]+([.,;:])", r"\1", _CITE.sub(keep, answer)).strip()
    cites = [{"source": sid, "note_id": by_sid[sid]["note_id"], "version": by_sid[sid]["version"],
              "chunk": by_sid[sid]["chunk_index"], "title": by_sid[sid]["title"],
              "snippet": by_sid[sid]["content"][:280]} for sid in used]
    return text, cites, invented


def answer(conn: psycopg.Connection, team_id: UUID, user_id: UUID, question: str, top_k: int) -> dict:
    start = time.perf_counter()
    try:
        # Notes written while the model was unreachable (or before this feature existed) get indexed now.
        caught_up = index_pending(conn, team_id, config.ASK_CATCHUP_LIMIT)
        pending = repo.count_notes_needing_index(conn, team_id)
        qvec = ai.to_pgvector(ai.embed(question))
        rows = repo.search_chunks(conn, team_id, user_id, query=question, query_vec=qvec, k=top_k)
        sources = [{**r, "sid": f"S{i}", "note_id": str(r["note_id"])} for i, r in enumerate(rows, 1)]
        if not sources:  # nothing readable matched: don't ask the model to improvise
            ASKS.labels("no_sources").inc()
            return {"answer": NOT_FOUND, "citations": [], "grounded": False,
                    "index": {"pending": pending, "indexed_now": caught_up}, "usage": {}}
        out = ai.generate(question, sources)
        ASK_TOKENS.labels("input").inc(out["input_tokens"])
        ASK_TOKENS.labels("output").inc(out["output_tokens"])
        text, cites, invented = _citations(out["text"], sources)
        not_found = text.startswith(NOT_FOUND)
        if not_found:  # the model said "not found" and then kept talking: return the plain refusal
            text, cites = NOT_FOUND, []
        grounded = bool(cites)
        ASKS.labels("answered" if grounded else ("not_found" if not_found else "uncited")).inc()
        log.info("ask", extra={"fields": {"team_id": str(team_id), "sources": len(sources), "cited": len(cites),
                                          "invented_citations": invented, "grounded": grounded,
                                          "input_tokens": out["input_tokens"],
                                          "output_tokens": out["output_tokens"]}})
        return {"answer": text, "citations": cites, "grounded": grounded,
                "index": {"pending": pending, "indexed_now": caught_up},
                "usage": {"input_tokens": out["input_tokens"], "output_tokens": out["output_tokens"],
                          "model": config.CHAT_MODEL_ID if config.AI_MODE == "bedrock" else "fake"}}
    finally:
        ASK_LATENCY.observe(time.perf_counter() - start)
