"""Inbound webhook schema + internal dataclasses.

The Coperniq webhook payload shape varies by org/automation config. We parse defensively:
project_id is the only hard requirement; everything else we re-read from get_project so the
handler never depends on the webhook carrying full project state.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class CoperniqWebhook(BaseModel):
    """Defensive parse of the inbound webhook. Extra keys are ignored.

    Coperniq Automations can be configured to send different envelopes; we accept the common
    shapes (project_id at top level, or nested under 'project'/'data'). resolve_project_id()
    centralizes the extraction so you can adjust it in ONE place to match your automation.
    """
    model_config = {"extra": "allow"}

    project_id: Optional[Any] = None
    event: Optional[str] = None
    task_key: Optional[str] = None
    project: Optional[dict] = None
    data: Optional[dict] = None

    def resolve_project_id(self) -> Optional[str]:
        if self.project_id is not None:
            return str(self.project_id)
        for blob in (self.project, self.data):
            if isinstance(blob, dict):
                for k in ("project_id", "projectId", "id"):
                    if blob.get(k) is not None:
                        return str(blob[k])
        return None

    def resolve_task_key(self, default: str) -> str:
        if self.task_key:
            return self.task_key
        for blob in (self.data, self.project):
            if isinstance(blob, dict) and blob.get("task_key"):
                return str(blob["task_key"])
        return default


class Assignee(BaseModel):
    id: Optional[int] = None
    first_name: str = ""
    last_name: str = ""
    email: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email or "Reviewer"

    def mention(self) -> str:
        """Coperniq @mention token, e.g. [Ankurkumar Suthar|~id:11695]."""
        if self.id is not None:
            return f"[{self.display_name}|~id:{self.id}]"
        return self.display_name


class ProjectContext(BaseModel):
    """The subset of the project the pipeline needs."""
    project_id: str
    number: Optional[int] = None
    customer_name: str = ""
    address: str = ""
    zone: str = ""
    create_bom_assignee: Assignee = Field(default_factory=Assignee)
    raw: dict = Field(default_factory=dict)  # full get_project payload for the engine
