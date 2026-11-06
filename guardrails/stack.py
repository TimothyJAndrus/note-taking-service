"""The notes MCP server's action gate: agent-guardrails ActionGate + the role the MCP server runs as.

  MCP server (role NotesMcpAgent) --SigV4--> gate API --policy.yaml--> allow | deny | hold for a human
                                                           |
                                                           +-- dashboard (Cognito): approve, reject, replay

The role can do exactly one thing: call the gate's agent routes. It is registered with the gate
as agent "notes-mcp", so the gate knows which policy rules apply and who is calling.

Who may assume the role (pick one; nobody by default):
  -c mcpTrustRoleArn=arn:aws:iam::<acct>:role/<role>   e.g. the notes service's ECS task role
  -c mcpTrustAccount=1                                 any principal in this account that IAM allows
                                                       (convenient for running the MCP server locally)
"""
from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct
from guardrails_cdk import ActionGate

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = "notes-mcp"


class NotesMcpGateStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, **kw) -> None:
        super().__init__(scope, cid, **kw)
        ctx = self.node.try_get_context
        emails = [e.strip() for e in str(ctx("approverEmails") or "").split(",") if e.strip()]
        dashboard = ctx("dashboardAssets")  # path to agent-guardrails' built dashboard (dashboard/dist), optional

        gate = ActionGate(self, "Gate", policy=os.path.join(HERE, "policy.yaml"),
                          approver_emails=emails or None,
                          dashboard_assets=dashboard if dashboard and os.path.isdir(dashboard) else None,
                          removal_policy=cdk.RemovalPolicy.DESTROY, log_retention=logs.RetentionDays.ONE_MONTH)

        trust_role = ctx("mcpTrustRoleArn")
        if trust_role:
            principal: iam.IPrincipal = iam.ArnPrincipal(trust_role)
        elif str(ctx("mcpTrustAccount") or "") in ("1", "true"):
            principal = iam.AccountRootPrincipal()
        else:
            # Assumable by nobody until configured. (IAM rejects a made-up account as a principal, so
            # trust this account under a condition no session can satisfy.)
            principal = iam.AccountRootPrincipal().with_conditions(
                {"StringEquals": {"aws:PrincipalTag/notes-mcp-trust": "configure-mcpTrustRoleArn"},
                 "Bool": {"aws:MultiFactorAuthPresent": "true", "aws:ViaAWSService": "true"}})
        role = iam.Role(self, "NotesMcpAgent", assumed_by=principal, max_session_duration=cdk.Duration.hours(1),
                        description="The notes MCP server's identity at the action gate (agent notes-mcp).")
        gate.register_agent(role, AGENT)

        cdk.CfnOutput(self, "McpAgentRoleArn", value=role.role_arn)
        cdk.CfnOutput(self, "GateApiUrlForMcp", value=gate.api_url)
