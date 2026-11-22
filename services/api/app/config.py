from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "production", "test"] = "development"
    database_url: str = "postgresql://kb_app:kb_app_password@127.0.0.1:55432/kb"
    migration_database_url: str = "postgresql://kb:kb_dev_password@127.0.0.1:55432/kb"
    api_port: int = 8000
    web_url: str = "http://127.0.0.1:3000"
    default_tenant_slug: str = "harbour"

    storage_dir: Path = ROOT / "storage"
    max_upload_bytes: int = 15 * 1024 * 1024
    url_fetch_timeout_seconds: float = 15.0

    ai_mode: Literal["dev", "openai"] = "dev"
    embedding_provider: Literal["dev", "openai", "local"] = "dev"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_embedding_api_key: str = ""
    model_ingest_small: str = ""
    model_rewrite: str = ""
    model_answer: str = ""
    model_judge: str = ""
    model_embed: str = ""
    # OpenAI-compatible embedding APIs support this. Providers that reject it
    # must still return EMBEDDING_DIM values, so disable only when necessary.
    openai_embedding_dimensions: bool = True
    model_rerank: str = ""
    rerank_base_url: str = ""
    rerank_api_key: str = ""

    embedding_dim: int = 1024
    embedding_model: str = "dev-hashed-v1"
    embedding_version: str = "1"

    local_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    local_embedding_device: str = "cuda"
    local_embedding_dtype: Literal["float16", "float32"] = "float16"
    local_embedding_batch_size: int = 16
    local_embedding_max_seq_length: int = 512
    local_embedding_query_instruction: str = (
        "Given a support question, retrieve passages that answer it"
    )

    internal_low_threshold: float = 0.34
    internal_high_threshold: float = 0.52
    public_low_threshold: float = 0.55
    public_high_threshold: float = 0.72

    db_pool_min: int = 1
    db_pool_max: int = 10

    def model_for(self, role: str) -> str:
        mapping = {
            "ingest_small": self.model_ingest_small,
            "rewrite": self.model_rewrite,
            "answer": self.model_answer,
            "judge": self.model_judge,
            "embed": self.model_embed,
            "rerank": self.model_rerank,
        }
        return mapping.get(role, "")

    def validate_provider(self) -> None:
        if self.embedding_dim != 1024:
            raise RuntimeError(
                "EMBEDDING_DIM must be 1024 to match the pgvector column created by "
                "migration 006. Changing embedding model or dimension requires a re-index: "
                "run `make reindex` after migrating."
            )
        if self.ai_mode == "openai":
            required = {
                "OPENAI_API_KEY": self.openai_api_key,
                "OPENAI_BASE_URL": self.openai_base_url,
                "MODEL_ANSWER": self.model_answer,
                "MODEL_REWRITE": self.model_rewrite,
                "MODEL_INGEST_SMALL": self.model_ingest_small,
            }
            if self.embedding_provider == "openai":
                required["MODEL_EMBED"] = self.model_embed
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise RuntimeError(
                    "AI_MODE=openai but required configuration is missing: " + ", ".join(missing)
                )
        if self.embedding_provider == "openai" and self.ai_mode != "openai":
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai requires AI_MODE=openai. "
                "For offline chat with remote embeddings, use AI_MODE=openai and configure chat models."
            )
        if self.embedding_provider == "openai" and not self.model_embed:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai requires MODEL_EMBED for the embeddings endpoint."
            )

    @property
    def active_embedding_model(self) -> str:
        """The model name written to passages and shown in /v1/meta."""
        if self.embedding_provider == "local":
            return self.local_embedding_model
        if self.embedding_provider == "openai":
            return self.model_embed or self.embedding_model
        return self.embedding_model

    @property
    def is_dev(self) -> bool:
        return self.ai_mode == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
