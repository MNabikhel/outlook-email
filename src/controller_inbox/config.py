from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator, model_validator

from controller_inbox.clock import AUTO, effective_timezone, is_timezone
from pydantic_settings import BaseSettings, SettingsConfigDict


PROFILES = {
    "general": "General — any inbox",
    "finance": "Finance & accounting — adds month-end and close",
}


def _on_this_network(host: str) -> bool:
    name = host.rsplit("@", 1)[-1]
    name = name[1:].split("]", 1)[0] if name.startswith("[") else name.rsplit(":", 1)[0]
    name = name.lower()
    if name == "localhost" or name.endswith((".local", ".lan", ".home")) or "." not in name:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONTROLLER_INBOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Path("./data")
    inbox_dir: Path = Path("./inbox")
    # The AP cost code workbook's folder. Empty = "AP cost codes" beside the inbox folder.
    cost_codes_dir: Path | None = None
    # ``auto`` follows this computer. A saved Setup choice or CONTROLLER_INBOX_TIMEZONE overrides it.
    timezone: str = AUTO
    host: str = "127.0.0.1"
    port: int = 8765
    # Extra names or addresses the dashboard answers on when HOST is beyond this computer (comma-separated).
    # This computer's own names and addresses are always allowed; "*" is not accepted.
    allowed_hosts: str = ""
    poll_seconds: int = 120
    lookback_hours: int = 72
    high_amount: float = 10_000.0
    vip_senders: str = ""
    trusted_domains: str = ""
    writeback: bool = False
    digest_hour: int = 7
    digest_to: str = ""
    mailbox: str = ""
    llm: bool | None = None
    llm_model: str = "local-model"
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    llm_api_key: str = ""
    llm_timeout: float = 90.0
    llm_max_prompt_chars: int = 6000
    llm_max_tokens: int = 450
    overnight_batch: int = 40
    # Long attachments the model summarizes per overnight run, so "summarize this file" answers at once. 0 = none.
    overnight_file_summaries: int = 20
    # Search by meaning uses an embedding model on the local server (LM Studio ships nomic-embed-text).
    # Empty = find one there; "off" = keyword search only. The URL defaults to the chat server's.
    embedding_model: str = ""
    embedding_base_url: str = ""
    digest_lookback_days: int = 1
    profile: str = "general"
    chat_max_tokens: int = 500
    # The model's context window in tokens when the server doesn't report it (LM Studio does).
    chat_context_tokens: int = 0
    # Enough for most attachments to be read whole. LM Studio models loaded with less are reloaded with this; 0 = leave as loaded.
    min_context_tokens: int = 16384
    # A question about a file's tables is first turned into a query the model writes and CloseDesk runs exactly
    # (one short extra call). table_query_thinking lets a model that can think do so first: right about twice
    # as often on the hardest questions (which row and column a question means), but some 600 tokens slower
    # per question, so it is off unless asked for.
    table_queries: bool = True
    table_query_thinking: bool = False

    azure_client_id: str = ""
    azure_tenant_id: str = "common"
    azure_client_secret: str = ""
    openai_api_key: str = ""

    @field_validator("data_dir", "inbox_dir", mode="before")
    @classmethod
    def _path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @field_validator("cost_codes_dir", mode="before")
    @classmethod
    def _optional_path(cls, value: str | Path | None) -> Path | None:
        return Path(value).expanduser() if value and str(value).strip() else None

    @field_validator("llm", mode="before")
    @classmethod
    def _llm_mode(cls, value):
        """``auto`` (the default) means: use the local model when one is answering."""
        if value is None or (isinstance(value, str) and value.strip().lower() in {"", "auto"}):
            return None
        return value

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def _base_url(cls, value) -> str:
        """Accept any URL LM Studio shows (…/v1/chat/completions, …/api/v1/chat, host:port) as the /v1 base."""
        text = str(value or "").strip().rstrip("/")
        if not text:
            return "http://127.0.0.1:1234/v1"
        if "://" not in text:
            text = "http://" + text
        scheme, rest = text.split("://", 1)
        host, _, path = rest.partition("/")
        path = "/" + path if path else ""
        path = re.sub(r"/(chat/completions|completions|responses|models|embeddings)$", "", path)
        # Hosted gateways such as OpenRouter really live under /api/v1; only a server on
        # this machine or network is LM Studio's native /api/vN address.
        if _on_this_network(host):
            path = re.sub(r"^/api/v\d+(/chat|/models)?$", "", path)
        return f"{scheme}://{host}{path or '/v1'}"

    @field_validator("timezone", mode="before")
    @classmethod
    def _timezone(cls, value) -> str:
        text = str(value or AUTO).strip()
        if not text or text.lower() == AUTO:
            return AUTO
        return text if is_timezone(text) else AUTO

    @field_validator("profile", mode="before")
    @classmethod
    def _profile(cls, value) -> str:
        text = str(value or "general").strip().lower()
        return text if text in PROFILES else "general"

    @model_validator(mode="after")
    def _unprefixed_secrets(self) -> "Settings":
        self.azure_client_id = self.azure_client_id or os.getenv("AZURE_CLIENT_ID", "")
        self.azure_tenant_id = os.getenv("AZURE_TENANT_ID", "") or self.azure_tenant_id or "common"
        self.azure_client_secret = self.azure_client_secret or os.getenv("AZURE_CLIENT_SECRET", "")
        self.openai_api_key = self.openai_api_key or os.getenv("OPENAI_API_KEY", "")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(effective_timezone(self.timezone))

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
    def overnight_dir(self) -> Path:
        return self.data_dir / "overnight"

    @property
    def training_path(self) -> Path:
        return self.data_dir / "training" / "corrections.jsonl"

    @property
    def inbox_incoming(self) -> Path:
        return self.inbox_dir / "incoming"

    @property
    def inbox_attachments(self) -> Path:
        return self.inbox_dir / "attachments"

    @property
    def inbox_processed(self) -> Path:
        return self.inbox_dir / "processed"

    @property
    def inbox_extracted(self) -> Path:
        return self.inbox_dir / "extracted"

    @property
    def inbox_failed(self) -> Path:
        return self.inbox_dir / "failed"

    @property
    def cost_codes_folder(self) -> Path:
        return self.cost_codes_dir or self.inbox_dir.parent / "AP cost codes"

    @property
    def llm_mode(self) -> str:
        if self.llm is None:
            return "auto"
        return "on" if self.llm else "off"

    @property
    def vip_list(self) -> list[str]:
        return [part.strip().lower() for part in self.vip_senders.split(",") if part.strip()]

    @property
    def trusted_domain_list(self) -> list[str]:
        """Domains from the setting (``@taz.com`` or ``taz.com``) plus this mailbox's own domain."""
        found = [part.strip().lower().lstrip("@").strip(".") for part in re.split(r"[,;\s]+", self.trusted_domains)]
        if "@" in self.mailbox:
            found.append(self.mailbox.rsplit("@", 1)[1].strip().lower())
        return list(dict.fromkeys(part for part in found if "." in part))

    @property
    def graph_configured(self) -> bool:
        return bool(self.azure_client_id)

    @property
    def daemon_mode(self) -> bool:
        return bool(self.azure_client_id and self.azure_client_secret and self.mailbox)

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.digest_dir.mkdir(parents=True, exist_ok=True)
        self.overnight_dir.mkdir(parents=True, exist_ok=True)
        self.training_path.parent.mkdir(parents=True, exist_ok=True)
        for folder in (
            self.inbox_incoming,
            self.inbox_attachments,
            self.inbox_processed,
            self.inbox_extracted,
            self.inbox_failed,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        from controller_inbox.cost_codes import ensure_workbook

        ensure_workbook(self)


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_data_dir()
    return settings
