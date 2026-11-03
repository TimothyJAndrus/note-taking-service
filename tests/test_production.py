"""Production features: migrations, row-level security, revision history, OIDC, attachments,
rate limiting, request IDs and metrics. Same real-PostgreSQL setup as test_api.py."""
import base64
import json
import time
import uuid

import boto3
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from app import auth, config, storage
from app.migrate import migrate
from app.observability import RequestContextMiddleware, TokenBucket
from tests.test_api import TEST_DB, client, notes_url, register, team  # noqa: F401  (fixtures)


def _note(client, team, **kw):
    r = client.post(notes_url(team), json={"title": "Runbook", "body": "v1 body", **kw}, headers=team["editor"])
    assert r.status_code == 201, r.text
    return r.json()


def _user_id(client, headers):
    return client.get("/me", headers=headers).json()["id"]


# ---- migrations -----------------------------------------------------------------------

def test_migrations_recorded_and_idempotent(client):
    with psycopg.connect(TEST_DB) as conn:
        done = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert done[:2] == ["0001_initial", "0002_production"]
    assert migrate(TEST_DB) == []  # second run applies nothing


# ---- row-level security ----------------------------------------------------------------

def test_rls_hides_other_teams_rows_even_without_a_team_filter(client, team):
    _note(client, team)
    owner_id, outsider_id = _user_id(client, team["owner"]), _user_id(client, team["outsider"])
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("SET ROLE notes_app")  # exactly what the pool does for every connection

        def visible(user_id):
            conn.execute("SELECT set_config('app.user_id', %s, false)", (user_id,))
            return conn.execute("SELECT count(*) FROM notes").fetchone()[0]  # deliberately unfiltered

        assert visible(owner_id) == 1
        assert visible(outsider_id) == 0
        assert visible("") == 0  # no identity, no rows
        conn.execute("SELECT set_config('app.user_id', %s, false)", (outsider_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # can't write into another team either
            conn.execute("INSERT INTO notes (team_id, author_id, title) VALUES (%s, %s, 'x')",
                         (team["id"], outsider_id))


# ---- revision history ------------------------------------------------------------------

def test_every_change_is_a_revision_with_its_author(client, team):
    note = _note(client, team)
    url = f"{notes_url(team)}/{note['id']}"
    r = client.patch(url, json={"title": "Runbook v2"}, headers={**team["owner"], "If-Match": '"1"'})
    assert r.status_code == 200
    revs = client.get(f"{url}/revisions", headers=team["viewer"]).json()
    assert [(v["version"], v["action"], v["title"]) for v in revs] == [(2, "update", "Runbook v2"),
                                                                     (1, "create", "Runbook")]
    assert revs[0]["changed_by"] == _user_id(client, team["owner"])
    assert revs[1]["changed_by"] == _user_id(client, team["editor"])

    assert client.delete(url, headers=team["owner"]).status_code == 204
    with psycopg.connect(TEST_DB) as conn:  # the delete is recorded too, even though the note is gone from the API
        last = conn.execute("SELECT version, action FROM note_revisions WHERE note_id = %s "
                            "ORDER BY version DESC LIMIT 1", (note["id"],)).fetchone()
    assert last == (3, "delete")


def test_restore_an_earlier_version(client, team):
    note = _note(client, team)
    url = f"{notes_url(team)}/{note['id']}"
    client.patch(url, json={"title": "Oops", "body": "overwritten"}, headers={**team["editor"], "If-Match": '"1"'})
    assert client.post(f"{url}/revisions/1/restore", headers=team["editor"]).status_code == 428
    assert client.post(f"{url}/revisions/1/restore", headers={**team["editor"], "If-Match": '"1"'}).status_code == 412
    assert client.post(f"{url}/revisions/1/restore", headers={**team["viewer"], "If-Match": '"2"'}).status_code == 403
    r = client.post(f"{url}/revisions/1/restore", headers={**team["editor"], "If-Match": '"2"'})
    assert r.status_code == 200
    assert (r.json()["title"], r.json()["body"], r.json()["version"]) == ("Runbook", "v1 body", 3)
    assert r.headers["ETag"] == '"3"'
    assert client.post(f"{url}/revisions/99/restore", headers={**team["editor"], "If-Match": '"3"'}).status_code == 404


def test_outsider_cannot_read_history(client, team):
    note = _note(client, team)
    assert client.get(f"{notes_url(team)}/{note['id']}/revisions", headers=team["outsider"]).status_code == 404


# ---- OIDC --------------------------------------------------------------------------------

@pytest.fixture
def idp(monkeypatch):
    """A fake identity provider: an RSA key, its JWKS on disk, and a token minter."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", use="sig", alg="RS256")
    keyset = jwt.PyJWKSet.from_dict({"keys": [jwk]})

    class FakeJWKSClient:  # stands in for PyJWKClient, which only fetches over http(s)
        def get_signing_key_from_jwt(self, token):
            kid = jwt.get_unverified_header(token).get("kid")
            try:
                return keyset[kid]
            except KeyError:
                raise jwt.PyJWKClientError(f"Unknown key {kid}") from None

    monkeypatch.setattr(config, "AUTH_MODE", "both")
    monkeypatch.setattr(config, "OIDC_ISSUER", "https://idp.example.com/")
    monkeypatch.setattr(config, "OIDC_AUDIENCE", "notes-api")
    monkeypatch.setattr(config, "OIDC_JWKS_URL", "https://idp.example.com/.well-known/jwks.json")
    monkeypatch.setattr(auth, "_jwks_client", lambda: FakeJWKSClient())

    def mint(sub="user-123", email="sso@example.com", aud="notes-api", exp_in=300, signer=key):
        claims = {"sub": sub, "email": email, "name": "SSO User", "iss": "https://idp.example.com/",
                  "aud": aud, "exp": int(time.time()) + exp_in}
        return {"Authorization": "Bearer " + jwt.encode(claims, signer, algorithm="RS256",
                                                         headers={"kid": "test-key"})}
    return mint


def test_oidc_user_is_provisioned_once(client, idp):
    first = client.get("/me", headers=idp())
    assert first.status_code == 200 and first.json()["email"] == "sso@example.com"
    assert client.get("/me", headers=idp()).json()["id"] == first.json()["id"]
    # An SSO user can work like anyone else.
    assert client.post("/teams", json={"name": "SSO team"}, headers=idp()).status_code == 201


def test_oidc_rejects_bad_tokens(client, idp):
    assert client.get("/me", headers=idp(aud="someone-else")).status_code == 401
    assert client.get("/me", headers=idp(exp_in=-60)).status_code == 401
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert client.get("/me", headers=idp(signer=forged)).status_code == 401


def test_oidc_does_not_take_over_an_api_key_account_by_email(client, idp):
    register(client, "taken@example.com")
    assert client.get("/me", headers=idp(email="taken@example.com")).status_code == 409


def test_signup_disabled_in_oidc_only_mode(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "oidc")
    assert client.post("/users", json={"email": "x@example.com", "name": "x"}).status_code == 403


# ---- attachments (S3 mocked with moto) -------------------------------------------------

@pytest.fixture
def bucket(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_DEFAULT_REGION": "us-west-2"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(config, "ATTACHMENTS_BUCKET", "notes-attachments-test")
    with mock_aws():
        storage.s3.cache_clear()
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(Bucket="notes-attachments-test",
                         CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        yield s3
    storage.s3.cache_clear()


def test_attachment_upload_complete_list_download(client, team, bucket):
    note = _note(client, team)
    base = f"{notes_url(team)}/{note['id']}/attachments"
    r = client.post(base, json={"filename": "diagram.png", "content_type": "image/png", "size_bytes": 5},
                    headers=team["editor"])
    assert r.status_code == 201, r.text
    att, upload = r.json()["attachment"], r.json()["upload"]
    assert att["status"] == "pending"
    key = upload["fields"]["key"]
    assert key == f"teams/{team['id']}/notes/{note['id']}/{att['id']}"  # no user-supplied name in the key
    policy = json.loads(base64.b64decode(upload["fields"]["policy"]))
    assert ["content-length-range", 5, 5] in policy["conditions"]  # S3 enforces the declared size

    assert client.post(f"{base}/{att['id']}/complete", headers=team["editor"]).status_code == 409  # not uploaded
    assert client.get(base, headers=team["viewer"]).json() == []  # pending files aren't listed

    bucket.put_object(Bucket="notes-attachments-test", Key=key, Body=b"12345")
    done = client.post(f"{base}/{att['id']}/complete", headers=team["editor"])
    assert done.status_code == 200 and done.json()["status"] == "ready"
    assert [a["filename"] for a in client.get(base, headers=team["viewer"]).json()] == ["diagram.png"]

    dl = client.get(f"{base}/{att['id']}/download", headers=team["viewer"], follow_redirects=False)
    assert dl.status_code == 307
    assert "X-Amz-Signature" in dl.headers["location"] and "attachment" in dl.headers["location"]


def test_attachment_rules(client, team, bucket, monkeypatch):
    note = _note(client, team)
    base = f"{notes_url(team)}/{note['id']}/attachments"
    body = {"filename": "a.txt", "content_type": "text/plain", "size_bytes": 10}
    assert client.post(base, json=body, headers=team["viewer"]).status_code == 403
    assert client.post(base, json=body, headers=team["outsider"]).status_code == 404
    assert client.post(base, json={**body, "size_bytes": config.ATTACHMENT_MAX_BYTES + 1},
                       headers=team["editor"]).status_code == 413
    assert client.post(base, json={**body, "filename": "../../etc/passwd"}, headers=team["editor"]).status_code == 422
    monkeypatch.setattr(config, "ATTACHMENTS_BUCKET", "")
    assert client.post(base, json=body, headers=team["editor"]).status_code == 503


# ---- operations ----------------------------------------------------------------------

def test_liveness_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_request_ids_and_metrics(client):
    r = client.get("/healthz")
    assert len(r.headers["X-Request-ID"]) == 32
    assert client.get("/healthz", headers={"X-Request-ID": "trace-abc.1"}).headers["X-Request-ID"] == "trace-abc.1"
    assert client.get("/healthz", headers={"X-Request-ID": "bad id\n"}).headers["X-Request-ID"] != "bad id\n"
    client.get("/teams")  # 401, still counted under its route template
    m = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/teams",status="401"}' in m


def test_token_bucket():
    b = TokenBucket(per_minute=2)
    assert b.take("k") == 0 and b.take("k") == 0
    assert b.take("k") > 0           # third request within the minute is refused
    assert b.take("other") == 0      # limits are per credential


def test_rate_limit_middleware_returns_429():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware, limiter=TokenBucket(per_minute=2))
    app.get("/ping")(lambda: {"ok": True})
    app.get("/healthz")(lambda: {"ok": True})
    c = TestClient(app)
    h = {"Authorization": "Bearer key-1"}
    assert [c.get("/ping", headers=h).status_code for _ in range(3)] == [200, 200, 429]
    assert int(c.get("/ping", headers=h).headers["Retry-After"]) >= 1
    assert c.get("/ping", headers={"Authorization": "Bearer key-2"}).status_code == 200
    assert all(c.get("/healthz", headers=h).status_code == 200 for _ in range(5))  # probes never limited
