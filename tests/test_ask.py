"""/teams/{id}/ask: grounded answers with citations, and the permission rules retrieval must keep.

Runs against real PostgreSQL + pgvector with AI_MODE=fake (deterministic offline embeddings and
a stand-in model), so these tests check the retrieval, isolation and citation logic, not model
quality. Model quality is measured separately by the evals in evals/.
"""
import psycopg
import pytest

from app import ai, ask, config, repo
from tests.test_api import TEST_DB, client, notes_url, register, team  # noqa: F401  (fixtures)

ASK = "/teams/{}/ask"


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "fake")


def _note(client, team, who="editor", **kw):
    r = client.post(notes_url(team), json={"title": "Note", "body": "", **kw}, headers=team[who])
    assert r.status_code == 201, r.text
    return r.json()


def _ask(client, team, who, q, **kw):
    return client.post(ASK.format(team["id"]), json={"question": q, **kw}, headers=team[who])


def _cited_ids(resp):
    return {c["note_id"] for c in resp.json()["citations"]}


def test_answer_cites_note_id_and_version(client, team):
    note = _note(client, team, title="Deploy checklist", body="Run database migrations before the rollout.")
    _note(client, team, title="Lunch", body="Tacos on Friday.")
    r = _ask(client, team, "viewer", "What do we run before the rollout?")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grounded"] is True
    assert body["citations"][0]["note_id"] == note["id"]
    assert body["citations"][0]["version"] == 1
    assert "[S1]" in body["answer"] and "migrations" in body["answer"]
    assert r.headers["Cache-Control"] == "no-store"


def test_outsider_gets_404_and_other_teams_never_leak(client, team):
    _note(client, team, title="Payroll", body="The payroll bank account number is 12345.")
    assert _ask(client, team, "outsider", "payroll bank account").status_code == 404
    # The outsider's own team asks the same question: nothing from the first team comes back.
    other = client.post("/teams", json={"name": "Other"}, headers=team["outsider"]).json()
    _note(client, {"id": other["id"], "editor": team["outsider"]}, title="Picnic", body="Bring a blanket.")
    r = client.post(ASK.format(other["id"]), json={"question": "payroll bank account number"},
                    headers=team["outsider"])
    assert r.status_code == 200
    assert "12345" not in r.text and "Payroll" not in r.text


def test_rls_blocks_chunks_even_if_the_query_names_the_wrong_team(client, team):
    """Defense in depth: call retrieval directly as the outsider, pointed at the victim team."""
    _note(client, team, title="Payroll", body="The payroll bank account number is 12345.")
    outsider_id = client.get("/me", headers=team["outsider"]).json()["id"]
    owner_id = client.get("/me", headers=team["owner"]).json()["id"]
    qvec = ai.to_pgvector(ai.embed("payroll bank account"))
    with psycopg.connect(TEST_DB, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("SET ROLE notes_app")  # exactly what the pool does
        conn.execute("SELECT set_config('app.user_id', %s, false)", (owner_id,))
        assert repo.search_chunks(conn, team["id"], owner_id, query="payroll", query_vec=qvec, k=5)
        conn.execute("SELECT set_config('app.user_id', %s, false)", (outsider_id,))
        assert repo.search_chunks(conn, team["id"], outsider_id, query="payroll", query_vec=qvec, k=5) == []
        assert conn.execute("SELECT count(*) AS n FROM note_chunks").fetchone()["n"] == 0  # unfiltered


def test_private_notes_only_inform_their_author(client, team):
    private = _note(client, team, title="Salary", body="My salary target is 250k.", visibility="private")
    r = _ask(client, team, "viewer", "What is the salary target?")
    assert private["id"] not in _cited_ids(r) and "250k" not in r.text
    assert r.json()["grounded"] is False
    r = _ask(client, team, "editor", "What is the salary target?")  # the author
    assert _cited_ids(r) == {private["id"]}


def test_edits_and_deletes_are_never_cited_stale(client, team):
    note = _note(client, team, title="Oncall", body="The oncall phone number is 555-0100.")
    r = client.patch(f"{notes_url(team)}/{note['id']}", json={"body": "The oncall rotation uses PagerDuty now."},
                     headers={**team["editor"], "If-Match": '"1"'})
    assert r.status_code == 200
    r = _ask(client, team, "viewer", "oncall phone number")
    assert "555-0100" not in r.text
    assert [c["version"] for c in r.json()["citations"]] == [2]
    assert client.delete(f"{notes_url(team)}/{note['id']}", headers=team["editor"]).status_code == 204
    r = _ask(client, team, "viewer", "oncall rotation PagerDuty")
    assert r.json()["citations"] == [] and r.json()["answer"] == ask.NOT_FOUND


def test_stale_chunks_still_in_the_table_are_never_retrieved(client, team, monkeypatch):
    """The version join, not cleanup, is what keeps old text out: freeze indexing so v1 chunks linger."""
    note = _note(client, team, title="Oncall", body="The oncall phone number is 555-0100.")
    monkeypatch.setattr(ask, "index_after_write", lambda *a: None)  # background re-index never runs
    monkeypatch.setattr(config, "ASK_CATCHUP_LIMIT", 0)              # nor does catch-up
    client.patch(f"{notes_url(team)}/{note['id']}", json={"body": "Rotation is in PagerDuty."},
                 headers={**team["editor"], "If-Match": '"1"'})
    with psycopg.connect(TEST_DB) as conn:
        assert conn.execute("SELECT count(*) FROM note_chunks WHERE version = 1").fetchone()[0] == 1
    r = _ask(client, team, "viewer", "oncall phone number")
    assert "555-0100" not in r.text and r.json()["citations"] == []
    assert r.json()["index"]["pending"] == 1  # and the API says the edit isn't searchable yet


def test_stale_indexer_cannot_overwrite_newer_chunks(client, team):
    note = _note(client, team, title="Doc", body="first version")
    with psycopg.connect(TEST_DB, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        stale = {"id": note["id"], "team_id": note["team_id"], "title": "Doc", "body": "first version", "version": 1}
        client.patch(f"{notes_url(team)}/{note['id']}", json={"body": "second version"},
                     headers={**team["editor"], "If-Match": '"1"'})
        assert ask.index_note(conn, stale) is False
        assert [r["version"] for r in conn.execute("SELECT version FROM note_chunks").fetchall()] == [2]


def test_invented_citations_are_removed(client, team, monkeypatch):
    note = _note(client, team, title="Backups", body="Backups run nightly at 2am.")
    monkeypatch.setattr(ai, "generate", lambda q, s: {
        "text": "Backups run nightly [S1]. They are encrypted [S7].", "input_tokens": 1, "output_tokens": 1})
    body = _ask(client, team, "viewer", "When do backups run?").json()
    assert body["answer"] == "Backups run nightly [S1]. They are encrypted."
    assert [c["note_id"] for c in body["citations"]] == [note["id"]]


def test_uncited_answer_is_not_grounded(client, team, monkeypatch):
    _note(client, team, title="Backups", body="Backups run nightly at 2am.")
    monkeypatch.setattr(ai, "generate", lambda q, s: {"text": "Probably weekly.", "input_tokens": 1,
                                                       "output_tokens": 1})
    body = _ask(client, team, "viewer", "When do backups run?").json()
    assert body["grounded"] is False and body["citations"] == []


def test_not_found_followed_by_chatter_is_returned_as_a_plain_refusal(client, team, monkeypatch):
    """Seen live: 'I can't find that... While S1 mentions 250k...'. The refusal must not carry content."""
    _note(client, team, title="Comp", body="Target is 250k.")
    monkeypatch.setattr(ai, "generate", lambda q, s: {
        "text": f"{ask.NOT_FOUND}\n\nWhile [S1] mentions 250k, it is private.", "input_tokens": 1,
        "output_tokens": 1})
    body = _ask(client, team, "viewer", "What is the target?").json()
    assert body == {**body, "answer": ask.NOT_FOUND, "citations": [], "grounded": False}


def test_model_outage_never_breaks_writes_and_ask_catches_up(client, team, monkeypatch):
    def down(_text):
        raise ai.ModelUnavailable("down")
    monkeypatch.setattr(ai, "embed", down)
    note = _note(client, team, title="Runbook", body="Restart the worker with systemctl.")  # write still works
    r = _ask(client, team, "viewer", "How do I restart the worker?")
    assert r.status_code == 503 and r.headers["Retry-After"]
    monkeypatch.undo()
    monkeypatch.setattr(config, "AI_MODE", "fake")
    r = _ask(client, team, "viewer", "How do I restart the worker?")
    assert r.status_code == 200
    assert r.json()["index"] == {"pending": 0, "indexed_now": 1}
    assert _cited_ids(r) == {note["id"]}


def test_ask_disabled_rate_limited_and_validated(client, team, monkeypatch):
    assert _ask(client, team, "viewer", "hi").status_code == 422          # too short
    assert _ask(client, team, "viewer", "what?", top_k=50).status_code == 422
    from app import main
    from app.observability import TokenBucket
    monkeypatch.setattr(main, "_ask_limiter", TokenBucket(per_minute=1))
    assert _ask(client, team, "viewer", "anything here?").status_code == 200
    r = _ask(client, team, "viewer", "anything here?")
    assert r.status_code == 429 and r.headers["Retry-After"]
    monkeypatch.setattr(config, "AI_MODE", "off")
    assert _ask(client, team, "viewer", "anything here?").status_code == 503


def test_reindex_backfills_every_team(client, team, monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "off")  # writes while AI is off: nothing indexed
    _note(client, team, title="A", body="alpha")
    _note(client, team, title="B", body="beta")
    monkeypatch.setattr(config, "AI_MODE", "fake")
    with psycopg.connect(TEST_DB, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        assert repo.count_notes_needing_index(conn, team["id"]) == 2
        assert ask.index_pending(conn, None, 100) == 2
        assert ask.index_pending(conn, None, 100) == 0  # idempotent


def test_chunking():
    assert ai.chunk("Title", "") == ["Title"]
    long = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(10))
    chunks = ai.chunk("T", long, size=1500)
    assert len(chunks) > 1 and all(len(c) <= 1500 for c in chunks)
    assert "Paragraph 0." in chunks[0] and "Paragraph 9." in chunks[-1]
    assert all(len(c) <= 1500 for c in ai.chunk("T", "x" * 10_000))
