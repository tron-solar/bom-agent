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
        # ALIGN WITH YOUR EXISTING HANDLERS. Common schemes:
        #   {"Authorization": f"Bearer {self.api_key}"}  or  {"x-api-key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    # ---------- READ ----------
    def get_project(self, project_id: str, include_virtual: bool = True) -> dict:
        params = {"include_virtual_properties": str(include_virtual).lower()}
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base}/projects/{project_id}", headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()

    def list_project_files(self, project_id: str) -> list[dict]:
        """Return ALL files on a project, paging through Coperniq's 20-file cap.

        CONFIRMED: GET /v1/projects/{id}/files returns at most 20 files per call (a page cap),
        ordered by id ascending = oldest first. A manually-uploaded planset (newer, higher id)
        lands on a later page and is INVISIBLE to a single call. This method pages until the full
        set is retrieved, so the planset matcher sees every file.

        The endpoint's paging params are undocumented; `coperniq_files_pagination_probe.py`
        discovers the real one. This method supports the common schemes and is controlled by
        env var COPERNIQ_FILES_PAGING (default "auto"):
          - "auto"   : try page-size=200; if still capped at 20, fall back to id-cursor paging
          - "page"   : ?page=N&limit=PAGE_SIZE
          - "offset" : ?offset=N&limit=PAGE_SIZE
          - "none"   : single call (old behavior)
        """
        import os
        scheme = os.environ.get("COPERNIQ_FILES_PAGING", "auto")
        page_size = int(os.environ.get("COPERNIQ_FILES_PAGE_SIZE", "200"))

        def _call(params=None):
            with httpx.Client(timeout=self.timeout) as c:
                r = c.get(f"{self.base}/projects/{project_id}/files",
                          headers=self._headers(), params=params or {})
                r.raise_for_status()
                data = r.json()
            if isinstance(data, dict):
                for k in ("items", "data", "files", "results"):
                    if k in data:
                        return data[k]
                return []
            return data

        if scheme == "none":
            return _call()

        if scheme == "page":
            return self._page_loop(_call, "page", page_size, start=1)
        if scheme == "offset":
            return self._page_loop(_call, "offset", page_size, start=0)

        # auto: 1) try a big page size in one shot
        big = _call({"limit": page_size, "pageSize": page_size})
        if len(big) > 20:
            return big  # page-size param honored; got everything (or a much larger page)
        # 2) page-size ignored -> page through with page=N (most common), then offset as fallback
        paged = self._page_loop(_call, "page", 20, start=1)
        if len(paged) > 20:
            return paged
        offset_paged = self._page_loop(_call, "offset", 20, start=0)
        if len(offset_paged) > len(big):
            return offset_paged
        # 3) nothing exceeded the cap: id-cursor fallback (ascending id) if API supports it,
        #    else return what we have and let the matcher raise (fail-closed, never wrong file).
        return self._id_cursor_loop(_call) or big

    @staticmethod
    def _page_loop(_call, param, size, start):
        """Generic page/offset loop. Stops on empty page, short page, or a repeat (no progress)."""
        out, seen_ids = [], set()
        idx = start
        for _ in range(100):  # hard safety cap (100 pages)
            params = {"limit": size, ("page" if param == "page" else "offset"): idx}
            batch = _call(params)
            if not batch:
                break
            new = [f for f in batch if f.get("id") not in seen_ids]
            if not new:
                break  # no progress -> param ignored
            out.extend(new)
            seen_ids.update(f.get("id") for f in new)
            if len(batch) < size:
                break  # last (short) page
            idx += (1 if param == "page" else size)
        return out

    @staticmethod
    def _id_cursor_loop(_call):
        """Last-resort: if the API accepts an id cursor (after/since), page by ascending id."""
        out, seen = [], set()
        after = None
        for _ in range(100):
            params = {}
            if after is not None:
                params = {"after": after, "since_id": after, "afterId": after}
            batch = _call(params or None)
            new = [f for f in batch if f.get("id") not in seen]
            if not new:
                break
            out.extend(new)
            seen.update(f.get("id") for f in new)
            after = max(f.get("id") for f in new if isinstance(f.get("id"), int))
            if len(batch) < 20:
                break
        return out

    def get_project_file(self, project_id: str, file_id: Any) -> dict:
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.base}/projects/{project_id}/files/{file_id}", headers=self._headers())
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
