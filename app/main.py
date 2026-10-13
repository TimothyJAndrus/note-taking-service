"""HTTP layer: routes, status codes, and access rules. SQL lives in app/repo.py."""
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status

from app import repo
from app.auth import current_user, hash_key, new_api_key, require_role
from app.db import get_conn, init_db
from app.models import (MemberAdd, MemberOut, NoteCreate, NoteOut, NotePage, NoteUpdate,
                        TeamCreate, TeamOut, UserCreate, UserCreated, UserOut)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Note-Taking Service",
    version="1.0.0",
    description="A shared notes service for multiple teams. Notes belong to a team; "
                "access is controlled by team membership and role.",
    lifespan=lifespan,
)


def _note_or_404(conn, team_id: str, note_id: str, user_id: str) -> dict:
    note = repo.get_note(conn, team_id, note_id, user_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


def _etag(note: dict) -> str:
    return f'"{note["version"]}"'


# ---- health & identity ------------------------------------------------------

@app.get("/healthz", tags=["ops"])
def healthz(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.post("/users", response_model=UserCreated, status_code=201, tags=["users"],
          summary="Register a user and receive an API key (shown once)")
def register(body: UserCreate, conn: sqlite3.Connection = Depends(get_conn)):
    if repo.get_user_by_email(conn, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    key = new_api_key()
    user = repo.create_user(conn, body.email, body.name, hash_key(key))
    return {**user, "api_key": key}


@app.get("/me", response_model=UserOut, tags=["users"])
def me(user: dict = Depends(current_user)):
    return user


# ---- teams ------------------------------------------------------------------

@app.post("/teams", response_model=TeamOut, status_code=201, tags=["teams"])
def create_team(body: TeamCreate, user: dict = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_conn)):
    return repo.create_team(conn, body.name, user["id"])


@app.get("/teams", response_model=list[TeamOut], tags=["teams"], summary="Teams I belong to")
def list_teams(user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return repo.list_teams(conn, user["id"])


@app.get("/teams/{team_id}/members", response_model=list[MemberOut], tags=["teams"])
def list_members(team_id: str, user: dict = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    return repo.list_members(conn, team_id)


@app.put("/teams/{team_id}/members", response_model=list[MemberOut], tags=["teams"],
         summary="Add a member or change their role (owners only)")
def put_member(team_id: str, body: MemberAdd, user: dict = Depends(current_user),
               conn: sqlite3.Connection = Depends(get_conn)):
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
def create_note(team_id: str, body: NoteCreate, response: Response,
                user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "editor")
    note = repo.create_note(conn, team_id, user["id"], body.model_dump())
    response.headers["ETag"] = _etag(note)
    response.headers["Location"] = f"/teams/{team_id}/notes/{note['id']}"
    return note


@app.get("/teams/{team_id}/notes", response_model=NotePage, tags=["notes"],
         summary="List or search notes, newest first")
def list_notes(team_id: str,
               q: str | None = Query(default=None, max_length=200, description="Full-text search over title and body"),
               tag: str | None = Query(default=None, max_length=40),
               author_id: str | None = None,
               limit: int = Query(default=20, ge=1, le=100),
               cursor: str | None = None,
               user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    try:
        items, next_cursor = repo.list_notes(conn, team_id, user["id"], q=q, tag=tag,
                                             author_id=author_id, limit=limit, cursor=cursor)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor")
    return {"items": items, "next_cursor": next_cursor}


@app.get("/teams/{team_id}/notes/{note_id}", response_model=NoteOut, tags=["notes"])
def get_note(team_id: str, note_id: str, response: Response,
             user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    require_role(conn, team_id, user["id"], "viewer")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    response.headers["ETag"] = _etag(note)
    return note


@app.patch("/teams/{team_id}/notes/{note_id}", response_model=NoteOut, tags=["notes"],
           summary="Partially update a note. Requires If-Match with the current ETag.")
def update_note(team_id: str, note_id: str, body: NoteUpdate, response: Response,
                if_match: str | None = Header(default=None),
                user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
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
def delete_note(team_id: str, note_id: str,
                user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    role = require_role(conn, team_id, user["id"], "editor")
    note = _note_or_404(conn, team_id, note_id, user["id"])
    if note["author_id"] != user["id"] and role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the author or a team owner can delete")
    repo.soft_delete_note(conn, note_id)
    return Response(status_code=204)
