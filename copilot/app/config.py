"""Runtime configuration. All secrets come from the environment (Railway private
vars / local .env) — never hardcoded, never committed (CLAUDE.md).

Env-var resolution (Gate 0 fix): every field accepts BOTH a `COPILOT_`-prefixed
name AND the bare name Railway injects (e.g. `ANTHROPIC_API_KEY`, `MYSQLHOST`),
via `AliasChoices`. A prior version used `env_prefix="COPILOT_"`, which silently
ignored Railway's unprefixed vars and left the deployed agent with no DB/key.

Local defaults target the repo's `docker/development-easy-light` stack (MariaDB
on host port 8320). On Railway these are overridden by private vars pointing at
the `mariadb` service, using a dedicated READ-ONLY DB user (invariant #3).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute path to copilot/.env so settings load regardless of CWD.
_ENV_FILE = str(Path(__file__).resolve().parent.parent / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore",
        populate_by_name=True,
    )

    # --- Database (READ-ONLY user) -------------------------------------------
    db_host: str = Field(
        "127.0.0.1",
        validation_alias=AliasChoices("COPILOT_DB_HOST", "MYSQLHOST", "MYSQL_HOST"),
    )
    db_port: int = Field(
        8320, validation_alias=AliasChoices("COPILOT_DB_PORT", "MYSQLPORT", "MYSQL_PORT")
    )
    db_user: str = Field(
        "copilot_ro",
        validation_alias=AliasChoices("COPILOT_DB_USER", "MYSQLUSER", "MYSQL_USER"),
    )
    db_password: str = Field(
        "copilot_ro_localdev",
        validation_alias=AliasChoices(
            "COPILOT_DB_PASSWORD", "MYSQLPASSWORD", "MYSQL_PASS"
        ),
    )
    db_name: str = Field(
        "openemr",
        validation_alias=AliasChoices("COPILOT_DB_NAME", "MYSQLDATABASE", "MYSQL_DATABASE"),
    )
    db_pool_size: int = Field(5, validation_alias=AliasChoices("COPILOT_DB_POOL_SIZE"))
    db_connect_timeout: int = Field(5, validation_alias=AliasChoices("COPILOT_DB_CONNECT_TIMEOUT"))
    db_statement_timeout_ms: int = Field(
        4000, validation_alias=AliasChoices("COPILOT_DB_STATEMENT_TIMEOUT_MS")
    )

    # --- LLM provider (Anthropic; ZDR + BAA assumed) -------------------------
    anthropic_api_key: str = Field(
        "", validation_alias=AliasChoices("COPILOT_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    )
    anthropic_model: str = Field(
        "claude-sonnet-5",
        validation_alias=AliasChoices("COPILOT_ANTHROPIC_MODEL", "ANTHROPIC_MODEL"),
    )
    anthropic_max_tokens: int = Field(
        2048, validation_alias=AliasChoices("COPILOT_ANTHROPIC_MAX_TOKENS")
    )
    agent_max_tool_iters: int = Field(6, validation_alias=AliasChoices("COPILOT_AGENT_MAX_TOOL_ITERS"))
    request_deadline_s: float = Field(20.0, validation_alias=AliasChoices("COPILOT_REQUEST_DEADLINE_S"))

    # --- Observability (LangSmith) -------------------------------------------
    langsmith_api_key: str = Field(
        "",
        validation_alias=AliasChoices(
            "COPILOT_LANGSMITH_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"
        ),
    )
    langsmith_project: str = Field(
        "clinical-copilot",
        validation_alias=AliasChoices(
            "COPILOT_LANGSMITH_PROJECT", "LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"
        ),
    )
    langsmith_endpoint: str = Field(
        "https://api.smith.langchain.com",
        validation_alias=AliasChoices(
            "COPILOT_LANGSMITH_ENDPOINT", "LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT"
        ),
    )

    # --- Security / behaviour ------------------------------------------------
    # DEPRECATED: the Week-1 static bearer no longer authenticates patient
    # access. Patient/ops auth is now the OpenEMR-minted session token
    # (session_token.py + AUTH_TOKEN_SPEC.md). This field is retained only for
    # backward-compat / a clean startup diagnostic and must be rotated to an
    # inert value in production (the old value was exposed).
    bearer_secret: str = Field(
        "", validation_alias=AliasChoices("COPILOT_BEARER_SECRET")
    )
    # Session-token TTL (seconds); exp = iat + this. Default 15 min (spec §TTL).
    token_ttl_s: int = Field(900, validation_alias=AliasChoices("COPILOT_TOKEN_TTL_S"))
    # Local-only: mint a scope:patient token for demo_user_id/default_pid so the
    # standalone UI + load tests work without a running OpenEMR. NEVER true in
    # production (production requires real OpenEMR-minted tokens).
    auth_dev_mode: bool = Field(False, validation_alias=AliasChoices("COPILOT_AUTH_DEV_MODE"))
    # Fixed dev signing key used only when auth_dev_mode is on AND the DB signing
    # key table is absent. Inert everywhere else.
    dev_signing_key: str = Field(
        "dev-local-signing-key-not-for-prod",
        validation_alias=AliasChoices("COPILOT_DEV_SIGNING_KEY"),
    )
    # The agent NEVER trusts a client-supplied user id. Identity is pinned
    # server-side (demo) or derived from an OpenEMR-validated token (prod).
    demo_user_id: int = Field(1, validation_alias=AliasChoices("COPILOT_DEMO_USER_ID"))
    # Default patient the standalone panel opens with (a seeded showcase patient,
    # NOT pid 1 — a fresh live install has no pid 1 and the panel looked broken).
    default_pid: int = Field(101, validation_alias=AliasChoices("COPILOT_DEFAULT_PID"))
    mock_llm: bool = Field(False, validation_alias=AliasChoices("COPILOT_MOCK_LLM"))
    rate_limit_per_min: int = Field(30, validation_alias=AliasChoices("COPILOT_RATE_LIMIT_PER_MIN"))
    phase1_encounter_cap: int = Field(5, validation_alias=AliasChoices("COPILOT_PHASE1_ENCOUNTER_CAP"))
    max_history_turns: int = Field(12, validation_alias=AliasChoices("COPILOT_MAX_HISTORY_TURNS"))
    env_name: str = Field("local", validation_alias=AliasChoices("COPILOT_ENV_NAME", "RAILWAY_ENVIRONMENT_NAME"))
    # Week-3 attack-surface hardening (remediates the red-team unauth-disclosure findings).
    # Both default to the SAFE posture; a demo/dev may opt back in.
    expose_docs: bool = Field(False, validation_alias=AliasChoices("COPILOT_EXPOSE_DOCS"))
    ready_public_detail: bool = Field(
        False, validation_alias=AliasChoices("COPILOT_READY_PUBLIC_DETAIL")
    )

    # --- Week 2: guideline retrieval (hybrid RAG, D4) -------------------------
    cohere_api_key: str = Field(
        "", validation_alias=AliasChoices("COPILOT_COHERE_API_KEY", "COHERE_API_KEY")
    )
    # "auto" = Cohere when a key is present, else the keyless fallback
    # (hashed TF-IDF embed / noop rerank). Explicit values pin a provider.
    embed_provider: str = Field("auto", validation_alias=AliasChoices("COPILOT_EMBED_PROVIDER"))
    rerank_provider: str = Field("auto", validation_alias=AliasChoices("COPILOT_RERANK_PROVIDER"))
    retrieval_top_k: int = Field(5, validation_alias=AliasChoices("COPILOT_RETRIEVAL_TOP_K"))

    # --- Week 2: supervisor graph (D5) ----------------------------------------
    supervisor_max_routes: int = Field(
        4, validation_alias=AliasChoices("COPILOT_SUPERVISOR_MAX_ROUTES")
    )
    worker_max_iters: int = Field(3, validation_alias=AliasChoices("COPILOT_WORKER_MAX_ITERS"))
    # Per-node model tiers (latency tuning): empty = inherit anthropic_model.
    # Routing is a forced 3-way tool choice and worker calls are bounded tool
    # loops — neither needs answerer-class reasoning, so both default to a fast
    # Haiku-class tier; the answerer keeps the main model. The deterministic
    # verifier remains the quality guarantee regardless of tier (QA latency fix).
    supervisor_model: str = Field(
        "claude-haiku-4-5-20251001", validation_alias=AliasChoices("COPILOT_SUPERVISOR_MODEL")
    )
    worker_model: str = Field(
        "claude-haiku-4-5-20251001", validation_alias=AliasChoices("COPILOT_WORKER_MODEL")
    )
    answerer_model: str = Field("", validation_alias=AliasChoices("COPILOT_ANSWERER_MODEL"))

    # --- Week 2: eval gate (D6) ------------------------------------------------
    # "stub" (default, hermetic) or "llm" (--live judge; boolean, forced tool).
    judge_mode: str = Field(
        "stub", validation_alias=AliasChoices("COPILOT_JUDGE_MODE", "COPILOT_JUDGE")
    )

    # --- Week 2: document ingestion (D2/D3) ------------------------------------
    ingest_enabled: bool = Field(False, validation_alias=AliasChoices("COPILOT_INGEST_ENABLED"))
    ingest_max_file_mb: int = Field(
        10, validation_alias=AliasChoices("COPILOT_INGEST_MAX_FILE_MB")
    )
    extract_max_retries: int = Field(
        2, validation_alias=AliasChoices("COPILOT_EXTRACT_MAX_RETRIES")
    )
    # Vision model for extraction; None = reuse `anthropic_model`.
    extract_model: str | None = Field(
        None, validation_alias=AliasChoices("COPILOT_EXTRACT_MODEL")
    )
    # Separate INSERT-only writer identity (`copilot_ingest`) — the read-only
    # `copilot_ro` user is untouched (invariant #3). Host/port/name are reused
    # from the db_* fields above; only the identity differs.
    ingest_db_user: str = Field("", validation_alias=AliasChoices("COPILOT_INGEST_DB_USER"))
    ingest_db_password: str = Field(
        "", validation_alias=AliasChoices("COPILOT_INGEST_DB_PASSWORD")
    )
    # Fallback: insert the `documents` row directly when the OAuth2 REST path
    # is unavailable (oauth_keys gap — top risk #1). Off by default.
    ingest_db_doc_fallback: bool = Field(
        False, validation_alias=AliasChoices("COPILOT_INGEST_DB_DOC_FALLBACK")
    )
    # Fail-closed persistence: unverified extracted values are NEVER written
    # while this is False — and False is the default and demo configuration.
    ingest_allow_unverified: bool = Field(
        False, validation_alias=AliasChoices("COPILOT_INGEST_ALLOW_UNVERIFIED")
    )

    # --- Week 2: OpenEMR REST API (document upload front door) -----------------
    openemr_base_url: str = Field(
        "", validation_alias=AliasChoices("COPILOT_OPENEMR_BASE_URL", "OPENEMR_BASE_URL")
    )
    openemr_oauth_client_id: str = Field(
        "", validation_alias=AliasChoices("COPILOT_OPENEMR_OAUTH_CLIENT_ID")
    )
    openemr_oauth_client_secret: str = Field(
        "", validation_alias=AliasChoices("COPILOT_OPENEMR_OAUTH_CLIENT_SECRET")
    )
    openemr_api_username: str = Field(
        "", validation_alias=AliasChoices("COPILOT_OPENEMR_API_USERNAME")
    )
    openemr_api_password: str = Field(
        "", validation_alias=AliasChoices("COPILOT_OPENEMR_API_PASSWORD")
    )

    @property
    def ingest_sqlalchemy_url(self) -> str:
        """DSN for the INSERT-only `copilot_ingest` writer — same host/port/db
        as the read path, different (least-privilege) identity."""
        return (
            f"mysql+pymysql://{self.ingest_db_user}:{self.ingest_db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?connect_timeout={self.db_connect_timeout}"
        )

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?connect_timeout={self.db_connect_timeout}"
        )

    @property
    def anthropic_ready(self) -> bool:
        return bool(self.anthropic_api_key) or self.mock_llm

    @property
    def langsmith_ready(self) -> bool:
        return bool(self.langsmith_api_key)

    def resolution_summary(self) -> dict[str, bool]:
        """PHI-free startup diagnostic: did each dependency resolve to a real
        (non-empty) value? Logged at boot so a missing Railway var is loud."""
        return {
            "db_configured": self.db_host not in ("", "127.0.0.1")
            or self.env_name == "local",
            "anthropic_key_present": bool(self.anthropic_api_key),
            "langsmith_key_present": bool(self.langsmith_api_key),
            "bearer_secret_present": bool(self.bearer_secret),
            "mock_llm": self.mock_llm,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
