# Note-Taking Service

A small REST service where multiple teams capture and share notes. A note belongs to a team. Who can read, write, or delete it depends on the caller's role in that team.

- **Stack:** Python 3.12+, FastAPI, PostgreSQL 17 (psycopg 3, connection pool, built-in full-text search)
- **Interactive API docs:** `http://localhost:8000/docs` (OpenAPI, generated from the code)
- **Tests:** 15 end-to-end API tests (`pytest`), run in CI on every push

## Run it

Everything in Docker (API + PostgreSQL):

```bash
docker compose up --build              # API at http://localhost:8000/docs
```

Or run the API locally against the Dockerized database:

```bash
docker compose up -d db                # PostgreSQL on localhost:5433
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000/docs
pytest -v                              # uses the separate notes_test database
```

Configuration is one environment variable: `DATABASE_URL` (default `postgresql://notes:notes@localhost:5433/notes`). `DB_POOL_SIZE` sets the connection pool size (default 10). The schema is applied on startup. The database container also creates a separate `notes_test` database, so the tests never touch dev data.

## Try it

```bash
B=http://localhost:8000

# 1. Register. The API key is returned once; only its hash is stored.
KEY=$(curl -s -X POST $B/users -H 'content-type: application/json' \
  -d '{"email":"ana@example.com","name":"Ana"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')
AUTH="Authorization: Bearer $KEY"

# 2. Create a team (you become its owner)
TEAM=$(curl -s -X POST $B/teams -H "$AUTH" -H 'content-type: application/json' \
  -d '{"name":"Platform"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 3. Write a note (the response carries ETag: "1")
curl -si -X POST $B/teams/$TEAM/notes -H "$AUTH" -H 'content-type: application/json' \
  -d '{"title":"Deploy checklist","body":"Run migrations before the rollout","tags":["ops"]}'

# 4. Search, filter, and page
curl -s "$B/teams/$TEAM/notes?q=migrations" -H "$AUTH"
curl -s "$B/teams/$TEAM/notes?tag=ops&limit=10" -H "$AUTH"

# 5. Update: send back the ETag you received, as If-Match
curl -s -X PATCH $B/teams/$TEAM/notes/<note_id> -H "$AUTH" -H 'If-Match: "1"' \
  -H 'content-type: application/json' -d '{"body":"Run migrations, then roll out"}'
```

## API

| Method | Path | Who | What |
|---|---|---|---|
| GET | `/healthz` | anyone | Liveness plus a database check |
| POST | `/users` | anyone | Register; returns an API key once |
| GET | `/me` | any user | Who am I |
| POST | `/teams` | any user | Create a team; the caller becomes owner |
| GET | `/teams` | any user | Teams I belong to, with my role |
| GET | `/teams/{team_id}/members` | viewer+ | List members |
| PUT | `/teams/{team_id}/members` | owner | Add a member or change a role (idempotent) |
| POST | `/teams/{team_id}/notes` | editor+ | Create a note |
| GET | `/teams/{team_id}/notes` | viewer+ | List newest first; `q`, `tag`, `author_id`, `limit`, `cursor` |
| GET | `/teams/{team_id}/notes/{id}` | viewer+ | Read one note (returns `ETag`) |
| PATCH | `/teams/{team_id}/notes/{id}` | editor+ | Partial update; requires `If-Match` |
| DELETE | `/teams/{team_id}/notes/{id}` | author or owner | Soft delete |

**Roles:** `viewer` reads. `editor` also creates and edits team notes. `owner` also manages members and can delete any note. A `private` note is visible only to its author, even inside the team, and only the author can make a note private.

**Status codes used on purpose:**
- `401`: missing or bad token
- `404`: you aren't a member, so the team appears not to exist
- `403`: you're a member, but your role doesn't allow the action
- `409`: duplicate email
- `412`: someone else changed the note since you read it
- `428`: `If-Match` is missing
- `422`: validation failed

## Design choices, and why

**Team-scoped resources (`/teams/{id}/notes`).** Isolating one team's data from another's is the core requirement of a shared service, so the team is part of every note URL and every query. Every SQL statement filters on `team_id`, and access is checked by one function, `require_role`, before any data is read. Non-members get `404`, not `403`, so the API never confirms a team exists to someone outside it.

**Optimistic concurrency with ETag / If-Match.** Shared notes mean two people will edit the same note. Without a guard, the second save silently erases the first ("lost update"). Each note carries a `version`. `GET` returns it as an `ETag`, and `PATCH` must send it back. The update runs as `UPDATE ... WHERE version = ?`, so a stale write matches zero rows and returns `412`, and the client re-reads and retries. This needs no locks or long transactions, and it uses standard HTTP semantics that any client library understands.

**Keyset (cursor) pagination, not offset.** `?cursor=` encodes the last row's `(updated_at, id)`. Each page is an index range scan, cheap at any depth. New notes arriving between requests don't shift pages or cause duplicates, which offset pagination would.

**PostgreSQL.** A service shared by many teams needs many concurrent writers, and PostgreSQL handles that with row-level locking. It also provides what this design leans on: native `uuid` and `timestamptz` types, `text[]` arrays for tags, partial indexes, `CHECK` constraints, and full-text search, all in one system. Connections come from a pool (psycopg 3 + psycopg-pool), so the app doesn't open a new connection per request. Several instances can run against one database: an advisory lock makes sure only one of them applies the schema at startup.

**Full-text search inside PostgreSQL.** Search is what makes a notes service useful. A generated `tsvector` column (title weighted above body) is maintained by PostgreSQL itself, so it can never drift out of sync, and a GIN index makes it fast. Queries go through `websearch_to_tsquery`, which accepts any user text (phrases in quotes, `-exclusions`, `or`) and never raises a syntax error. English stemming means a search for "migration" also finds "migrations." There's no separate search cluster to run until scale demands one.

**Tags as a `text[]` column with a GIN index.** Tags belong to the note, so they live on the note row. The array is read and written with the note in one statement, and `tags @> ARRAY['ops']` filtering uses the GIN index.

**API keys, hashed.** Each user gets a 256-bit random key, and only its SHA-256 hash is stored. A fast hash is appropriate for high-entropy tokens; bcrypt and argon2 exist to slow down guessing of low-entropy human passwords. In a real shared service, identity would come from the company's SSO (OIDC / JWT), with API keys reserved for service-to-service calls. `current_user` is the only function that would change.

**Soft delete.** `DELETE` sets `deleted_at` rather than destroying the row. Notes are work records, and an accidental delete should be recoverable. Every read filters deleted rows out, and the main list index is a partial index that excludes them.

**Constraints in the database, not just the app.** Unique emails, valid roles, valid visibility, title length, and foreign keys are enforced by PostgreSQL as well as by the API. If two people register the same email at the same instant, the `UNIQUE` constraint decides, and the API turns the violation into a clean `409`.

**Validation at the edge.** Pydantic models enforce limits before any business logic runs: title 1–200 characters, body ≤ 100 KB, ≤ 20 tags of ≤ 40 characters, page size 1–100. Tags are trimmed, lower-cased, and de-duplicated, so `Ops` and ` ops ` are the same tag.

**Layering.** `models.py` (shapes) → `main.py` (HTTP and access rules) → `repo.py` (SQL) → `db.py` (connection and schema). Each layer is small enough to read in one sitting.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

Fifteen end-to-end tests drive the real app over HTTP (FastAPI `TestClient`) against a real PostgreSQL database, not a mock or SQLite stand-in, so the SQL that ships is the SQL that's tested. Every table is truncated before each test, so tests are isolated and can run in any order. Set `TEST_DATABASE_URL` to point them elsewhere (it must be a disposable database). GitHub Actions runs the suite on every push against a PostgreSQL 17 service container (`.github/workflows/tests.yml`).

The tests are organized around the rules a shared service must not break:

| Area | Test | What it proves |
|---|---|---|
| Ops | `test_health` | The service is up and can reach its database |
| Auth | `test_requires_auth` | No token or a bad token gets `401` |
| Auth | `test_duplicate_email_rejected` | Emails are unique, case-insensitively (`409`) |
| Core flow | `test_create_get_and_list_note` | Create, read, and list work; tags are normalized; `ETag` is returned |
| Isolation | `test_outsider_cannot_see_team` | Non-members get `404` for reads and writes, so a team's existence never leaks |
| Roles | `test_viewer_is_read_only` | Viewers can't create notes (`403`) |
| Roles | `test_only_owner_manages_members` | Only owners can add members or change roles |
| Privacy | `test_private_notes_are_author_only` | Private notes are hidden from teammates, in both reads and lists |
| Privacy | `test_only_author_can_make_note_private` | Nobody can hide a teammate's note |
| Concurrency | `test_update_requires_current_version` | Missing `If-Match` → `428`; a stale version → `412`; the newer edit survives |
| Search | `test_search_and_tag_filter` | Full-text and tag filters return the right notes; search operators in user input can't break the query |
| Search | `test_search_reflects_edits` | The search index updates when a note is edited |
| Paging | `test_cursor_pagination_walks_every_note_once` | Paging returns every note exactly once, newest first; a bad cursor gets `400` |
| Delete | `test_delete_rules_and_soft_delete` | Only the author or an owner can delete; deleted notes disappear from reads and lists |
| Validation | `test_validation_errors` | Empty titles, too many tags, and bad page sizes are rejected (`422`) |

**Not covered yet:** load and concurrency testing under real parallel writers, and fuzzing of request bodies. Both are on the production list below.

## Design decisions at a glance

| Decision | Choice | Main reason | Trade-off accepted |
|---|---|---|---|
| Language / framework | Python + FastAPI | Typed validation and OpenAPI docs generated from the code | Slower raw throughput than Go; fine for a notes service |
| Storage | PostgreSQL 17 + built-in full-text search | Concurrent writers, strong constraints, search without extra infrastructure | One more service to run (handled by Docker Compose) |
| Search | Generated `tsvector` + GIN, `websearch_to_tsquery` | Always in sync, fast, safe with any user input | English stemming only; dedicated search engine if needs grow |
| Tags | `text[]` + GIN | Stored with the note; one-statement reads and writes | Renaming a tag across notes is a bulk update |
| Tenancy | Team in every URL and query | Isolation is the core requirement of a shared service | Cross-team sharing needs an explicit feature later |
| Authorization | Roles: viewer, editor, owner | Simple, covers read/write/admin | No per-note permissions beyond `private` |
| Concurrent edits | Version + ETag / If-Match | Prevents lost updates without locks | Clients must send `If-Match` and handle `412` |
| Pagination | Keyset cursor | Fast at any depth; stable while notes are added | No "jump to page 7" |
| Auth | Hashed API keys | Simple and secure for a deliverable | Production should use the company's SSO (OIDC) |
| Delete | Soft delete | Work records should be recoverable | Rows accumulate; needs a purge policy |

The reasoning behind each choice is in **Design choices, and why** above.

## Data model

```
users(id uuid, email text UNIQUE, name, api_key_hash UNIQUE, created_at timestamptz)
teams(id uuid, name, created_at)
memberships(team_id, user_id, role CHECK in owner|editor|viewer)   PK(team_id, user_id), index(user_id)
notes(id uuid, team_id, author_id, title CHECK 1..200, body, tags text[],
      visibility CHECK in team|private, version, created_at, updated_at, deleted_at,
      search tsvector GENERATED ALWAYS (title weight A, body weight B))
  index (team_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL   -- the list/paging path
  GIN (search)                                                         -- full-text search
  GIN (tags)                                                           -- tag filter
```

IDs are UUIDs generated by PostgreSQL (`gen_random_uuid()`), so they can't be guessed or enumerated. Timestamps are `timestamptz` (UTC).

## What I'd add for production

1. **Identity:** OIDC/JWT from the corporate IdP; team membership synced from groups.
2. **Database operations:** versioned migrations (Alembic) instead of startup DDL, row-level security as a second line of tenant isolation, a managed instance (for example Amazon RDS or Cloud SQL) with backups and a read replica.
3. **Audit and history:** a `note_revisions` table (who changed what, when), which also enables restore.
4. **Operations:** structured JSON logs with request IDs, OpenTelemetry traces, Prometheus metrics, and rate limiting per API key.
5. **Features teams will ask for next:** attachments (object storage plus signed URLs), sharing a note across teams, and webhooks or events on note changes.

## Project layout

```
app/
  main.py     routes, status codes, access rules
  auth.py     bearer-token auth and role checks
  models.py   request/response schemas and validation
  repo.py     all SQL
  db.py       connection pool and schema
tests/
  test_api.py end-to-end tests against PostgreSQL
docker-compose.yml   API + PostgreSQL 17 (plus a notes_test database)
docker/initdb/       creates the test database on first start
```
