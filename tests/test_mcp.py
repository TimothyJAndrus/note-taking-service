"""MCP server: tools call the real API with the caller's own token, and nothing else.

The API runs in-process (TestClient) against real PostgreSQL; the MCP tools' HTTP client is
pointed at it. AI_MODE=fake for the ask tool.
"""
import pytest

from app import config, mcp_server
from tests.test_api import client, notes_url, register, team  # noqa: F401  (fixtures)


class FakeCtx:
    """What the MCP SDK hands a tool: request headers (HTTP transport) or None (stdio)."""

    def __init__(self, headers):
        self.headers = headers


@pytest.fixture
def api(client, monkeypatch):
    monkeypatch.setattr(config, "AI_MODE", "fake")

    class Bound:  # httpx.Client stand-in that sends requests to the in-process app
        def __init__(self, base_url, timeout):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mcp_server.httpx, "Client", Bound)
    monkeypatch.delenv("NOTES_API_TOKEN", raising=False)
    return client


def _bearer(headers):
    return headers["Authorization"][7:]


def test_tools_act_as_the_caller(api, team):
    owner = FakeCtx({"authorization": team["owner"]["Authorization"]})
    teams = mcp_server.list_teams(owner)
    assert [t["role"] for t in teams] == ["owner"]
    note = mcp_server.create_note(team["id"], "Release process", owner, body="Releases ship on Tuesdays.")
    assert mcp_server.read_note(team["id"], note["id"], owner)["body"] == "Releases ship on Tuesdays."
    assert mcp_server.search_notes(team["id"], owner, query="tuesdays")["notes"][0]["id"] == note["id"]
    out = mcp_server.ask(team["id"], "When do releases ship?", owner)
    assert out["grounded"] and out["citations"][0]["note_id"] == note["id"]


def test_api_rules_apply_to_agents(api, team):
    viewer = FakeCtx({"authorization": team["viewer"]["Authorization"]})
    outsider = FakeCtx({"authorization": team["outsider"]["Authorization"]})
    with pytest.raises(mcp_server.ApiError, match="403"):
        mcp_server.create_note(team["id"], "x", viewer)               # viewers can't write
    with pytest.raises(mcp_server.ApiError, match="404"):
        mcp_server.search_notes(team["id"], outsider)                 # outsiders can't see the team
    with pytest.raises(mcp_server.ApiError, match="404"):
        mcp_server.ask(team["id"], "anything at all?", outsider)


def test_http_requests_never_fall_back_to_the_server_token(api, team, monkeypatch):
    """A shared HTTP server started with NOTES_API_TOKEN must not lend that identity to anonymous callers."""
    monkeypatch.setenv("NOTES_API_TOKEN", _bearer(team["owner"]))
    with pytest.raises(mcp_server.ApiError, match="Not signed in"):
        mcp_server.list_teams(FakeCtx({}))                             # HTTP request, no Authorization
    with pytest.raises(mcp_server.ApiError, match="Not signed in"):
        mcp_server.list_teams(FakeCtx({"authorization": "Bearer "}))  # empty bearer
    assert mcp_server.list_teams(FakeCtx(None))[0]["role"] == "owner"  # stdio: the local user's token
