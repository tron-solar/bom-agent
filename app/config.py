"""Environment-driven configuration. All knobs live here so the rest of the app reads typed values."""
from __future__ import annotations
import os
from dataclasses import dataclass


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    coperniq_api_base: str
    coperniq_api_key: str
    coperniq_webhook_secret: str
    anthropic_api_key: str
    public_base_url: str
    file_storage_dir: str
    file_url_ttl_hours: int
    create_bom_task_key: str
    draft_mode: bool
    libreoffice_bin: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            # Coperniq's REST API is versioned under /v1 (confirmed live: bare host 403s, /v1 works).
            coperniq_api_base=os.environ.get("COPERNIQ_API_BASE", "https://api.coperniq.io/v1").rstrip("/"),
            coperniq_api_key=os.environ.get("COPERNIQ_API_KEY", ""),
            coperniq_webhook_secret=os.environ.get("COPERNIQ_WEBHOOK_SECRET", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            file_storage_dir=os.environ.get("FILE_STORAGE_DIR", "/data/bom-files"),
            file_url_ttl_hours=int(os.environ.get("FILE_URL_TTL_HOURS", "168")),
            create_bom_task_key=os.environ.get("CREATE_BOM_TASK_KEY", "create_bom"),
            # draft_mode True -> attached file display names keep the "DRAFT — " prefix. Flip to False
            # at go-live (DRAFT_MODE=0) to drop the prefix. Controls ONLY the prefix (Fixes 4-6).
            draft_mode=_bool(os.environ.get("DRAFT_MODE"), True),
            libreoffice_bin=os.environ.get("LIBREOFFICE_BIN", "libreoffice"),
        )


CONFIG = Config.from_env()
