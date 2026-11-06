# note-taking-service — AWS CDK infra

TypeScript CDK app (`aws-cdk-lib` 2.27x, cdk-nag `AwsSolutionsChecks`) that deploys the service as stack **`NoteServiceStack`** (default region `us-west-2`).

## What gets created

| Area | Resources |
|---|---|
| Network | VPC across 2 AZs: public, private-with-egress (app) and isolated (DB) subnets; 1 NAT gateway; S3 gateway endpoint; VPC flow logs → CloudWatch (30 d) |
| Compute | ECS cluster (Container Insights), Fargate **arm64** service: 0.5 vCPU / 1 GB, 2–6 tasks, autoscaling on CPU 60 % + requests/target, circuit breaker with rollback, read-only root FS (`/tmp` volume), ECS Exec off |
| Migrations | Separate one-off task definition (same image, `python -m app.migrate`), own SG; the stack outputs a ready-to-run `aws ecs run-task` command |
| Edge | Internet-facing ALB (HTTP, or HTTPS + 80→443 redirect when a cert is supplied), `/metrics` returns a fixed 404, health check `/readyz`, access logs → S3; WAFv2 WebACL (Common, KnownBadInputs, SQLi, 1000 req/5 min per IP) |
| Database | RDS PostgreSQL 17, `db.t4g.medium`, Multi-AZ, gp3 20→100 GB autoscaling, encrypted, `rds.force_ssl=1`, 7-day backups, Performance Insights, Enhanced Monitoring, `postgresql` logs → CloudWatch, deletion protection, snapshot on delete; credentials generated in Secrets Manager |
| Storage | Attachments bucket (versioned, SSE-S3, TLS-only, no public access, CORS PUT/GET, multipart cleanup 7 d, noncurrent versions 90 d, retained); access-logs bucket (90-day expiry, retained) |
| Ops | CloudWatch alarms: 5xx count, p95 latency, RDS CPU, RDS free storage, running tasks < 2; optional SNS email topic |

## Estimated monthly cost (rough estimate, us-west-2, on-demand, light traffic)

**About $190–$260/month.** Main items: RDS t4g.medium Multi-AZ + storage/PI (~$100–120), NAT gateway (~$35 + data), ALB (~$20–25), Fargate 2× arm64 0.5 vCPU/1 GB (~$30), WAF (~$10 + requests), CloudWatch logs/metrics/Container Insights (~$5–20). Single-AZ DB (`-c dbMultiAz=false`) saves about $50. This is an estimate; check the AWS Pricing Calculator for your traffic.

## Context flags

| Key | Default | Notes |
|---|---|---|
| `environment` | `prod` | `Environment` tag; `prod` turns on termination protection |
| `dbInstanceType` | `t4g.medium` | Graviton class recommended |
| `dbMultiAz` | `true` | |
| `certificateArn`, `domainName` | empty | Set **both** to enable HTTPS. Create the DNS record for `domainName` yourself → `LoadBalancerDns` output |
| `oidcIssuer`, `oidcAudience`, `oidcJwksUrl` | empty | With no issuer, `AUTH_MODE` is forced to `api_key` |
| `authMode` | `both` | Only used when `oidcIssuer` is set |
| `corsOrigin` | `*` | Restrict this to your web origin |
| `alarmEmail` | empty | Creates an SNS topic + email subscription (confirm the email) |
| `aiMode` | `off` | `bedrock` turns on `POST /teams/{id}/ask` and grants the task role `bedrock:InvokeModel` on the two models below only. Enable model access in the Bedrock console first. After the first deploy with it on, backfill existing notes once: run the migrate task with command `python -m app.reindex`. |
| `embedModelId` | `amazon.titan-embed-text-v2:0` | Must produce 1024-dim vectors (`migrations/0003_ask.sql`) |
| `chatModelId` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Any Converse-capable model or inference profile |

## Deploy

```bash
cd infra
NODE_ENV=development npm install
export AWS_PROFILE=Tian001            # or whatever profile you use
npx cdk bootstrap aws://<ACCOUNT>/us-west-2      # once per account/region
npx cdk diff
npx cdk deploy \
  -c certificateArn=arn:aws:acm:us-west-2:<ACCOUNT>:certificate/<id> \
  -c domainName=notes.example.com \
  -c oidcIssuer=https://<issuer>/ -c oidcAudience=<aud> -c oidcJwksUrl=https://<issuer>/.well-known/jwks.json \
  -c corsOrigin=https://app.example.com -c alarmEmail=ops@example.com
```

Docker must be running: deploy builds the arm64 image from the repo root `Dockerfile`.

**Migrations run automatically.** Each app task starts with a short-lived `migrate` init container (`python -m app.migrate`); the app container starts only if it exits 0. Running it from several tasks at once is safe: an advisory lock means each migration file is applied exactly once. So the first deploy and later schema changes need no manual step. Keep schema changes backward compatible (expand, then contract), because old tasks keep serving while new ones start.

To run migrations ahead of a rollout anyway, use the one-off task:

```bash
CMD=$(aws cloudformation describe-stacks --stack-name NoteServiceStack \
  --query "Stacks[0].Outputs[?OutputKey=='MigrationRunTaskCommand'].OutputValue" --output text)
TASK=$(eval "$CMD")
aws ecs wait tasks-stopped --cluster <ClusterName> --tasks "$TASK"
aws ecs describe-tasks --cluster <ClusterName> --tasks "$TASK" --query 'tasks[0].containers[0].exitCode'   # expect 0
```

**Create the first user.**
- With OIDC configured (`-c oidcIssuer=...`): users are created automatically on first sign-in with an IdP token.
- API-key mode: `POST /users` returns a key once (`curl -X POST https://<alb>/users -H 'content-type: application/json' -d '{"email":"you@example.com","name":"You"}'`). Then set `ALLOW_API_KEY_SIGNUP=false` in the task environment if you don't want open self-registration.

## Teardown

- RDS has **deletion protection**. Disable it first: `aws rds modify-db-instance --db-instance-identifier <id> --no-deletion-protection --apply-immediately`. In `prod` the stack also has **termination protection**: `aws cloudformation update-termination-protection --no-enable-termination-protection --stack-name NoteServiceStack`.
- `cdk destroy` takes a **final DB snapshot** (RemovalPolicy.SNAPSHOT). Delete it manually when you no longer need it.
- The attachments and access-log buckets are **retained** and keep costing money until you empty and delete them by hand (versioned: delete all versions).

## Security posture

- Tasks and DB sit in private subnets. The DB is in isolated subnets with no route out, and port 5432 is open only to the service and migration SGs.
- TLS to the DB is enforced (`rds.force_ssl=1`, `DB_SSLMODE=require`). Storage is encrypted at rest (RDS, S3). The S3 bucket policies reject non-TLS requests.
- DB credentials are generated into Secrets Manager and injected as ECS secrets, never as plaintext env vars. Automatic rotation is **off** because tasks read credentials only at start; rotate manually, then redeploy.
- The task role can read/write the attachments bucket and nothing else. The execution role has only ECR pull, logs, and the DB secret.
- Containers run non-root with a read-only root FS. ECS Exec is disabled.
- At the edge: the WAF managed rule sets plus a per-IP rate limit (the app adds per-credential limits). The ALB drops invalid headers. `/metrics` is blocked publicly.
- **HTTP-only unless `certificateArn` + `domainName` are set.** Synth prints a warning and the stack emits an `HttpsWarning` output.
- Logging: VPC flow logs, ALB and S3 access logs, app logs (JSON, 30 d), and Postgres logs. CloudWatch alarms are optionally wired to SNS email.
- cdk-nag (AwsSolutions) runs on every synth. Remaining suppressions are documented inline in `lib/note-service-stack.ts`: public ALB SG, log-bucket self-logging, secret rotation, default DB port, non-secret env vars, S3 object wildcards from `grantReadWrite`, `ecr:GetAuthorizationToken`, and CDK-managed helper roles.
