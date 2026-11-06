#!/usr/bin/env python3
"""CDK app: the action gate for the notes MCP server (stack NotesMcpGate).

Built from agent-guardrails' own ActionGate construct, with guardrails/policy.yaml.

    cd guardrails
    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    npx cdk deploy -c approverEmails=you@example.com [-c mcpTrustAccount=1] [-c mcpTrustRoleArn=arn:...]

Outputs the gate API URL, the dashboard URL, and the role ARN the MCP server must run as.
"""
import os

import aws_cdk as cdk

from stack import NotesMcpGateStack

app = cdk.App()
NotesMcpGateStack(
    app, "NotesMcpGate",
    env=cdk.Environment(account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
                        region=app.node.try_get_context("region") or "us-west-2"),
    description="Action gate (agent-guardrails) for the note-taking-service MCP server.",
)
app.synth()
