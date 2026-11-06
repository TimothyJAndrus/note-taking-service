-- 0003: retrieval for /teams/{id}/ask (hybrid vector + full-text search over note chunks).
--
-- Each note version is split into chunks; each chunk stores its embedding and its own
-- tsvector. A chunk records the note version it was cut from, and every query joins on the
-- note's *current* version, so a stale chunk (from before an edit, a restore or a delete)
-- can never be retrieved or cited. Isolation is the same two walls as notes: the app's
-- visibility filter (private = author only), and row-level security on team_id.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS note_chunks (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    note_id      uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    team_id      uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    version      integer NOT NULL,
    chunk_index  integer NOT NULL,
    title        text NOT NULL,
    content      text NOT NULL,
    embedding    vector(1024) NOT NULL,         -- Titan Text Embeddings v2, normalized
    model        text NOT NULL,                 -- which embedding model produced it
    search       tsvector GENERATED ALWAYS AS (
                     setweight(to_tsvector('english', title), 'A') ||
                     setweight(to_tsvector('english', content), 'B')) STORED,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (note_id, version, chunk_index)
);
-- HNSW rather than ivfflat: ivfflat derives its lists from the rows present when the index is
-- built, which on a new, empty table gives poor recall. HNSW needs no training data.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON note_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_search ON note_chunks USING gin (search);
CREATE INDEX IF NOT EXISTS idx_chunks_note_version ON note_chunks (note_id, version);
CREATE INDEX IF NOT EXISTS idx_chunks_team ON note_chunks (team_id);

ALTER TABLE note_chunks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS team_isolation ON note_chunks;
CREATE POLICY team_isolation ON note_chunks TO notes_app
    USING (team_id IN (SELECT app_user_teams())) WITH CHECK (team_id IN (SELECT app_user_teams()));

GRANT SELECT, INSERT, DELETE ON note_chunks TO notes_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO notes_app;
