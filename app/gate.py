"""Action gate for the MCP server: every agent tool call is checked by agent-guardrails first.

The notes API already limits an agent to what its user may do. The gate adds the other half:
what this *agent* may do, and who approved it. Each call is allowed, denied, or held for a
human (policy: guardrails/policy.yaml), and every step lands in the gate's hash-chained audit
trail, replayable from its dashboard.

  GUARDRAILS_MODE=off      no gate (default; the MCP server behaves as before)
  GUARDRAILS_MODE=local    in-process gate with the policy file, memory store (dev and tests)
  GUARDRAILS_MODE=remote   the deployed gate API (GUARDRAILS_API_URL), signed with SigV4 using this
                           process's AWS credentials, which must be the role registered as
                           GUARDRAILS_AGENT in the gate stack (guardrails/stack.py)

Fails closed: if the gate is unreachable or errors, the tool does not run.

One gate "run" per user per server process, rotated every GUARDRAILS_RUN_SECONDS, so per-run
limits (max_per_run, max_calls_per_run) apply per user session. Every call's arguments include
`acting_for` (the user's email and id): approvers see whose session it came from, and an approved
call can only be executed by that same user.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver.exceptions import ToolError

MODE = os.environ.get("GUARDRAILS_MODE", "off").lower()
API_URL = os.environ.get("GUARDRAILS_API_URL", "")
AGENT = os.environ.get("GUARDRAILS_AGENT", "notes-mcp")
POLICY = os.environ.get("GUARDRAILS_POLICY", str(Path(__file__).resolve().parent.parent / "guardrails" / "policy.yaml"))
RUN_SECONDS = int(os.environ.get("GUARDRAILS_RUN_SECONDS", "3600"))

current_ctx: ContextVar[Any] = ContextVar("notes_mcp_ctx", default=None)  # the MCP request context


class GateRefused(ToolError):
    """The gate said no, or couldn't be reached. Shown to the agent as a tool error."""


class ToolGate:
    def __init__(self, guard, tools: dict[str, Callable], whoami: Callable[[], dict]):
        self.guard = guard
        self.whoami = whoami  # returns {"id", "email"} for the current caller (validates the token too)
        self._runs: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        for name, fn in tools.items():
            guard.tool(name=name)(self._bind(fn))

    # The gate passes the approved arguments to the function. `acting_for` is checked, then dropped.
    def _bind(self, fn):
        def impl(acting_for: str, **args):
            if acting_for != self._label(self.whoami()):
                raise GateRefused("this call belongs to a different user")
            return fn(**args)
        return impl

    @staticmethod
    def _label(user: dict) -> str:
        return f"{user['email']} ({user['id']})"

    def _run_for(self, user: dict) -> str:
        key = hashlib.sha256(str(user["id"]).encode()).hexdigest()
        now = time.time()
        with self._lock:
            run_id, started = self._runs.get(key, ("", 0.0))
            if run_id and now - started < RUN_SECONDS:
                return run_id
            if run_id:
                try:
                    self.guard.client.end_run(run_id, AGENT, "finished")
                except Exception:  # an old run that can't be closed must not block new work
                    pass
            run = self.guard.client.start_run(AGENT, f"MCP session for {user['email']}",
                                              {"user_id": str(user["id"]), "email": user["email"], "via": "mcp"})
            self._runs[key] = (run["id"], now)
            return run["id"]

    def call(self, tool: str, args: dict) -> Any:
        from agent_guardrails.engine import GateError

        user = self.whoami()
        gated_args = {**args, "acting_for": self._label(user)}
        try:
            run_id = self._run_for(user)
        except (GateError, OSError) as e:
            raise GateRefused(f"action gate unavailable, so {tool} did not run: {e}") from None
        with self.guard.run(run_id=run_id):
            out = self.guard.call_tool(tool, gated_args)
        status = out["status"]
        if status == "ok":
            return out["result"]
        if status == "pending_approval":
            return {"status": "pending_approval", "call_id": out["call"],
                    "message": f"{tool} needs a human's approval and has NOT run. Tell the user it is waiting "
                               f"for approval. After it is approved, call run_approved with this call_id."}
        if status == "denied":
            raise GateRefused(f"denied by policy: {out['error']}")
        raise GateRefused(f"action gate error, so {tool} did not run: {out.get('error', status)}")

    def run_approved(self, call_id: str) -> Any:
        """Execute a call a human approved. Only the user it was made for can execute it, once."""
        from agent_guardrails.engine import GateError
        from agent_guardrails.sdk import ApprovalPending, Denied, Expired, Rejected

        user = self.whoami()
        try:
            call = self.guard.client.get_call(call_id, AGENT)
        except GateError:
            raise GateRefused("no such call") from None
        if (call.get("args") or {}).get("acting_for") != self._label(user):
            raise GateRefused("no such call")  # someone else's call: don't confirm it exists
        try:
            return self.guard.execute(call_id, call["args"])
        except ApprovalPending:
            return {"status": "pending_approval", "call_id": call_id, "message": "still waiting for approval"}
        except Rejected:
            raise GateRefused(f"{call['tool']} was rejected by an approver; it will not run") from None
        except (Expired, Denied) as e:
            raise GateRefused(f"{call['tool']} will not run: {e}") from None
        except GateError as e:  # e.g. already executed (409)
            raise GateRefused(f"{call['tool']} will not run: {e}") from None


def build(tools: dict[str, Callable], whoami: Callable[[], dict], guard=None) -> ToolGate | None:
    """The gate for this process, or None when GUARDRAILS_MODE=off. Misconfiguration fails at startup."""
    if guard is None:
        if MODE == "off":
            return None
        from agent_guardrails import Guard
        if MODE == "local":
            guard = Guard.local(POLICY, agent=AGENT, on_pending="raise")
        elif MODE == "remote":
            if not API_URL:
                raise RuntimeError("GUARDRAILS_MODE=remote needs GUARDRAILS_API_URL")
            guard = Guard.remote(API_URL, agent=AGENT, on_pending="raise")
        else:
            raise RuntimeError(f"GUARDRAILS_MODE must be off, local or remote (got {MODE!r})")
    return ToolGate(guard, tools, whoami)
