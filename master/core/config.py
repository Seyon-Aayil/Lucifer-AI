"""
master.core.config
==================
Centralised application configuration loaded from environment variables.
Uses Pydantic Settings for type-safe, validated configuration.
All secrets are read at startup — no runtime .env parsing in hot paths.
"""

from __future__ import annotations

import enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(enum.StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class Settings(BaseSettings):
    """
    Application-wide settings. Loaded once at startup via get_settings().
    All fields are immutable after construction.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────
    app_env: Environment = Environment.DEVELOPMENT
    app_secret_key: str = Field(..., min_length=32)
    log_level: str = "INFO"

    # ── Auth ───────────────────────────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_seconds: int = 3600
    jwt_refresh_token_ttl_seconds: int = 604800

    # ── Master API ──────────────────────────────────────────────────────────
    master_api_host: str = "0.0.0.0"
    master_api_port: int = 8000

    # ── PostgreSQL ─────────────────────────────────────────────────────────
    database_url: str = Field(..., description="asyncpg DSN")
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "lucifer"

    # ── Neo4j ──────────────────────────────────────────────────────────────
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field(..., min_length=8)
    neo4j_database: str = "neo4j"

    # ── Redis ──────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── NATS ───────────────────────────────────────────────────────────────
    nats_url: str = "nats://localhost:4222"
    nats_stream_name: str = "lucifer"

    # ── MinIO ──────────────────────────────────────────────────────────────
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = Field(..., min_length=8)
    minio_bucket: str = "lucifer-artifacts"
    minio_secure: bool = False

    # ── LiteLLM ────────────────────────────────────────────────────────────
    litellm_proxy_url: str = "http://localhost:8080"
    litellm_master_key: str = Field(..., min_length=16)

    # ── RouteLLM ───────────────────────────────────────────────────────────
    routellm_threshold: float = Field(0.5, ge=0.0, le=1.0)
    routellm_strong_model: str = "anthropic/claude-opus-4"
    routellm_weak_model: str = "anthropic/claude-haiku-4"

    # ── LLM Providers ──────────────────────────────────────────────────────
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    vllm_base_url: str | None = None
    vllm_model: str | None = None

    # ── Mem0 ───────────────────────────────────────────────────────────────
    mem0_api_url: str = "http://localhost:8090"
    mem0_api_key: str = Field(..., min_length=8)

    # ── Zep ────────────────────────────────────────────────────────────────
    zep_api_url: str = "http://localhost:8091"
    zep_api_key: str = Field(..., min_length=8)

    # ── GPTCache / Semantic Cache ───────────────────────────────────────────
    semantic_cache_similarity_threshold: float = Field(0.92, ge=0.0, le=1.0)
    semantic_cache_enabled: bool = True

    # ── Token Optimizer ────────────────────────────────────────────────────
    llmlingua_model: str = "microsoft/llmlingua-2-bert-large-multilingual-cased-meetingbank"
    token_optimizer_enabled: bool = True

    # ── OpenTelemetry ───────────────────────────────────────────────────────
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "lucifer-master"

    # ── News Sync ──────────────────────────────────────────────────────────
    news_scheduler_enabled: bool = True
    news_default_fetch_cadence_minutes: int = 60

    # ── gRPC Sync (Phase 4a) ───────────────────────────────────────────────
    grpc_bind_addr: str = "0.0.0.0:50051"
    grpc_tls_cert_path: str | None = None
    grpc_tls_key_path: str | None = None
    grpc_tls_client_ca_path: str | None = None
    # Path to the CA private key used to mint per-device client certs during
    # the pairing flow. Defaults to the dev CA produced by `make dev-certs`.
    pairing_ca_cert_path: str = "infra/certs/ca.crt"
    pairing_ca_key_path: str = "infra/certs/ca.key"
    pairing_code_ttl_seconds: int = 60
    pairing_cert_validity_days: int = 365
    pairing_rate_limit_per_window: int = 5
    pairing_rate_limit_window_seconds: int = 60

    # ── Operator login ─────────────────────────────────────────────────────
    # Single-operator self-hosted setup: username + argon2-hashed password
    # supplied via env (LUCIFER_OPERATOR_USERNAME, LUCIFER_OPERATOR_PASSWORD_HASH).
    # When operator_username is empty the admin endpoints fall back to the
    # legacy shared-bearer check (only accepted in development).
    operator_username: str = ""
    operator_password_hash: str = ""
    operator_session_ttl_seconds: int = 8 * 3600

    grpc_sync_enabled: bool = True
    sync_max_subgraph_bytes: int = 50 * 1024 * 1024  # 50 MiB
    sync_max_offline_actions: int = 10_000
    sync_subgraph_lookback_days: int = 30

    # ── Model Upgrade ──────────────────────────────────────────────────────
    benchmark_enabled: bool = True
    benchmark_schedule_cron: str = "0 2 * * *"
    shadow_eval_sample_size: int = 100
    # Candidate model ids the upgrade scheduler shadow-evaluates against the
    # incumbent strong-tier model. Empty disables the upgrade job.
    model_upgrade_candidates: list[str] = Field(default_factory=list)
    # Use an LLM-as-judge scorer instead of exact-substring golden matching.
    model_upgrade_use_judge: bool = False
    model_upgrade_judge_model: str = "anthropic/claude-haiku-4"
    # Replay real captured traffic (query_log) instead of the golden set.
    model_upgrade_use_replay: bool = False

    @field_validator("app_secret_key")
    @classmethod
    def secret_key_not_default(cls, v: str) -> str:
        """Prevent use of placeholder values in non-dev environments."""
        if v in ("<generate-with-openssl>", "changeme", "secret"):
            raise ValueError(
                "APP_SECRET_KEY must be set to a real secret (use: openssl rand -hex 32)"
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.app_env == Environment.DEVELOPMENT

    @property
    def is_test(self) -> bool:
        return self.app_env == Environment.TEST


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return cached Settings singleton.
    Called at startup and injected via FastAPI Depends.
    """
    return Settings()  # type: ignore[call-arg]
