"""
Admin endpoints for the common search index.

Workflow-specific indices are managed by each plugin's own admin
routes (e.g. POST /toelatingen/admin/search/recreate). The common
index spans workflows and is owned by the engine.

All endpoints here require ``global_admin_access`` — a role tier
separate from ``global_audit_access``. Admin operations are
destructive or bulk (drop an index, reindex every dossier) and
belong to the ops / platform team, not to auditors. Admin is
role-only (no per-dossier grant) and lives only in config.yaml.

The common search itself — ``GET /dossiers?q=...`` — is registered
elsewhere (``routes/dossiers.py``) and is authenticated but not
admin-gated. ACL filtering inside ``search_common`` ensures users
only see dossiers they're allowed to see.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException

from ..auth import User
from ..db import get_session_factory
from ..db.models import Repository
from ..search.common_index import recreate_index, reindex_all


logger = logging.getLogger(__name__)


def register_admin_search_routes(
    app, registry, get_user,
    global_admin_access: list[str] | None = None,
):
    """Register /admin/search/... endpoints (common index only)."""

    def _require_admin(user: User) -> None:
        """Gate admin endpoints on global_admin_access roles. Default
        deny when unconfigured — destructive operations should not
        be openable by accident."""
        if not global_admin_access:
            raise HTTPException(
                403,
                detail=(
                    "Admin search endpoints require global_admin_access "
                    "to be configured in config.yaml."
                ),
            )
        if not any(r in user.roles for r in global_admin_access):
            raise HTTPException(
                403,
                detail=(
                    "Admin search endpoints require a role in "
                    "global_admin_access."
                ),
            )

    @app.post(
        "/admin/search/common/recreate",
        tags=["admin"],
        summary="Drop and recreate the common search index",
        description=(
            "DESTRUCTIVE. Drops the existing dossiers-common index "
            "(if any) and creates it with the current mapping. Does "
            "NOT re-index data — call /admin/search/common/reindex "
            "afterwards. Requires global_admin_access role."
        ),
    )
    async def recreate_common(user: User = Depends(get_user)):
        _require_admin(user)
        result = await recreate_index()
        logger.info("[admin] common index recreated: %s", result)
        return result

    @app.post(
        "/admin/search/common/reindex",
        tags=["admin"],
        summary="Re-index every dossier into the common index",
        description=(
            "Walks every dossier in Postgres and indexes it into "
            "dossiers-common. Idempotent — safe to run on a live "
            "index. Useful after a mapping change or to repair "
            "drift. Requires global_admin_access role."
        ),
    )
    async def reindex_common(user: User = Depends(get_user)):
        _require_admin(user)
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)
            result = await reindex_all(repo, registry)
        logger.info("[admin] common index reindexed: %s", result)
        return result
