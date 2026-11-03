-- 0002: production hardening.
--   * OIDC identities (users may have an IdP subject instead of / as well as an API key)
--   * note_revisions: every version of every note, written by a trigger (audit + restore)
--   * attachments: metadata for files stored in object storage (S3)
--   * row-level security: a second wall for tenant isolation, enforced by PostgreSQL itself

-- ---- identity --------------------------------------------------------------
ALTER TABLE users ALTER COLUMN api_key_hash DROP NOT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub text UNIQUE;
ALTER TABLE users ADD CONSTRAINT users_has_credential
    CHECK (api_key_hash IS NOT NULL OR oidc_sub IS NOT NULL);

-- ---- the role the application runs as ---------------------------------------
-- The app connects as the owner (needed for migrations) but immediately runs
-- SET ROLE notes_app. notes_app is not the table owner and not a superuser, so
-- row-level security always applies to it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'notes_app') THEN
        CREATE ROLE notes_app NOLOGIN NOBYPASSRLS;
    END IF;
END $$;
GRANT notes_app TO CURRENT_USER;

-- ---- audit history ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS note_revisions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    note_id     uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    team_id     uuid NOT NULL,
    version     integer NOT NULL,
    action      text NOT NULL CHECK (action IN ('create', 'update', 'delete')),
    title       text NOT NULL,
    body        text NOT NULL,
    tags        text[] NOT NULL,
    visibility  text NOT NULL,
    changed_by  uuid,                       -- the authenticated user (app.user_id)
    changed_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (note_id, version)
);
CREATE INDEX IF NOT EXISTS idx_revisions_team ON note_revisions (team_id);

CREATE OR REPLACE FUNCTION record_note_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    act text := CASE
        WHEN TG_OP = 'INSERT' THEN 'create'
        WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN 'delete'
        ELSE 'update' END;
BEGIN
    -- Every change (including soft delete) bumps the version, so each revision is one version.
    INSERT INTO note_revisions (note_id, team_id, version, action, title, body, tags, visibility, changed_by)
    VALUES (NEW.id, NEW.team_id, NEW.version, act, NEW.title, NEW.body, NEW.tags, NEW.visibility,
            nullif(current_setting('app.user_id', true), '')::uuid);
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS notes_revision ON notes;
CREATE TRIGGER notes_revision AFTER INSERT OR UPDATE ON notes
    FOR EACH ROW EXECUTE FUNCTION record_note_revision();

-- Backfill: existing notes get their current state as a revision.
INSERT INTO note_revisions (note_id, team_id, version, action, title, body, tags, visibility, changed_by, changed_at)
SELECT id, team_id, version, 'create', title, body, tags, visibility, author_id, updated_at FROM notes
ON CONFLICT DO NOTHING;

-- ---- attachments --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attachments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    note_id       uuid NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    team_id       uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    uploader_id   uuid NOT NULL REFERENCES users(id),
    filename      text NOT NULL CHECK (char_length(filename) BETWEEN 1 AND 255),
    content_type  text NOT NULL,
    size_bytes    bigint NOT NULL CHECK (size_bytes > 0),
    s3_key        text NOT NULL UNIQUE,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_attachments_note ON attachments (note_id) WHERE status = 'ready';

-- ---- row-level security ---------------------------------------------------------
-- A row is visible only to members of its team. The app sets app.user_id per request;
-- with no user set, nothing is visible. Application checks (roles, private notes) still
-- run first; this is the backstop if a query ever forgets its team filter.
CREATE OR REPLACE FUNCTION app_user_teams() RETURNS SETOF uuid
LANGUAGE sql STABLE AS $$
    SELECT team_id FROM memberships
    WHERE user_id = nullif(current_setting('app.user_id', true), '')::uuid
$$;

ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE note_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS team_isolation ON notes;
CREATE POLICY team_isolation ON notes TO notes_app
    USING (team_id IN (SELECT app_user_teams())) WITH CHECK (team_id IN (SELECT app_user_teams()));
DROP POLICY IF EXISTS team_isolation ON note_revisions;
CREATE POLICY team_isolation ON note_revisions TO notes_app
    USING (team_id IN (SELECT app_user_teams())) WITH CHECK (team_id IN (SELECT app_user_teams()));
DROP POLICY IF EXISTS team_isolation ON attachments;
CREATE POLICY team_isolation ON attachments TO notes_app
    USING (team_id IN (SELECT app_user_teams())) WITH CHECK (team_id IN (SELECT app_user_teams()));

GRANT USAGE ON SCHEMA public TO notes_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO notes_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO notes_app;
REVOKE ALL ON schema_migrations FROM notes_app;
