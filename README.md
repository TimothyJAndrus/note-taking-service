# Note-Taking Service

A small REST service where multiple teams capture and share notes. A note belongs to a team. Who can read, write, or delete it depends on the caller's role in that team.

- **Stack:** Python 3.12+, FastAPI, PostgreSQL 17 (psycopg 3, connection pool, built-in full-text search)
- **Interactive API docs:** `http://localhost:8000/docs` (OpenAPI, generated from the code)
- **Tests:** 47 end-to-end tests (`pytest`) against real PostgreSQL + pgvector, plus a dependency audit, an image build, and `cdk synth`, all in CI on every push
- **Ask:** `POST /teams/{id}/ask` answers questions from a team's notes, citing note id and version, and can only use notes the caller can read. An MCP server exposes search, read, create and ask to agents under the caller's own identity.
- **Deploy:** AWS CDK app in [`infra/`](infra/README.md): ECS Fargate behind an ALB with WAF, RDS PostgreSQL 17, S3 for attachments

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

Configuration is environment variables only (`app/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` or `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`/`DB_SSLMODE` | local compose DB | Connection. The `DB_*` form is how ECS injects Secrets Manager fields. |
| `RUN_MIGRATIONS_ON_STARTUP` | `true` | Local convenience. `false` in AWS, where migrations run as a one-off task. |
| `AUTH_MODE` | `api_key` | `api_key`, `oidc`, or `both` |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL` | empty | Corporate IdP settings for JWT verification |
| `ALLOW_API_KEY_SIGNUP` | `true` | Turn off self-registration when everyone signs in with SSO |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per-credential limit per instance; `0` disables |
| `ATTACHMENTS_BUCKET` / `ATTACHMENT_MAX_BYTES` | empty / 25 MB | S3 bucket for attachments (endpoints return 503 when unset) |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Structured logs |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Turns on OpenTelemetry tracing (e.g. an ADOT collector sidecar) |
| `AI_MODE` | `off` | `bedrock` turns on `/ask`; `fake` is an offline stand-in for tests and local dev |
| `EMBED_MODEL_ID` / `CHAT_MODEL_ID` | Titan Text Embeddings v2 / Claude Haiku 4.5 (`us.` profile) | Bedrock models for `/ask` |
| `ASK_TOP_K` / `ASK_MAX_OUTPUT_TOKENS` / `ASK_RATE_PER_MINUTE` | `8` / `600` / `10` | Chunks given to the model, answer length cap, per-user ask limit |

Migrations: `python -m app.migrate` applies the numbered SQL files in `migrations/` once each, in order, under an advisory lock. `docker compose up` runs them as a separate `migrate` service before the API starts, the same way production does. The database container also creates a separate `notes_test` database, so the tests never touch dev data.

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
| GET | `/healthz` | anyone | Liveness (process up; no dependencies) |
| GET | `/readyz` | anyone | Readiness (database reachable); the load balancer's health check |
| GET | `/metrics` | internal | Prometheus metrics (blocked at the public load balancer) |
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
| GET | `/teams/{team_id}/notes/{id}/revisions` | viewer+ | Every version: who changed what, when |
| POST | `/teams/{team_id}/notes/{id}/revisions/{version}/restore` | editor+ | Restore a version as a new one; requires `If-Match` |
| POST | `/teams/{team_id}/notes/{id}/attachments` | editor+ | Start an upload; returns a presigned S3 POST |
| POST | `/teams/{team_id}/notes/{id}/attachments/{aid}/complete` | editor+ | Confirm the upload (server checks the object and size) |
| GET | `/teams/{team_id}/notes/{id}/attachments` | viewer+ | List uploaded attachments |
| GET | `/teams/{team_id}/notes/{id}/attachments/{aid}/download` | viewer+ | 307 to a 5-minute presigned download link |
| POST | `/teams/{team_id}/ask` | viewer+ | Answer a question from notes you can read, with citations (`note_id`, `version`) |

**Roles:** `viewer` reads. `editor` also creates and edits team notes. `owner` also manages members and can delete any note. A `private` note is visible only to its author, even inside the team, and only the author can make a note private.

**Status codes used on purpose:**
- `401`: missing or bad token
- `404`: you aren't a member, so the team appears not to exist
- `403`: you're a member, but your role doesn't allow the action
- `409`: duplicate email; SSO email already owned by an API-key account; upload missing or wrong size
- `412`: someone else changed the note since you read it
- `428`: `If-Match` is missing
- `422`: validation failed
- `413`: attachment over the size limit
- `429`: rate limit exceeded (with `Retry-After`)
- `503`: attachments not configured, `/ask` not enabled, or the model is unavailable (with `Retry-After`)

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

## Ask: answers from your notes, with citations

```bash
curl -s -X POST $B/teams/$TEAM/ask -H "$AUTH" -H 'content-type: application/json' \
  -d '{"question":"What do we run before the rollout?"}'
# {"answer": "Run database migrations as the one-off ECS task before the rollout. [S1]",
#  "citations": [{"source":"S1","note_id":"…","version":3,"title":"Deploy checklist","snippet":"…"}],
#  "grounded": true, "index": {"pending": 0, "indexed_now": 0}, "usage": {…}}
```

**Permission-aware retrieval.** Retrieval runs on the caller's own request connection, so the same two walls that protect `GET /notes` protect every chunk the model sees: row-level security on `team_id` (PostgreSQL itself) and the private-note rule. The model can only draw on, and cite, notes the caller could already open. A non-member gets `404`, the same as for any other team URL.

**Hybrid search.** Each note version is split into chunks (`note_chunks`); each chunk has a Titan v2 embedding (pgvector, HNSW index) and its own `tsvector`. Vector and full-text results are merged with reciprocal rank fusion, so exact terms (an error code, a name) and paraphrases both find the right note.

**Citations you can check.** The model sees numbered sources and must cite them as `[S1]`. The server keeps only citations that point at a retrieved source, strips invented ones, and returns each with the note id and the version the text came from. `grounded` is true only if at least one real citation survived. If nothing readable matches, the model isn't called at all.

**Never stale.** A chunk records the note version it was cut from, and retrieval joins on the note's *current* version. Text from before an edit, a restore or a delete can't be retrieved or cited, even before re-indexing finishes. A slow indexer can't overwrite newer chunks either (it re-checks the version under a row lock).

**Indexing never blocks writes.** Creates, updates and restores schedule indexing after the response is sent. If Bedrock is down, the write still succeeds; the next `/ask` indexes whatever is missing (`index.pending` reports how many notes aren't searchable yet). `python -m app.reindex` backfills everything, for example after first enabling the feature.

**Notes are data, not instructions.** Note text goes to the model inside source tags with an instruction to treat it as text. The eval below includes a note that tries to override the rules.

**Measured, not assumed.** `evals/ask_eval.py` seeds a team and a rival team and scores 13 cases with deterministic rules: facts, multi-note answers, conflicting notes, a prompt-injection note, questions the notes can't answer, a private note asked about by a teammate and by its author, a cross-team question, and an outsider. Any leak fails the run. Claude Haiku 4.5 via Bedrock: **39/39** (13 cases × 3 runs), no leaks, ~19.7k input / ~1k output tokens for the whole run.

```bash
AWS_PROFILE=<profile> AI_MODE=bedrock python -m evals.ask_eval --repeat 3
```

### MCP server

`app/mcp_server.py` gives agents five tools: `list_teams`, `search_notes`, `read_note`, `create_note`, `ask`. It has no database access: every tool calls this API with the **caller's own token**, so an agent can't see or do more than the person it acts for (roles, private notes, RLS, rate limits and the revision history all apply). Read tools are annotated read-only.

```bash
# Local, for a desktop client (Claude Desktop, Cursor, Hermes):
NOTES_API_URL=https://notes.example.com NOTES_API_TOKEN=<api key or IdP JWT> python -m app.mcp_server

# Shared, over streamable HTTP: each request's own Authorization header is forwarded.
NOTES_API_URL=https://notes.example.com python -m app.mcp_server --http --port 8001
```

In HTTP mode a request without a bearer token is refused; the server never falls back to a token from its own environment.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

End-to-end tests drive the real app over HTTP (FastAPI `TestClient`) against a real PostgreSQL database, not a mock or SQLite stand-in, so the SQL that ships is the SQL that's tested. Every table is truncated before each test, so tests are isolated and can run in any order. Set `TEST_DATABASE_URL` to point them elsewhere (it must be a disposable database). GitHub Actions runs the suite on every push against a PostgreSQL 17 + pgvector service container (`.github/workflows/tests.yml`). `/ask` tests run with `AI_MODE=fake` (deterministic, offline), so CI needs no AWS credentials; model quality is measured by the live eval instead.

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
| Ask | `tests/test_ask.py` (14) | Citations carry note id + version; outsiders get `404`; another team's notes never appear; RLS alone blocks chunks; private notes inform only their author; edits and deletes are never cited stale; stale indexers can't overwrite; invented citations are stripped; a model outage never breaks writes; rate limit, disabled mode, chunking |
| MCP | `tests/test_mcp.py` (3) | Tools act as the caller; API roles apply to agents; HTTP never falls back to the server's token |

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
| Auth | OIDC/JWT from the IdP, plus hashed API keys for service accounts | SSO for people, keys for automation | Group-to-team sync not built yet |
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
note_chunks(note_id, team_id, version, chunk_index, title, content, embedding vector(1024), model,
            search tsvector GENERATED)                                 -- /ask retrieval; RLS on team_id
  HNSW (embedding vector_cosine_ops), GIN (search), (note_id, version)
```

IDs are UUIDs generated by PostgreSQL (`gen_random_uuid()`), so they can't be guessed or enumerated. Timestamps are `timestamptz` (UTC).

## Production readiness

Version 1 ended with a list of what production would need. Version 2 builds it:

| Need | What's built | Where | Proven by |
|---|---|---|---|
| **Identity** | OIDC/JWT from the corporate IdP (signature via JWKS, issuer, audience, expiry). Users are provisioned on first sign-in. An IdP email never silently takes over an existing API-key account. API keys remain for service accounts; self-signup can be turned off. | `app/auth.py` | `test_oidc_*` (valid, wrong audience, expired, forged signature, takeover attempt) |
| **Database operations** | Versioned SQL migrations run as a separate step. **Row-level security**: the app runs as a restricted role (`notes_app`), so PostgreSQL itself hides other teams' rows even if a query forgets its team filter. Managed RDS with backups, Multi-AZ, and forced TLS in `infra/`. | `migrations/`, `app/migrate.py`, `app/db.py` | `test_rls_hides_other_teams_rows...` (fails if RLS is disabled; checked), `test_migrations_recorded_and_idempotent` |
| **Audit and history** | A trigger writes every version of every note (create, update, delete) with the authenticated user. `GET .../revisions` shows the history; `POST .../revisions/{v}/restore` brings back an earlier version as a new one (If-Match required). | `migrations/0002_production.sql`, `app/main.py` | `test_every_change_is_a_revision...`, `test_restore_an_earlier_version` |
| **Operations** | JSON logs with a request ID on every line and response (`X-Request-ID`, accepted from upstream). Prometheus metrics at `/metrics` by route template. Liveness `/healthz` vs readiness `/readyz`. Per-credential rate limiting (429 + `Retry-After`) plus a WAF rate rule per IP at the edge. Optional OpenTelemetry traces (SQL text only, never parameters). | `app/observability.py` | `test_request_ids_and_metrics`, `test_rate_limit_middleware_returns_429`, `test_liveness_and_readiness` |
| **Attachments** | S3 with presigned POST uploads; S3 enforces the declared size and type. The server confirms the object exists before listing it. Object keys never contain user filenames. Downloads are 5-minute presigned links forced to `Content-Disposition: attachment`. | `app/storage.py` | `test_attachment_*` (S3 mocked with moto) |
| **Supply chain** | Pinned dependencies with `pip-audit` in CI (FastAPI/Starlette upgraded to clear 14 known Starlette advisories). Multi-stage image running as a fixed non-root UID with a read-only root filesystem. | `requirements.txt`, `Dockerfile` | CI `test` + `image` jobs |

**Still not done** (honest list): syncing team membership from IdP groups; sharing a note across teams; webhooks/events on note changes; malware scanning of uploads; load testing under parallel writers; a shared (cross-instance) per-credential rate limit, since today that is per instance plus WAF per IP.

## Project layout

```
app/
  main.py           routes, status codes, access rules
  auth.py           API-key and OIDC auth, role checks
  models.py         request/response schemas and validation
  repo.py           all SQL
  db.py             connection pool, restricted role, per-request identity
  migrate.py        versioned SQL migrations (python -m app.migrate)
  observability.py  JSON logs, request IDs, metrics, rate limiting, tracing
  storage.py        S3 presigned uploads/downloads
  ai.py             Bedrock embeddings + answers (and an offline fake), chunking
  ask.py            /ask: indexing, permission-aware retrieval, citation checks
  reindex.py        backfill the retrieval index (python -m app.reindex)
  mcp_server.py     MCP server: search/read/create/ask as the caller
  config.py         every environment variable in one place
migrations/         numbered .sql files
tests/
  test_api.py         core API behavior against PostgreSQL
  test_production.py  RLS, history, OIDC, attachments, rate limits, metrics
  test_ask.py         /ask retrieval, isolation, citations, staleness
  test_mcp.py         MCP tools under the caller's identity
evals/              live /ask eval (cases + deterministic scorer)
infra/              AWS CDK app (see infra/README.md)
docker-compose.yml  API + migrate step + PostgreSQL 17 with pgvector (plus a notes_test database)
docker/initdb/       creates the test database on first start
```
