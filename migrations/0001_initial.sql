-- 0001: initial schema (users, teams, memberships, notes with full-text search).
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
