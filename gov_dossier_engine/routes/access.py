"""Shared access control utilities for routes."""

from __future__ import annotations

from uuid import UUID
from ..db.models import Repository
from ..auth import User


async def get_visible_types(repo: Repository, dossier_id: UUID, user: User) -> set[str] | None:
    """Get the set of entity types visible to this user, or None if no filtering.
    
    Returns:
        None — no dossier_access entity exists, no filtering applied
        set[str] — entity types the user can see (may be empty = see nothing)
    """
    access_entity = await repo.get_latest_entity(dossier_id, "oe:dossier_access")
    if not access_entity or not access_entity.content:
        return None

    for entry in access_entity.content.get("access", []):
        entry_role = entry.get("role")
        if entry_role and entry_role in user.roles:
            return set(entry.get("view", []))
        entry_agents = entry.get("agents", [])
        if user.id in entry_agents:
            return set(entry.get("view", []))

    return set()  # no match = see nothing


async def get_access_entry(repo: Repository, dossier_id: UUID, user: User) -> dict | None:
    """Get the full matched dossier_access entry for this user.
    
    Returns the matched entry dict (with role, view, activity_view), 
    or None if no access entity or no match.
    """
    access_entity = await repo.get_latest_entity(dossier_id, "oe:dossier_access")
    if not access_entity or not access_entity.content:
        return None

    for entry in access_entity.content.get("access", []):
        entry_role = entry.get("role")
        if entry_role and entry_role in user.roles:
            return entry
        entry_agents = entry.get("agents", [])
        if user.id in entry_agents:
            return entry

    return None  # no match
