"""The MCP server behind agent-guardrails: policy on every agent tool call, approvals, audit.

Real gate engine (agent_guardrails, in-process store) with this repo's policy file, the real
API in-process against PostgreSQL, and the MCP tool functions called the way the SDK calls them.
"""
import pytest
from agent_guardrails import Guard

from app import gate, mcp_server
from tests.test_api import client, notes_url, register, team  # noqa: F401  (fixtures)
from tests.test_mcp import FakeCtx, api  # noqa: F401  (fixture)


@pytest.fixture
def gated(api, monkeypatch):
    guard = Guard.local(gate.POLICY, agent="notes-mcp", on_pending="raise")
    g = gate.build(mcp_server.IMPLS, mcp_server._whoami, guard=guard)
    monkeypatch.setattr(mcp_server, "GATE", g)
    return guard


def _ctx(team, who):
    return FakeCtx({"authorization": team[who]["Authorization"]})


def _run_events(guard, run_id):
    return guard.client.gate.replay(run_id)


def test_reads_are_allowed_and_audited(gated, team):
    owner = _ctx(team, "owner")
    assert mcp_server.list_teams(owner)[0]["role"] == "owner"
    assert mcp_server.search_notes(team["id"], owner, query="x")["notes"] == []
    runs = gated.client.gate.runs()
    assert len(runs) == 1 and runs[0]["meta"]["email"] == "owner@example.com"
    replay = _run_events(gated, runs[0]["id"])
    steps = [e["step"] for e in replay["events"]]
    assert steps.count("call.allowed") == 2 and steps.count("call.result") == 2
    assert replay["chain"]["ok"] is True


def test_private_note_allowed_team_note_held_for_a_human(gated, client, team):
    owner = _ctx(team, "owner")
    private = mcp_server.create_note(team["id"], "Scratch", owner, body="mine", visibility="private")
    assert private["visibility"] == "private"

    held = mcp_server.create_note(team["id"], "Announcement", owner, body="Offsite moved to Friday.")
    assert held["status"] == "pending_approval"
    titles = [n["title"] for n in client.get(notes_url(team), headers=team["owner"]).json()["items"]]
    assert "Announcement" not in titles  # nothing written while pending

    again = mcp_server.run_approved(held["call_id"], owner)
    assert again["status"] == "pending_approval"  # still undecided

    gated.client.approve(held["call_id"], approver="tim@example.com")
    note = mcp_server.run_approved(held["call_id"], owner)
    assert note["title"] == "Announcement"
    with pytest.raises(gate.GateRefused, match="already executed"):
        mcp_server.run_approved(held["call_id"], owner)  # approved once, runs once


def test_rejected_call_never_runs(gated, client, team):
    owner = _ctx(team, "owner")
    held = mcp_server.create_note(team["id"], "Spam", owner, body="buy now")
    gated.client.reject(held["call_id"], approver="tim@example.com")
    with pytest.raises(gate.GateRefused, match="rejected"):
        mcp_server.run_approved(held["call_id"], owner)
    titles = [n["title"] for n in client.get(notes_url(team), headers=team["owner"]).json()["items"]]
    assert "Spam" not in titles


def test_approved_call_cannot_be_run_by_another_user(gated, client, team):
    held = mcp_server.create_note(team["id"], "Announcement", _ctx(team, "owner"), body="hi")
    gated.client.approve(held["call_id"], approver="tim@example.com")
    with pytest.raises(gate.GateRefused, match="no such call"):
        mcp_server.run_approved(held["call_id"], _ctx(team, "editor"))   # a teammate
    with pytest.raises(gate.GateRefused, match="no such call"):
        mcp_server.run_approved(held["call_id"], _ctx(team, "outsider"))
    assert mcp_server.run_approved(held["call_id"], _ctx(team, "owner"))["title"] == "Announcement"


def test_executor_rechecks_the_user_even_if_run_approved_is_bypassed(gated, client, team):
    """Second wall: the gated function itself refuses approved args that name another user."""
    held = mcp_server.create_note(team["id"], "Announcement", _ctx(team, "owner"), body="hi")
    gated.client.approve(held["call_id"], approver="tim@example.com")
    args = gated.client.get_call(held["call_id"], "notes-mcp")["args"]
    t = gate.current_ctx.set(_ctx(team, "editor"))
    try:
        with pytest.raises(gate.GateRefused, match="different user"):
            gated.execute(held["call_id"], args)
    finally:
        gate.current_ctx.reset(t)
    titles = [n["title"] for n in client.get(notes_url(team), headers=team["owner"]).json()["items"]]
    assert "Announcement" not in titles


def test_unlisted_tool_and_over_limit_calls_are_denied(gated, team):
    owner = _ctx(team, "owner")
    t = gate.current_ctx.set(owner)  # as if an MCP tool named delete_everything existed
    try:
        with pytest.raises(gate.GateRefused, match="denied by policy.*default"):
            mcp_server.GATE.call("delete_everything", {})
    finally:
        gate.current_ctx.reset(t)
    for i in range(10):
        mcp_server.create_note(team["id"], f"p{i}", owner, visibility="private")
    with pytest.raises(gate.GateRefused, match="denied by policy.*private-notes-allowed"):
        mcp_server.create_note(team["id"], "p10", owner, visibility="private")


def test_api_rules_still_apply_after_the_gate_allows(gated, team):
    """The gate allows reads by policy; the API still refuses an outsider. Two independent walls."""
    with pytest.raises(mcp_server.ApiError, match="404"):
        mcp_server.search_notes(team["id"], _ctx(team, "outsider"))
    with pytest.raises(mcp_server.ApiError, match="403"):
        mcp_server.create_note(team["id"], "x", _ctx(team, "viewer"), visibility="private")


def test_each_user_gets_their_own_run(gated, team):
    mcp_server.list_teams(_ctx(team, "owner"))
    mcp_server.list_teams(_ctx(team, "editor"))
    mcp_server.list_teams(_ctx(team, "owner"))
    emails = sorted(r["meta"]["email"] for r in gated.client.gate.runs())
    assert emails == ["editor@example.com", "owner@example.com"]


def test_gate_down_means_tool_does_not_run(gated, client, team, monkeypatch):
    from agent_guardrails.engine import GateError

    def down(*a, **k):
        raise GateError("connection refused", 503)
    monkeypatch.setattr(gated.client, "check", down)
    with pytest.raises(gate.GateRefused, match="did not run"):
        mcp_server.create_note(team["id"], "x", _ctx(team, "owner"), visibility="private")
    assert client.get(notes_url(team), headers=team["owner"]).json()["items"] == []


def test_bad_token_fails_before_the_gate_is_asked(gated, team):
    with pytest.raises(mcp_server.ApiError, match="401"):
        mcp_server.list_teams(FakeCtx({"authorization": "Bearer nts_not-a-real-key"}))
    assert gated.client.gate.runs() == []


def test_misconfiguration_fails_at_startup(monkeypatch):
    monkeypatch.setattr(gate, "MODE", "remote")
    monkeypatch.setattr(gate, "API_URL", "")
    with pytest.raises(RuntimeError, match="GUARDRAILS_API_URL"):
        gate.build({}, lambda: {})
    monkeypatch.setattr(gate, "MODE", "on")
    with pytest.raises(RuntimeError, match="off, local or remote"):
        gate.build({}, lambda: {})
    monkeypatch.setattr(gate, "MODE", "off")
    assert gate.build({}, lambda: {}) is None


def test_policy_file_is_valid_and_default_deny():
    from agent_guardrails.policy import load
    pol = load(gate.POLICY)
    assert pol.default == "deny"
    assert {r.tool for r in pol.rules} == set(mcp_server.IMPLS)
