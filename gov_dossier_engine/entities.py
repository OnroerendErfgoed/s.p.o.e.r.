"""
Common entity models provided by the engine.
These are shared across all workflow plugins.
"""

from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class DossierAccessEntry(BaseModel):
    role: Optional[str] = None
    agents: list[str] = []
    view: list[str] = []
    activity_view: str = "related"  # "own", "related", "all"


class DossierAccess(BaseModel):
    access: list[DossierAccessEntry]


class TaskEntity(BaseModel):
    """Content model for system:task entities."""
    kind: str                           # "fire_and_forget", "recorded", "scheduled_activity", "cross_dossier_activity"
    function: Optional[str] = None      # plugin task function name
    target_activity: Optional[str] = None   # for kinds 3, 4
    target_dossier: Optional[str] = None    # for kind 4 (set by worker after function call)
    result_activity_id: Optional[str] = None  # pre-generated UUID for the scheduled activity
    scheduled_for: Optional[str] = None     # ISO datetime
    cancel_if_activities: list[str] = []
    allow_multiple: bool = False
    status: str = "scheduled"           # "scheduled", "completed", "cancelled", "superseded", "failed"
    result: Optional[str] = None        # URI or result data after completion
    error: Optional[str] = None         # error message if failed


# completeTask activity definition — injected into every workflow by the engine
COMPLETE_TASK_ACTIVITY_DEF = {
    "name": "completeTask",
    "label": "Voltooi taak",
    "description": "System activity that marks a task as completed",
    "can_create_dossier": False,
    "client_callable": False,
    "default_role": "systeem",
    "allowed_roles": ["systeem"],
    "authorization": {"access": "roles", "roles": [{"role": "systeemgebruiker"}]},
    "used": [],
    "generates": ["system:task"],
    "status": None,
    "validators": [],
    "side_effects": [],
    "tasks": [],
}
