"""Thin Coperniq REST client — only the calls this service needs.

Mirrors the MCP capabilities we validated:
  - get_project (read)
  - list_project_files / get_project_file (find + resolve the planset URL)
  - create_project_file (attach by URL)
  - create_project_comment (notify via @mention)
  - update_project_work_order (status/description; optional)

IMPORTANT: align `_headers()` and endpoint paths with your EXISTING Railway handlers — the auth
scheme and base path are org-specific. The method bodies below are the standard REST shape; if your
other handlers already wrap Coperniq, you may prefer to import that client instead of this one.
"""
from __future__ import annotations
import logging
from typing import Any, Optional
import httpx

from .config import CONFIG
from .models import ProjectContext, Assignee

log = logging.getLogger("coperniq")


class CoperniqClient:
    def __init__(self, base: str | None = None, api_key: str | None = None, timeout: float = 60.0):
        self.base = (base or CONFIG.coperniq_api_base).rstrip("/")
        self.api_key = api_key or CONFIG.coperniq_api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        # Coperniq authenticates with the `x-api-key` header (confirmed against the live API in
        # coperniq_files_pagination_probe.py and the Coperniq docs), NOT Bearer.
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    # ---------- READ ----------
    def get_project(self, project_id: str, include_virtual: bool = True) -> dict:
        params = {"include_virtual_properties": str(include_virtual).lower()}
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base}/projects/{project_id}", headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()

    def list_project_files(self, project_id: str) -> list[dict]:
        """Return ALL files on a project, paging through Coperniq's per-page cap.

        CONFIRMED SCHEME (probe against live API, project 868257):
          - the page-size param is `page_size` (NOT limit/pageSize/per_page/size)
          - Coperniq enforces a HARD MAX of 100 files per page even if you request more
          - the page param is `page` (1-indexed); ?page=2 returns the next distinct set
        So we loop ?page_size=100&page=N until a short/empty page. A manually-uploaded planset
        (newer, higher id) can be on page 2, 3, ... — a single call (first 20) never sees it,
        which was the whole bug.

        Override via env if Coperniq changes this: COPERNIQ_FILES_PAGE_SIZE (default 100).
        """
        import os
        page_size = min(int(os.environ.get("COPERNIQ_FILES_PAGE_SIZE", "100")), 100)
        out, seen = [], set()
        page = 1
        with httpx.Client(timeout=self.timeout) as c:
            for _ in range(200):  # safety cap: 200 pages * 100 = 20k files
                r = c.get(f"{self.base}/projects/{project_id}/files",
                          headers=self._headers(),
                          params={"page": page, "page_size": page_size})
                r.raise_for_status()
                data = r.json()
                batch = data if isinstance(data, list) else (
                    data.get("items") or data.get("data") or data.get("files") or [])
                if not batch:
                    break
                new = [f for f in batch if f.get("id") not in seen]
                if not new:
                    break  # no progress (defensive: param ignored / repeated page)
                out.extend(new)
                seen.update(f.get("id") for f in new)
                if len(batch) < page_size:
                    break  # last page
                page += 1
        return out

    def get_project_file(self, project_id: str, file_id: Any) -> dict:
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base}/projects/{project_id}/files/{file_id}", headers=self._headers())
            r.raise_for_status()
            return r.json()

    def list_project_forms(self, project_id: str) -> list[dict]:
        """All forms on a project (GET /v1/projects/{id}/forms). Confirmed live: returns a list."""
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base}/projects/{project_id}/forms",
                      headers=self._headers(), params={"page": 1, "page_size": 100})
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else (
                data.get("items") or data.get("data") or data.get("forms") or [])

    def get_form(self, form_id: Any) -> dict:
        """One form with its field layout (GET /v1/forms/{form_id}) — carries `formLayouts`, which the
        extractor's master-note parser walks for stack/wall wording."""
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base}/forms/{form_id}", headers=self._headers())
            r.raise_for_status()
            return r.json()

    # ---------- WRITE ----------
    def create_project_file(self, project_id: str, url: str, name: str,
                            phase_instance_id: Optional[int] = None, is_archived: bool = False) -> dict:
        body: dict[str, Any] = {"url": url, "name": name, "isArchived": is_archived}
        if phase_instance_id is not None:
            body["phaseInstanceId"] = phase_instance_id
        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(f"{self.base}/projects/{project_id}/files", headers=self._headers(), json=body)
            r.raise_for_status()
            return r.json()

    def create_project_comment(self, project_id: str, body: str) -> dict:
        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(f"{self.base}/projects/{project_id}/comments",
                       headers=self._headers(), json={"body": body})
            r.raise_for_status()
            return r.json()

    def update_project_work_order(self, project_id: str, work_order_id: Any, **fields) -> dict:
        with httpx.Client(timeout=self.timeout) as c:
            r = c.patch(f"{self.base}/projects/{project_id}/work-orders/{work_order_id}",
                        headers=self._headers(), json=fields)
            r.raise_for_status()
            return r.json()

    # ---------- HELPERS ----------
    def build_context(self, project_id: str) -> ProjectContext:
        p = self.get_project(project_id)
        custom = p.get("custom", {}) or {}
        addr = p.get("address")
        addr_str = addr[0] if isinstance(addr, list) and addr else (addr or "")
        assignee_blob = custom.get(f"{CONFIG.create_bom_task_key}_assignee") or {}
        assignee = Assignee(
            id=assignee_blob.get("id"),
            first_name=assignee_blob.get("firstName", ""),
            last_name=assignee_blob.get("lastName", ""),
            email=assignee_blob.get("email", ""),
        )
        # fallback: project_engineer is the usual create_bom owner if the task assignee is absent
        if assignee.id is None:
            pe = custom.get("project_engineer") or {}
            assignee = Assignee(id=pe.get("id"), first_name=pe.get("firstName", ""),
                                last_name=pe.get("lastName", ""), email=pe.get("email", ""))
        zone = custom.get("zone", [""])
        return ProjectContext(
            project_id=str(project_id),
            number=p.get("number"),
            customer_name=p.get("title", ""),
            address=addr_str,
            zone=zone[0] if isinstance(zone, list) and zone else (zone or ""),
            create_bom_assignee=assignee,
            raw=p,
        )

    def find_planset_file(self, project_id: str, customer_name: str) -> dict:
        """Confirm the single correct planset via the strict convention matcher.
        Raises PlansetNotConfirmed (NOT a silent fallback) if it can't be confirmed.

        Returns a dict: {"file": <file obj>, "url": str, "name": str, "revision": str,
                         "diagnostics": dict}.
        """
        from .planset_confirm import select_planset
        files = self.list_project_files(project_id)
        # If your file API can scope to the Engineering folder, pass that subset instead of `files`
        # and set engineering_only=True. The observed API returns no folder field, so we scan all.
        confirmed = select_planset(files, customer_name, engineering_only=False)
        # resolve URL via get_project_file if the list object lacked one (handled in select_planset
        # by raising; do a best-effort resolve here before that point)
        return {"file": confirmed.file, "url": confirmed.url, "name": confirmed.name,
                "revision": confirmed.revision, "diagnostics": confirmed.diagnostics}
