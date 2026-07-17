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
    work_order_webhook_token: str
    work_order_bom_title: str
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
            # Coperniq's "Work Order created" automation posts an UNSIGNED webhook, so that endpoint
            # authenticates on a URL-path shared secret instead of HMAC. work_order_bom_title is the
            # work-order title that triggers a BOM run (matched case-insensitively, trimmed).
            work_order_webhook_token=os.environ.get("WORK_ORDER_WEBHOOK_TOKEN", ""),
            work_order_bom_title=os.environ.get("WORK_ORDER_BOM_TITLE", "Create BOM"),
            # draft_mode True -> attached file display names keep the "DRAFT — " prefix. Flip to False
            # at go-live (DRAFT_MODE=0) to drop the prefix. Controls ONLY the prefix (Fixes 4-6).
            draft_mode=_bool(os.environ.get("DRAFT_MODE"), True),
            libreoffice_bin=os.environ.get("LIBREOFFICE_BIN", "libreoffice"),
        )


CONFIG = Config.from_env()
