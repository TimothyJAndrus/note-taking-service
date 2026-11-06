"""All configuration comes from environment variables (12-factor), read once at import."""
import os


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def database_url() -> str:
    """DATABASE_URL wins; otherwise build it from DB_* parts (how ECS injects Secrets Manager fields)."""
    if url := os.environ.get("DATABASE_URL"):
        return url
    if host := os.environ.get("DB_HOST"):
        from psycopg.conninfo import make_conninfo
        return make_conninfo(host=host, port=os.environ.get("DB_PORT", "5432"),
                             dbname=os.environ.get("DB_NAME", "notes"), user=os.environ.get("DB_USER", "notes"),
                             password=os.environ.get("DB_PASSWORD", ""),
                             sslmode=os.environ.get("DB_SSLMODE", "prefer"))
    return "postgresql://notes:notes@localhost:5433/notes"  # local docker compose


DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "10"))
# Local dev applies migrations at startup for convenience. In AWS this is false and
# migrations run as a one-off task before the new version takes traffic.
RUN_MIGRATIONS_ON_STARTUP = _bool("RUN_MIGRATIONS_ON_STARTUP", True)

# Identity: "api_key" (default), "oidc", or "both".
AUTH_MODE = os.environ.get("AUTH_MODE", "api_key").lower()
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", "")
OIDC_JWKS_URL = os.environ.get("OIDC_JWKS_URL", "")
# Self-registration (POST /users issuing API keys). Turn off when everyone signs in through the IdP.
ALLOW_API_KEY_SIGNUP = _bool("ALLOW_API_KEY_SIGNUP", True)

RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))  # 0 disables

ATTACHMENTS_BUCKET = os.environ.get("ATTACHMENTS_BUCKET", "")
ATTACHMENT_MAX_BYTES = int(os.environ.get("ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)))
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

# AI: /teams/{id}/ask. "bedrock" (real models), "fake" (offline, deterministic; tests), or "off".
AI_MODE = os.environ.get("AI_MODE", "off").lower()
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
CHAT_MODEL_ID = os.environ.get("CHAT_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
AI_TIMEOUT_SECONDS = int(os.environ.get("AI_TIMEOUT_SECONDS", "30"))
ASK_TOP_K = int(os.environ.get("ASK_TOP_K", "8"))                    # chunks sent to the model
ASK_MAX_OUTPUT_TOKENS = int(os.environ.get("ASK_MAX_OUTPUT_TOKENS", "600"))
ASK_RATE_PER_MINUTE = int(os.environ.get("ASK_RATE_PER_MINUTE", "10"))  # per user; model calls cost money
ASK_CATCHUP_LIMIT = int(os.environ.get("ASK_CATCHUP_LIMIT", "20"))  # unindexed notes indexed inline per ask

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.environ.get("LOG_FORMAT", "json")  # "json" or "text"
