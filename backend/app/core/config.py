"""Central, typed configuration. All secrets come from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-insecure-secret-change-me-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXUS_", env_file=".env", extra="ignore")

    # --- app ---
    app_name: str = "NEXUS AI"
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # --- persistence ---
    database_url: str = "sqlite+aiosqlite:///./nexus.db"
    redis_url: str | None = None
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: str = "./data/storage"
    s3_endpoint: str | None = None
    s3_bucket: str = "nexus-documents"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    # --- security ---
    jwt_secret: str = _DEV_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    max_upload_mb: int = 25
    allowed_extensions: tuple[str, ...] = (
        ".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".html", ".htm", ".csv",
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp",
    )
    rate_limit_per_minute: int = 120
    cors_origins: list[str] = []   # empty = same-origin only (the dev proxy / reverse proxy makes the UI same-origin)
    login_rate_limit_per_minute: int = 10
    min_password_length: int = 10
    bcrypt_rounds: int = Field(default=12, ge=4, le=16)

    # --- connectors (Phase 12) ---
    connector_secret_key: str | None = None       # Fernet key (urlsafe base64, 32 bytes); default: derived from jwt_secret
    connector_dns_check: bool = True              # resolve hostnames and refuse private/loopback/link-local targets (SSRF guard)
    connector_max_items_per_sync: int = Field(default=500, ge=1, le=100_000)
    connector_max_item_mb: int = Field(default=20, ge=1, le=200)
    connector_scheduler_seconds: float = Field(default=30.0, gt=0)
    sql_connector_allowed_dialects: tuple[str, ...] = ("postgresql",)   # sqlite would let an admin read any sqlite file on the server

    # --- autonomous agents (Phase 13) ---
    agentic_max_waves: int = Field(default=3, ge=1, le=10)            # plan -> execute -> re-plan rounds
    agentic_max_steps: int = Field(default=12, ge=1, le=50)           # total tool calls per run
    agentic_run_timeout_seconds: float = Field(default=60.0, gt=0)
    agentic_tool_timeout_seconds: float = Field(default=20.0, gt=0)
    agentic_max_tool_output_chars: int = 6000
    agentic_code_execution_enabled: bool = False                       # off by default: see docs/agentic.md before enabling
    agentic_sandbox_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    agentic_sandbox_memory_mb: int = Field(default=256, ge=64, le=2048)

    # --- models ---
    llm_provider: Literal["mock", "openai", "anthropic"] = "mock"
    llm_model: str = "mock-llm"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_price_in_per_mtok: float = 0.0               # single-model setups: USD per 1M prompt tokens (cost tracking only)
    llm_price_out_per_mtok: float = 0.0
    llm_routing_config: str | None = None            # JSON or @/path/to/routing.json (multi-model routing, fallback, budgets)
    llm_cache_enabled: bool = False                  # off by default: keeps evaluation runs honest; enable in production
    llm_cache_ttl_seconds: float = 3600.0
    llm_cache_max_entries: int = 2000
    llm_usage_flush_seconds: float = 2.0
    lease_ttl_seconds: float = 60.0                  # distributed lease for connector syncs / singleton housekeeping (Redis when NEXUS_REDIS_URL is set)
    worker_heartbeat_path: str = "/tmp/nexus-worker-heartbeat"   # liveness file written by `python -m app.worker` (see deploy/k8s/base/worker.yaml)
    worker_heartbeat_seconds: float = 15.0
    quality_monitor_interval_seconds: float = 3600.0   # 0 disables the background quality monitor (alerts are still available via GET /admin/quality)
    queue_gauge_seconds: float = 10.0                # how often the ingestion queue-depth gauge is refreshed (autoscaling signal)
    embedding_provider: Literal["mock", "sentence_transformers", "openai"] = "mock"
    embedding_model: str = "hashing-v1"
    embedding_dim: int = Field(default=384, ge=8, le=4096)
    embedding_api_key: str | None = None
    embedding_base_url: str = "https://api.openai.com/v1"
    reranker_provider: Literal["lexical", "cross_encoder"] = "lexical"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rerank_depth: int = 30

    # --- ingestion ---
    chunk_target_tokens: int = 220
    chunk_overlap_tokens: int = 40
    parent_chunk_tokens: int = 800
    ocr_enabled: bool = False            # OCR for scanned PDF pages
    # --- multimodal (Phase 11) ---
    multimodal_enabled: bool = True      # image enrichment + table extraction during ingestion
    image_ocr_enabled: bool = True       # tesseract on images; degrades to a recorded "unavailable" when the binary is missing
    extract_pdf_tables: bool = True
    vision_provider: Literal["none", "mock", "openai", "anthropic"] = "none"
    vision_model: str = "gpt-4o-mini"
    vision_api_key: str | None = None
    vision_base_url: str | None = None
    max_images_per_document: int = Field(default=20, ge=0, le=200)
    max_image_pixels: int = 40_000_000
    figure_min_pixels: int = 20_000      # embedded images smaller than this (icons, bullets) are skipped
    ingestion_max_attempts: int = 3
    ingestion_retry_backoff_seconds: float = 2.0
    ingestion_workers: int = 2
    # inline: the API process runs the workers. external: the API only enqueues (DB rows) and separate `python -m app.worker`
    # processes do the work, so ingestion scales independently of the API.
    ingestion_mode: Literal["inline", "external"] = "inline"
    worker_poll_seconds: float = 1.0

    # --- retrieval / workflow ---
    retrieval_top_k: int = 8
    retrieval_candidates: int = 40
    rrf_k: int = 60
    max_retrieval_loops: int = 2
    evidence_sufficiency_threshold: float = 0.35
    # Abstention gate: answer "insufficient evidence" when retrieval confidence is below this. 0 disables it. Confidence is
    # backend-sensitive (embedder/reranker/FTS), so calibrate per deployment: `python -m app.evaluation.calibrate`.
    abstain_confidence_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    checkpoint_path: str = "./data/checkpoints.sqlite"
    checkpoint_backend: Literal["sqlite", "postgres"] = "sqlite"   # postgres reuses database_url (required for multi-replica resume)

    # --- agents / workflow ---
    human_review_mode: Literal["auto", "always", "never"] = "auto"
    verify_with_llm: bool = True
    answer_passages: int = 8
    support_threshold: float = 0.7
    weak_support_threshold: float = 0.5
    stream_chunk_words: int = 6

    # --- memory / reports ---
    memory_ttl_session_hours: int = 24
    memory_ttl_doc_interaction_days: int = 90
    memory_purge_interval_seconds: float = 3600
    report_max_documents: int = 8
    report_min_relevance: float = 0.5    # a document is considered only if a retrieved passage covers >= this share of the topic's terms
    pdf_font_path: str | None = None

    # --- knowledge graph ---
    graph_enabled: bool = True
    graph_extractor: Literal["rule", "llm"] = "rule"
    graph_depth: int = Field(default=2, ge=1, le=3)
    graph_max_edges_per_hop: int = 200

    # --- observability ---
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    langsmith_enabled: bool = False
    metrics_enabled: bool = True
    evaluation_results_dir: str | None = None   # default: <repo>/experiments/results

    @model_validator(mode="after")
    def _test_checkpoints_in_memory(self) -> "Settings":
        if self.env == "test" and "checkpoint_path" not in self.model_fields_set:
            self.checkpoint_path = ":memory:"  # tests never write checkpoints into the working directory unless asked
        return self

    @model_validator(mode="after")
    def _prod_requires_real_secret(self) -> "Settings":
        if self.env == "prod" and (self.jwt_secret == _DEV_SECRET or len(self.jwt_secret) < 32):
            raise ValueError("NEXUS_JWT_SECRET must be set to a strong value (>=32 chars) in prod")
        return self

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
