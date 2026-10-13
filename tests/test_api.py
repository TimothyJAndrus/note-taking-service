"""End-to-end API tests: each test drives the real app against a fresh SQLite file."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_DB", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:  # runs startup, which creates the schema
        yield c


def register(client, email):
    r = client.post("/users", json={"email": email, "name": email.split("@")[0]})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


@pytest.fixture
def team(client):
    """A team with an owner, an editor, a viewer, and an outsider."""
    owner, editor = register(client, "owner@example.com"), register(client, "editor@example.com")
    viewer, outsider = register(client, "viewer@example.com"), register(client, "outsider@example.com")
    team_id = client.post("/teams", json={"name": "Platform"}, headers=owner).json()["id"]
    for email, role in [("editor@example.com", "editor"), ("viewer@example.com", "viewer")]:
        assert client.put(f"/teams/{team_id}/members", json={"email": email, "role": role},
                          headers=owner).status_code == 200
    return {"id": team_id, "owner": owner, "editor": editor, "viewer": viewer, "outsider": outsider}


def notes_url(team):
    return f"/teams/{team['id']}/notes"


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_requires_auth(client, team):
    assert client.get(notes_url(team)).status_code == 401
    assert client.get(notes_url(team), headers={"Authorization": "Bearer nope"}).status_code == 401


def test_duplicate_email_rejected(client):
    register(client, "a@example.com")
    assert client.post("/users", json={"email": "A@example.com", "name": "a"}).status_code == 409


def test_create_get_and_list_note(client, team):
    r = client.post(notes_url(team), json={"title": "Runbook", "body": "Restart the API", "tags": ["Ops", "ops", " runbook "]},
                    headers=team["editor"])
    assert r.status_code == 201
    note = r.json()
    assert note["tags"] == ["ops", "runbook"]  # trimmed, lower-cased, de-duplicated
    assert r.headers["ETag"] == '"1"'
    got = client.get(f"{notes_url(team)}/{note['id']}", headers=team["viewer"])
    assert got.status_code == 200 and got.json()["title"] == "Runbook"
    page = client.get(notes_url(team), headers=team["owner"]).json()
    assert [n["id"] for n in page["items"]] == [note["id"]] and page["next_cursor"] is None


def test_outsider_cannot_see_team(client, team):
    # 404 rather than 403: outsiders can't even confirm the team exists.
    assert client.get(notes_url(team), headers=team["outsider"]).status_code == 404
    assert client.post(notes_url(team), json={"title": "x"}, headers=team["outsider"]).status_code == 404


def test_viewer_is_read_only(client, team):
    assert client.post(notes_url(team), json={"title": "x"}, headers=team["viewer"]).status_code == 403


def test_only_owner_manages_members(client, team):
    r = client.put(f"/teams/{team['id']}/members", json={"email": "outsider@example.com"}, headers=team["editor"])
    assert r.status_code == 403


def test_private_notes_are_author_only(client, team):
    note = client.post(notes_url(team), json={"title": "Draft", "visibility": "private"}, headers=team["editor"]).json()
    assert client.get(f"{notes_url(team)}/{note['id']}", headers=team["owner"]).status_code == 404
    assert client.get(notes_url(team), headers=team["owner"]).json()["items"] == []
    assert client.get(f"{notes_url(team)}/{note['id']}", headers=team["editor"]).status_code == 200


def test_update_requires_current_version(client, team):
    note = client.post(notes_url(team), json={"title": "v1"}, headers=team["editor"]).json()
    url = f"{notes_url(team)}/{note['id']}"
    assert client.patch(url, json={"title": "no etag"}, headers=team["editor"]).status_code == 428
    ok = client.patch(url, json={"title": "v2", "tags": ["x"]}, headers={**team["editor"], "If-Match": '"1"'})
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.json()["tags"] == ["x"]
    assert ok.headers["ETag"] == '"2"'
    # A second writer still holding version 1 is rejected instead of silently overwriting.
    stale = client.patch(url, json={"title": "lost update"}, headers={**team["owner"], "If-Match": '"1"'})
    assert stale.status_code == 412
    assert client.get(url, headers=team["owner"]).json()["title"] == "v2"


def test_only_author_can_make_note_private(client, team):
    note = client.post(notes_url(team), json={"title": "shared"}, headers=team["editor"]).json()
    r = client.patch(f"{notes_url(team)}/{note['id']}", json={"visibility": "private"},
                     headers={**team["owner"], "If-Match": '"1"'})
    assert r.status_code == 403


def test_search_and_tag_filter(client, team):
    h = team["editor"]
    client.post(notes_url(team), json={"title": "Postgres failover", "body": "Promote the replica", "tags": ["db"]}, headers=h)
    client.post(notes_url(team), json={"title": "Lunch", "body": "Tacos on Friday", "tags": ["social"]}, headers=h)
    hits = client.get(notes_url(team), params={"q": "replica"}, headers=h).json()["items"]
    assert [n["title"] for n in hits] == ["Postgres failover"]
    tagged = client.get(notes_url(team), params={"tag": "social"}, headers=h).json()["items"]
    assert [n["title"] for n in tagged] == ["Lunch"]
    # FTS operators in user input are treated as plain words, not syntax errors.
    assert client.get(notes_url(team), params={"q": 'AND "( NEAR'}, headers=h).status_code == 200


def test_search_reflects_edits(client, team):
    h = team["editor"]
    note = client.post(notes_url(team), json={"title": "alpha"}, headers=h).json()
    client.patch(f"{notes_url(team)}/{note['id']}", json={"title": "bravo"}, headers={**h, "If-Match": '"1"'})
    assert client.get(notes_url(team), params={"q": "alpha"}, headers=h).json()["items"] == []
    assert len(client.get(notes_url(team), params={"q": "bravo"}, headers=h).json()["items"]) == 1


def test_cursor_pagination_walks_every_note_once(client, team):
    h = team["editor"]
    created = [client.post(notes_url(team), json={"title": f"n{i}"}, headers=h).json()["id"] for i in range(7)]
    seen, cursor = [], None
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = client.get(notes_url(team), params=params, headers=h).json()
        seen += [n["id"] for n in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == list(reversed(created))  # newest first, no duplicates, none missing
    assert client.get(notes_url(team), params={"cursor": "!!bad!!"}, headers=h).status_code == 400


def test_delete_rules_and_soft_delete(client, team):
    note = client.post(notes_url(team), json={"title": "temp"}, headers=team["owner"]).json()
    url = f"{notes_url(team)}/{note['id']}"
    assert client.delete(url, headers=team["editor"]).status_code == 403   # not the author, not an owner
    mine = client.post(notes_url(team), json={"title": "mine"}, headers=team["editor"]).json()
    assert client.delete(f"{notes_url(team)}/{mine['id']}", headers=team["editor"]).status_code == 204
    assert client.delete(url, headers=team["owner"]).status_code == 204
    assert client.get(url, headers=team["owner"]).status_code == 404
    assert client.get(notes_url(team), headers=team["owner"]).json()["items"] == []


def test_validation_errors(client, team):
    h = team["editor"]
    assert client.post(notes_url(team), json={"title": ""}, headers=h).status_code == 422
    assert client.post(notes_url(team), json={"title": "x", "tags": [f"t{i}" for i in range(21)]}, headers=h).status_code == 422
    assert client.get(notes_url(team), params={"limit": 0}, headers=h).status_code == 422
