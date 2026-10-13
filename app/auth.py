"""Authentication (who are you) and authorization (what may you do in this team)."""
import hashlib
import secrets
from uuid import UUID

import psycopg
from fastapi import Depends, Header, HTTPException, status

from app.db import get_conn

ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}


def new_api_key() -> str:
    return "nts_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    # Keys are 256-bit random tokens, so a fast hash is safe here; slow hashes
    # (bcrypt/argon2) exist to protect low-entropy human passwords.
    return hashlib.sha256(key.encode()).hexdigest()


def current_user(
    authorization: str | None = Header(default=None),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    token = authorization[7:].strip()
    row = conn.execute("SELECT id, email, name, created_at FROM users WHERE api_key_hash = %s",
                       (hash_key(token),)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token",
                            headers={"WWW-Authenticate": "Bearer"})
    return row


def require_role(conn: psycopg.Connection, team_id: UUID, user_id: UUID, minimum: str) -> str:
    """Return the caller's role in the team, or raise.

    Non-members get 404, not 403, so the API never confirms that a team exists
    to someone outside it.
    """
    row = conn.execute("SELECT role FROM memberships WHERE team_id = %s AND user_id = %s",
                       (team_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    if ROLE_RANK[row["role"]] < ROLE_RANK[minimum]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {minimum} role")
    return row["role"]
