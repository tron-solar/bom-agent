"""File hosting on the Railway volume.

Coperniq's create_project_file fetches a URL, so we persist the generated xlsx to the mounted
volume and expose it at a tokenized public route (`/files/<token>/<name>`). The token is an opaque
random id so the URL isn't guessable; TTL is enforced by a stored expiry the file route checks.

This keeps everything inside one Railway service — no S3 credentials needed.
"""
from __future__ import annotations
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote

from .config import CONFIG


@dataclass
class HostedFile:
    token: str
    name: str
    path: str
    public_url: str
    expires_at: float


def _meta_path(token_dir: str) -> str:
    return os.path.join(token_dir, "_meta.json")


def host_bytes(content: bytes, name: str) -> HostedFile:
    """Write `content` to a fresh token dir on the volume and return its public URL."""
    os.makedirs(CONFIG.file_storage_dir, exist_ok=True)
    token = secrets.token_urlsafe(16)
    token_dir = os.path.join(CONFIG.file_storage_dir, token)
    os.makedirs(token_dir, exist_ok=True)
    path = os.path.join(token_dir, name)
    with open(path, "wb") as f:
        f.write(content)
    expires_at = time.time() + CONFIG.file_url_ttl_hours * 3600
    with open(_meta_path(token_dir), "w") as f:
        json.dump({"name": name, "expires_at": expires_at}, f)
    public_url = f"{CONFIG.public_base_url}/files/{token}/{quote(name)}"
    return HostedFile(token=token, name=name, path=path, public_url=public_url, expires_at=expires_at)


def resolve_file(token: str, name: str) -> tuple[str, dict] | None:
    """Return (path, meta) if the token/name exists and hasn't expired, else None."""
    token_dir = os.path.join(CONFIG.file_storage_dir, token)
    path = os.path.join(token_dir, name)
    meta_p = _meta_path(token_dir)
    if not (os.path.isfile(path) and os.path.isfile(meta_p)):
        return None
    with open(meta_p) as f:
        meta = json.load(f)
    if time.time() > meta.get("expires_at", 0):
        return None
    return path, meta


def gc_expired() -> int:
    """Delete expired token dirs. Call periodically (or on startup). Returns count removed."""
    removed = 0
    base = CONFIG.file_storage_dir
    if not os.path.isdir(base):
        return 0
    now = time.time()
    for token in os.listdir(base):
        meta_p = _meta_path(os.path.join(base, token))
        try:
            with open(meta_p) as f:
                meta = json.load(f)
            if now > meta.get("expires_at", 0):
                import shutil
                shutil.rmtree(os.path.join(base, token), ignore_errors=True)
                removed += 1
        except (OSError, json.JSONDecodeError):
            continue
    return removed
