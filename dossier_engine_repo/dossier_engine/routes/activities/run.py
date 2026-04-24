"""
Activity execution helpers — pure functions called from the
register/typed modules.

* ``_resolve_plugin_and_def`` — resolve plugin + activity definition
  from (type, workflow) — used by all entry points.
* ``_run_activity`` — the execute_activity call + response shaping.
* ``_emit_activity_success`` — post-commit audit emission.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from ...auth import User
from ...observability.audit import emit_dossier_audit
from ...db import Repository
from ...engine import ActivityError, execute_activity
from ...plugin import Plugin, PluginRegistry
from .._helpers.errors import activity_error_to_http
from .._helpers.models import FullResponse


def _resolve_plugin_and_def(
    registry: PluginRegistry,
    activity_type: str,
    workflow_name: str | None,
) -> tuple[Plugin, dict]:
    """Find the plugin and activity definition for `activity_type`.

    ``activity_type`` can arrive in bare (``dienAanvraagIn``) or
    qualified (``oe:dienAanvraagIn``) form — both resolve to the
    same activity. The registry stores activities by their qualified
    name (guaranteed by ``_normalize_activity_names`` at plugin
    load), so we qualify the incoming value before lookup.

    Two paths:

    1. **Registered activity** — `registry.get_for_activity` knows
       which plugin owns this activity type. Returns immediately.
    2. **First-activity-on-new-dossier** — the activity hasn't been
       registered with the registry yet (because the dossier doesn't
       exist), so we fall back to the explicit `workflow_name` from
       the request body, look up the plugin, and walk its activity
       list for a matching `name`.

    Raises 404 if neither path resolves.
    """
    from ...prov.activity_names import qualify

    qualified_type = qualify(activity_type)

    result = registry.get_for_activity(qualified_type)
    if result is not None:
        return result

    if not workflow_name:
        raise HTTPException(
            404, detail=f"Unknown activity type: {activity_type}",
        )

    plugin = registry.get(workflow_name)
    if plugin is None:
        raise HTTPException(
            404, detail=f"Unknown workflow: {workflow_name}",
        )

    for a in plugin.workflow.get("activities", []):
        if a["name"] == qualified_type:
            return plugin, a
    raise HTTPException(
        404, detail=f"Unknown activity: {activity_type}",
    )


def _emit_activity_success(
    *,
    user: User,
    dossier_id: UUID,
    act_def: dict,
    activity_id: UUID,
) -> None:
    """Emit the success audit event for one activity execution.

    Extracted from ``_run_activity`` for Bug 7 fix: the event must
    fire *after* the DB transaction has committed, not during the
    transactional work. Callers invoke this only after
    ``run_with_deadlock_retry`` has returned success; on rollback or
    deadlock-retry this function is never called, so the audit log
    never claims an activity that didn't commit.

    The action name is derived from the activity definition:
    ``can_create_dossier: true`` (the entry-point activity, e.g.
    ``dienAanvraagIn``) emits ``dossier.created``; everything else
    emits ``dossier.updated``. The distinction matters to SIEM rules
    that track dossier lifecycle.

    Best-effort emission: ``emit_dossier_audit`` never raises, so a
    misbehaving audit sink cannot invalidate a committed DB
    transaction. See ``dossier_engine.observability.audit`` for the log-sink
    contract.
    """
    is_root = bool(act_def.get("can_create_dossier"))
    action = "dossier.created" if is_root else "dossier.updated"
    emit_dossier_audit(
        action=action,
        user=user,
        dossier_id=dossier_id,
        outcome="allowed",
        activity_type=act_def.get("name"),
        activity_id=str(activity_id),
    )


async def _run_activity(
    *,
    repo: Repository,
    plugin: Plugin,
    act_def: dict,
    dossier_id: UUID,
    activity_id: UUID,
    user: User,
    role: str | None,
    used: list,
    generated: list,
    relations: list,
    remove_relations: list,
    workflow_name: str | None,
    informed_by: str | None,
) -> dict:
    """Call `execute_activity` with the standard argument set,
    forwarding any `ActivityError` to an `HTTPException` so FastAPI
    serializes it correctly.

    Centralizes the `[item.model_dump() for item in ...]` pattern
    that all three endpoints repeat — the engine takes plain dicts,
    not Pydantic models.

    Audit emission on writes:

    * **Denial path** (``ActivityError`` with code 403) emits
      ``dossier.denied`` directly here, in-transaction. This is
      correct on rollback: the denial decision *is* the auditable
      fact; whether the transaction rolled back or committed doesn't
      change that. The denial event reflects that the user attempted
      the action and was refused — independent of any DB state.
    * **Success path** does NOT emit here. The caller invokes
      ``_emit_activity_success`` after ``run_with_deadlock_retry``
      returns success, so the audit event fires only once per
      committed activity (not per deadlock-retry attempt, and never
      for an item that was part of a batch that later rolled back).

    Non-authorization errors (validation, 422, etc.) are not audited
    — those belong in the application log / Sentry, not the SIEM
    audit trail.
    """
    try:
        result = await execute_activity(
            plugin=plugin,
            activity_def=act_def,
            repo=repo,
            dossier_id=dossier_id,
            activity_id=activity_id,
            user=user,
            role=role,
            used_items=[u.model_dump() for u in used],
            generated_items=[g.model_dump() for g in generated],
            relation_items=[r.model_dump(by_alias=True) for r in relations],
            remove_relation_items=[r.model_dump(by_alias=True) for r in remove_relations],
            workflow_name=workflow_name,
            informed_by=informed_by,
        )
    except ActivityError as e:
        # Write-side authorization denial: emit dossier.denied so this
        # shows up in the SIEM stream alongside read-side denials from
        # routes/access.py. Non-403 errors (validation, business rule
        # violations) are NOT audited — those are app-level concerns,
        # not security events.
        #
        # This emit happens in-transaction, which is correct: the
        # denial decision is the auditable fact regardless of whether
        # downstream DB work rolls back. A 403 means no material DB
        # writes happened yet anyway — ``authorize`` runs before
        # ``resolve_used`` / ``process_generated`` / ``persistence``
        # in ``execute_activity``.
        #
        # Attribute names: ``ActivityError`` stores ``status_code`` and
        # ``detail`` (see ``engine/errors.py``). Earlier versions of
        # this code read ``code`` and ``message`` via getattr-with-
        # default, which silently returned ``None`` for every denial —
        # meaning write-side ``dossier.denied`` events had never
        # reached the SIEM at all. Fixed under Bug 77; the regression
        # test in ``TestAuditEmitIsPostCommit.test_denial_still_emits_in_transaction``
        # pins the emit path and the attribute names.
        if e.status_code == 403:
            emit_dossier_audit(
                action="dossier.denied",
                user=user,
                dossier_id=dossier_id,
                outcome="denied",
                reason=str(e.detail),
                activity_type=act_def.get("name"),
                activity_id=str(activity_id),
            )
        raise activity_error_to_http(e)

    return result
