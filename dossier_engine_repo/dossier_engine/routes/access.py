"""Shared access control utilities for routes."""

from __future__ import annotations

from uuid import UUID
from fastapi import HTTPException
from ..db.models import Repository
from ..auth import User


async def check_dossier_access(
    repo: Repository, dossier_id: UUID, user: User,
    global_access: list[dict] | None = None,
) -> dict | None:
    """Check if user has access to this dossier. Returns the matched access entry.
    
    Checks global_access first (applies to all dossiers), then dossier-specific access.
    
    Returns:
        None — no restrictions (global access or no dossier_access entity)
        dict — the matched access entry (with role, view, activity_view)
    
    Raises:
        HTTPException 403 if user has no access
    """
    # Check global access entries first (defined in config, apply to all dossiers)
    if global_access:
        for entry in global_access:
            entry_role = entry.get("role")
            if entry_role and entry_role in user.roles:
                return entry

    access_entity = await repo.get_singleton_entity(dossier_id, "oe:dossier_access")
    if not access_entity or not access_entity.content:
        return None  # no access entity = no restrictions

    for entry in access_entity.content.get("access", []):
        entry_role = entry.get("role")
        if entry_role and entry_role in user.roles:
            return entry
        entry_agents = entry.get("agents", [])
        if user.id in entry_agents:
            return entry

    # No access. Emit an audit event before raising so SIEM has a
    # record of the attempt. We don't know which action the caller
    # was about to perform (this is shared code called from reads
    # and writes alike) — the generic "dossier.denied" action name
    # is enough for investigation; correlate with the HTTP request
    # logs for the specific endpoint.
    from ..audit import emit_audit
    emit_audit(
        action="dossier.denied",
        actor_id=user.id,
        actor_name=user.name,
        target_type="Dossier",
        target_id=str(dossier_id),
        outcome="denied",
        dossier_id=str(dossier_id),
        reason="User has no matching role or agent entry for this dossier",
    )
    raise HTTPException(403, detail="No access to this dossier")


def get_visibility_from_entry(entry: dict | None) -> tuple[set[str] | None, str]:
    """Extract visible types and activity_view mode from an access entry.
    
    Returns:
        (visible_types, activity_view_mode)
        visible_types is None if no restrictions (entry is None or 'view' key absent)
        visible_types is a set if 'view' key is present (even if empty = see nothing)
    """
    if entry is None:
        return None, "all"
    
    activity_view = entry.get("activity_view", "all")
    
    if "view" not in entry:
        return None, activity_view  # no view key = see everything
    
    visible = set(entry["view"])
    return visible, activity_view
