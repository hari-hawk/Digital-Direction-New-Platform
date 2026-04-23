from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://dd_user:dd_local_dev@localhost:5433/digital_direction"
    database_url_sync: str = "postgresql+psycopg2://dd_user:dd_local_dev@localhost:5433/digital_direction"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Storage
    storage_backend: str = "local"  # "local" or "gcs"
    storage_base_dir: str = str(Path(__file__).parent.parent / "storage")
    gcs_bucket_name: str = ""

    # LLM — Backend selection
    # "aistudio" uses GEMINI_API_KEY (default, simple, rate-limited shared capacity)
    # "vertex" uses Vertex AI with ADC (better 503/reliability, needs GCP project + billing)
    llm_backend: str = "aistudio"
    gcp_project_id: str = ""
    gcp_region: str = "us-central1"

    # LLM — Models (override via env to switch models without code change)
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_extraction_model: str = "gemini-2.5-flash"
    gemini_complex_model: str = "gemini-2.5-pro"
    gemini_embedding_model: str = "models/text-embedding-004"
    claude_merge_model: str = "claude-sonnet-4-6"
    claude_complex_model: str = "claude-opus-4-6"
    claude_eval_model: str = "claude-opus-4-6"
    claude_max_tokens: int = 8192

    # LLM — Rate limiting & extraction tuning
    gemini_max_concurrent: int = 200  # Tier 2: 2000+ RPM. True async via client.aio.
    claude_max_concurrent: int = 10
    llm_retry_delays: list[int] = [1, 2, 4, 8, 16]

    # LLM — Spend cap (hard stop for LLM calls once crossed; 0 = no cap)
    max_spend_usd: float = 100.0
    spend_warn_pct: float = 0.8  # Warn at 80% of cap

    # Extraction — timeouts and batching
    section_timeout: int = 600           # max total seconds per section (safety net — streaming handles most cases)
    extraction_batch_size: int = 50      # sections per batch

    # LangFuse — LLM observability (self-hosted)
    langfuse_enabled: bool = True  # Set to True + provide keys to enable tracing
    langfuse_public_key: str = "pk-lf-test-key"  # Override via LANGFUSE_PUBLIC_KEY env var
    langfuse_secret_key: str = "sk-lf-test-key"  # Override via LANGFUSE_SECRET_KEY env var
    langfuse_host: str = "http://localhost:3100"  # Override via LANGFUSE_HOST env var

    # CORS
    cors_origins: list[str] = ["http://localhost:3000"]  # Override for staging/prod

    # Paths
    configs_dir: str = str(Path(__file__).parent.parent / "configs")
    data_dir: str = str(Path(__file__).parent.parent / "data")

    # App
    app_name: str = "Digital Direction"
    debug: bool = True

    model_config = {"env_file": str(Path(__file__).parent.parent / ".env"), "extra": "ignore"}


settings = Settings()
