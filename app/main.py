"""HTTP layer: routes, status codes, and access rules. SQL lives in app/repo.py."""
import uuid
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse

from app import config, repo, storage
from app.auth import current_user, hash_key, new_api_key, require_role
from app.db import close_pool, get_conn, open_pool
from app.models import (AttachmentCreate, AttachmentOut, AttachmentUpload, MemberAdd, MemberOut,
                        NoteCreate, NoteOut, NotePage, NoteUpdate, RevisionOut, TeamCreate, TeamOut,
                        UserCreate, UserCreated, UserOut)
from app.observability import (RequestContextMiddleware, TokenBucket, metrics_endpoint, setup_logging,
                               setup_tracing)

setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    open_pool()
    yield
    close_pool()


app = FastAPI(
    title="Note-Taking Service",
    version="2.0.0",
    description="A shared notes service for multiple teams. Notes belong to a team; "
                "access is controlled by team membership and role.",
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware,
                   limiter=TokenBucket(config.RATE_LIMIT_PER_MINUTE) if config.RATE_LIMIT_PER_MINUTE > 0 else None)
setup_tracing(app)


def _note_or_404(conn, team_id: UUID, note_id: UUID, user_id: str) -> dict:
    note = repo.get_note(conn, team_id, note_id, user_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


def _etag(note: dict) -> str:
    return f'"{note["version"]}"'


# ---- health & identity ------------------------------------------------------

@app.get("/healthz", tags=["ops"], summary="Liveness: the process is up (no dependencies checked)")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"], summary="Readiness: the database is reachable")
def readyz(conn: psycopg.Connection = Depends(get_conn)) -> dict:
    conn.execute("SELECT 1").fetchone()
    return {"status": "ready"}


@app.get("/metrics", tags=["ops"], include_in_schema=False)
def metrics() -> Response:
    return metrics_endpoint()


@app.post("/users", response_model=UserCreated, status_code=201, tags=["users"],
          summary="Register a user and receive an API key (shown once)")
def register(body: UserCreate, conn: psycopg.Connection = Depends(get_conn)):
    if not config.ALLOW_API_KEY_SIGNUP or config.AUTH_MODE == "oidc":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Self-registration is disabled; sign in with SSO")
    key = new_api_key()
    try:
        user = repo.create_user(conn, body.email, body.name, hash_key(key))
    except repo.DuplicateEmail:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    return {**user, "api_key": key}


@app.get("/me", response_model=UserOut, tags=["users"])
def me(user: dict = Depends(current_user)):
    return user


# ---- teams ------------------------------------------------------------------

@app.post("/teams", response_model=TeamOut, status_code=201, tags=["teams"])
def create_team(body: TeamCreate, user: dict = Depends(current_user),
                conn: psycopg.Connection = Depends(get_conn)):
    return repo.create_team(conn, body.name, user["id"])


@app.get("/teams", response_model=list[TeamOut], tags=["teams"], summary="Teams I belong to")
def list_teams(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    return repo.list_teams(conn, user["id"])


@app.get("/teams/{team_id}/members", response_model=list[MemberOut], tags=["teams"])
def list_members(team_id: UUID, user: dict = Depends(current_user),
                 conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    return repo.list_members(conn, team_id)


@app.put("/teams/{team_id}/members", response_model=list[MemberOut], tags=["teams"],
         summary="Add a member or change their role (owners only)")
def put_member(team_id: UUID, body: MemberAdd, user: dict = Depends(current_user),
               conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "owner")
    target = repo.get_user_by_email(conn, body.email)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that email")
    if target["id"] == user["id"] and body.role != "owner":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Owners cannot demote themselves")
    repo.upsert_member(conn, team_id, target["id"], body.role)
    return repo.list_members(conn, team_id)


# ---- notes ------------------------------------------------------------------

@app.post("/teams/{team_id}/notes", response_model=NoteOut, status_code=201, tags=["notes"])
def create_note(team_id: UUID, body: NoteCreate, response: Response,
                user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "editor")
    note = repo.create_note(conn, team_id, user["id"], body.model_dump())
    response.headers["ETag"] = _etag(note)
    response.headers["Location"] = f"/teams/{team_id}/notes/{note['id']}"
    return note


@app.get("/teams/{team_id}/notes", response_model=NotePage, tags=["notes"],
         summary="List or search notes, newest first")
def list_notes(team_id: UUID,
               q: str | None = Query(default=None, max_length=200, description="Full-text search over title and body"),
               tag: str | None = Query(default=None, max_length=40),
               author_id: UUID | None = None,
               limit: int = Query(default=20, ge=1, le=100),
               cursor: str | None = None,
               user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    try:
        items, next_cursor = repo.list_notes(conn, team_id, user["id"], q=q, tag=tag,
                                             author_id=author_id, limit=limit, cursor=cursor)
    except (ValueError, UnicodeDecodeError):  # malformed base64, timestamp, or UUID
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor")
    return {"items": items, "next_cursor": next_cursor}


@app.get("/teams/{team_id}/notes/{note_id}", response_model=NoteOut, tags=["notes"])
def get_note(team_id: UUID, note_id: UUID, response: Response,
             user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    response.headers["ETag"] = _etag(note)
    return note


@app.patch("/teams/{team_id}/notes/{note_id}", response_model=NoteOut, tags=["notes"],
           summary="Partially update a note. Requires If-Match with the current ETag.")
def update_note(team_id: UUID, note_id: UUID, body: NoteUpdate, response: Response,
                if_match: str | None = Header(default=None),
                user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "editor")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    if if_match is None:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED,
                            "If-Match header required; send the ETag from your last GET")
    try:
        expected = int(if_match.strip().removeprefix("W/").strip('"'))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed If-Match header")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("visibility") == "private" and note["author_id"] != user["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the author can make a note private")
    if not repo.update_note(conn, note_id, expected, changes):
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED,
                            "Note changed since you read it; GET it again and retry")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    response.headers["ETag"] = _etag(note)
    return note


@app.delete("/teams/{team_id}/notes/{note_id}", status_code=204, tags=["notes"],
            summary="Soft-delete a note (author or team owner)")
def delete_note(team_id: UUID, note_id: UUID,
                user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    role = require_role(conn, team_id, user["id"], "editor")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    if note["author_id"] != user["id"] and role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the author or a team owner can delete")
    repo.soft_delete_note(conn, note_id)
    return Response(status_code=204)


# ---- revision history --------------------------------------------------------------

@app.get("/teams/{team_id}/notes/{note_id}/revisions", response_model=list[RevisionOut], tags=["history"],
         summary="Every version of a note, newest first: who changed what, and when")
def list_revisions(team_id: UUID, note_id: UUID,
                   user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    _note_or_404(conn, team_id, note_id, user["id"])
    return repo.list_revisions(conn, note_id)


@app.post("/teams/{team_id}/notes/{note_id}/revisions/{version}/restore", response_model=NoteOut,
          tags=["history"], summary="Restore an earlier version (creates a new version). Requires If-Match.")
def restore_revision(team_id: UUID, note_id: UUID, version: int, response: Response,
                     if_match: str | None = Header(default=None),
                     user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "editor")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    rev = repo.get_revision(conn, note_id, version)
    if rev is None or rev["action"] == "delete":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revision not found")
    if if_match is None:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "If-Match header required")
    try:
        expected = int(if_match.strip().removeprefix("W/").strip('"'))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed If-Match header")
    if rev["visibility"] == "private" and note["author_id"] != user["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the author can make a note private")
    changes = {k: rev[k] for k in ("title", "body", "tags", "visibility")}
    if not repo.update_note(conn, note_id, expected, changes):
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Note changed since you read it")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    response.headers["ETag"] = _etag(note)
    return note


# ---- attachments ---------------------------------------------------------------------

def _require_storage() -> None:
    if not config.ATTACHMENTS_BUCKET:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Attachments are not configured")


@app.post("/teams/{team_id}/notes/{note_id}/attachments", response_model=AttachmentUpload, status_code=201,
          tags=["attachments"], summary="Start an upload: returns a presigned POST valid for 15 minutes")
def create_attachment(team_id: UUID, note_id: UUID, body: AttachmentCreate,
                      user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    _require_storage()
    require_role(conn, team_id, user["id"], "editor")
    _note_or_404(conn, team_id, note_id, user["id"])
    if body.size_bytes > config.ATTACHMENT_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"Attachments are limited to {config.ATTACHMENT_MAX_BYTES} bytes")
    attachment_id = uuid.uuid4()
    key = storage.object_key(team_id, note_id, attachment_id)
    att = repo.create_attachment(conn, attachment_id, note_id, team_id, user["id"], body.model_dump(), key)
    return {"attachment": att, "upload": storage.presign_upload(key, body.content_type, body.size_bytes)}


@app.post("/teams/{team_id}/notes/{note_id}/attachments/{attachment_id}/complete", response_model=AttachmentOut,
          tags=["attachments"], summary="Confirm the upload; the server checks the object exists at the declared size")
def complete_attachment(team_id: UUID, note_id: UUID, attachment_id: UUID,
                        user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    _require_storage()
    require_role(conn, team_id, user["id"], "editor")
    _note_or_404(conn, team_id, note_id, user["id"])
    att = repo.get_attachment(conn, note_id, attachment_id)
    if att is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    if att["status"] == "ready":
        return att
    if storage.uploaded_size(att["s3_key"]) != att["size_bytes"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "Upload not found, or its size doesn't match")
    return repo.mark_attachment_ready(conn, attachment_id)


@app.get("/teams/{team_id}/notes/{note_id}/attachments", response_model=list[AttachmentOut],
         tags=["attachments"])
def list_attachments(team_id: UUID, note_id: UUID,
                     user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    _note_or_404(conn, team_id, note_id, user["id"])
    return repo.list_attachments(conn, note_id)


@app.get("/teams/{team_id}/notes/{note_id}/attachments/{attachment_id}/download", status_code=307,
         tags=["attachments"], summary="Redirect to a presigned download URL valid for 5 minutes")
def download_attachment(team_id: UUID, note_id: UUID, attachment_id: UUID,
                        user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    _require_storage()
    require_role(conn, team_id, user["id"], "viewer")
    _note_or_404(conn, team_id, note_id, user["id"])
    att = repo.get_attachment(conn, note_id, attachment_id)
    if att is None or att["status"] != "ready":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return RedirectResponse(storage.presign_download(att["s3_key"], att["filename"], att["content_type"]),
                            status_code=307)
