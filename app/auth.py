"""Authentication (who are you) and authorization (what may you do in this team).

Two credential types, chosen by AUTH_MODE:
  * api_key: opaque random keys issued by POST /users; only a SHA-256 hash is stored.
  * oidc:    a JWT from the corporate identity provider, verified against its JWKS
             (signature, issuer, audience, expiry). Users are provisioned on first sign-in.
"""
import hashlib
import secrets
from functools import lru_cache
from uuid import UUID

import jwt
import psycopg
from fastapi import Depends, Header, HTTPException, status

from app import config
from app.db import get_conn, set_actor

ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}
USER_COLS = "id, email, name, created_at"


def new_api_key() -> str:
    return "nts_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    # Keys are 256-bit random tokens, so a fast hash is safe here; slow hashes
    # (bcrypt/argon2) exist to protect low-entropy human passwords.
    return hashlib.sha256(key.encode()).hexdigest()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    # Caches signing keys and refetches on an unknown key ID, so IdP key rotation just works.
    return jwt.PyJWKClient(config.OIDC_JWKS_URL, cache_keys=True, lifespan=3600)


def _verify_jwt(token: str) -> dict:
    try:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=config.OIDC_AUDIENCE,
                          issuer=config.OIDC_ISSUER, options={"require": ["exp", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise _unauthorized(f"Invalid token: {e.__class__.__name__}") from None


def _user_from_claims(conn: psycopg.Connection, claims: dict) -> dict:
    email = (claims.get("email") or "").lower()
    if not email:
        raise _unauthorized("Token has no email claim")
    name = claims.get("name") or email.split("@")[0]
    try:
        return conn.execute(
            "INSERT INTO users (email, name, oidc_sub) VALUES (%s, %s, %s) "
            f"ON CONFLICT (oidc_sub) DO UPDATE SET name = EXCLUDED.name RETURNING {USER_COLS}",
            (email, name, claims["sub"])).fetchone()
    except psycopg.errors.UniqueViolation:
        # The email belongs to an existing API-key account. Linking accounts by email alone
        # would let anyone who controls an IdP email take over that account, so refuse.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Email already registered with an API key; ask an admin to link the accounts") from None


def current_user(
    authorization: str | None = Header(default=None),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("Missing bearer token")
    token = authorization[7:].strip()
    looks_like_jwt = token.count(".") == 2
    if looks_like_jwt and config.AUTH_MODE in ("oidc", "both"):
        row = _user_from_claims(conn, _verify_jwt(token))
    elif not looks_like_jwt and config.AUTH_MODE in ("api_key", "both"):
        row = conn.execute(f"SELECT {USER_COLS} FROM users WHERE api_key_hash = %s",
                           (hash_key(token),)).fetchone()
        if row is None:
            raise _unauthorized("Invalid token")
    else:
        raise _unauthorized("Credential type not accepted")
    set_actor(conn, row["id"])
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
