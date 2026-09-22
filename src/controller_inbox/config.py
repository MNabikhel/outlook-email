from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default to the LM Studio local server. Ollama users set http://localhost:11434/v1,
# Bionic / other OpenAI-compatible servers set their own base URL.
DEFAULT_LLM_BASE_URL = "http://localhost:1234/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONTROLLER_INBOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Path("./data")
    timezone: str = "America/New_York"
    host: str = "127.0.0.1"
    port: int = 8765
    poll_seconds: int = 120
    lookback_hours: int = 72
    high_amount: float = 10_000.0
    vip_senders: str = ""
    writeback: bool = False
    digest_hour: int = 7
    digest_to: str = ""
    mailbox: str = ""
    llm: bool = False
    llm_model: str = "local-model"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 45.0

    azure_client_id: str = ""
    azure_tenant_id: str = "common"
    azure_client_secret: str = ""
    openai_api_key: str = ""

    @field_validator("data_dir", mode="before")
    @classmethod
    def _path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @model_validator(mode="after")
    def _unprefixed_secrets(self) -> "Settings":
        self.azure_client_id = self.azure_client_id or os.getenv("AZURE_CLIENT_ID", "")
        self.azure_tenant_id = os.getenv("AZURE_TENANT_ID", "") or self.azure_tenant_id or "common"
        self.azure_client_secret = self.azure_client_secret or os.getenv("AZURE_CLIENT_SECRET", "")
        self.openai_api_key = self.openai_api_key or os.getenv("OPENAI_API_KEY", "")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "closedesk.db"

    @property
    def token_cache_path(self) -> Path:
        return self.data_dir / "msal_token_cache.bin"

    @property
    def digest_dir(self) -> Path:
        return self.data_dir / "digests"

    @property
    def vip_list(self) -> list[str]:
        return [part.strip().lower() for part in self.vip_senders.split(",") if part.strip()]

    @property
    def graph_configured(self) -> bool:
        return bool(self.azure_client_id)

    @property
    def llm_endpoint(self) -> str:
        """Effective OpenAI-compatible base URL for the local model server."""
        base = (self.llm_base_url or "").strip() or DEFAULT_LLM_BASE_URL
        return base.rstrip("/")

    @property
    def llm_key(self) -> str:
        # Local servers (LM Studio, Ollama, Bionic) ignore the key but the OpenAI
        # client format wants one; fall back to any OpenAI key, then a harmless placeholder.
        return self.llm_api_key or self.openai_api_key or "local-no-key"

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm and self.llm_endpoint)

    @property
    def daemon_mode(self) -> bool:
        return bool(self.azure_client_id and self.azure_client_secret and self.mailbox)

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.digest_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_data_dir()
    return settings
