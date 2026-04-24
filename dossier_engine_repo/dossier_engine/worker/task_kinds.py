"""
Per-kind task dispatch + the shared ``complete_task`` finalizer.

Four functions for the four task lifecycle paths:

* ``complete_task`` — finalize a task (write the ``completeTask``
  activity + the task's completed/failed version). Shared by all
  task kinds.
* ``_process_recorded`` — kind 2: invoke the plugin function,
  record result.
* ``_process_scheduled_activity`` — kind 3: execute the target
  activity in the same dossier.
* ``_process_cross_dossier`` — kind 4: invoke plugin function to
  determine the target dossier, execute there, record back here.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select

from ..auth import User, SYSTEM_USER
from ..db.models import AssociationRow, EntityRow, Repository
from ..engine import ActivityContext, Caller, execute_activity
from ..engine.refs import EntityRef

logger = logging.getLogger("dossier.worker")


async def complete_task(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
    status: str = "completed",
    result_uri: str | None = None,
    informed_by: str | None = None,
    extra_content: dict | None = None,
):
    """Record task completion by running a `systemAction` activity
    through the engine's full pipeline.

    Previously this function hand-wrote the activity row, association,
    task entity revision, and note entity directly via `Repository`
    calls, and then manually recomputed `cached_status` and
    `eligible_activities` on the dossier row. That was a second write
    path in parallel with the engine, which meant the post-activity
    hook didn't fire, the schema-versioning and disjoint-invariant
    checks were skipped, and any new engine invariant that gets added
    in the future would silently not apply to task completions.

    Now every write goes through `execute_activity` with the built-in
    `SYSTEM_ACTION_DEF`. The engine's pipeline runs normally: the task
    revision is validated against the TaskEntity Pydantic model, the
    note is validated against SystemNote, derivation chains are
    checked, the post-activity hook runs, and the finalization phase
    updates the cached status and eligible activities automatically.
    The worker no longer has a special-case write path — it's just
    another `execute_activity` caller.

    `extra_content` is a dict of additional fields to merge into the
    new task version's content. The retry policy uses this to carry
    `attempt_count`, `next_attempt_at`, and `last_attempt_at`
    through the completion path. Error telemetry does NOT flow
    through here — it goes to `logger.exception` and out to the
    configured logging backend (typically Sentry).
    """
    # Build the new task content with the status transition (and
    # optional result URI, plus any extra fields from the caller).
    # This is just a Python dict mutation on a copy of the existing
    # content; the engine will validate the dict against TaskEntity
    # when resolve_generated runs.
    new_content = dict(task.content)
    new_content["status"] = status
    if result_uri:
        new_content["result"] = result_uri
    if extra_content:
        new_content.update(extra_content)

    # Generate a fresh version UUID for the new task entity version.
    # The logical entity_id stays the same — we're creating a
    # revision, not a new logical task.
    new_task_version_id = uuid4()
    prev_task_ref = str(EntityRef(
        type="system:task", entity_id=task.entity_id, version_id=task.id,
    ))
    new_task_ref = str(EntityRef(
        type="system:task", entity_id=task.entity_id, version_id=new_task_version_id,
    ))

    # Build the explanatory note. It's a new logical entity, not a
    # revision of anything, so both the entity_id and version_id are
    # fresh UUIDs and there's no derivedFrom link.
    fn_name = task.content.get("function", "") if task.content else ""
    note_text = f"Task {status}: {fn_name}" if fn_name else f"Task {status}"
    new_note_entity_id = uuid4()
    new_note_version_id = uuid4()
    note_ref = str(EntityRef(
        type="system:note",
        entity_id=new_note_entity_id,
        version_id=new_note_version_id,
    ))

    systemaction_def = plugin.find_activity_def("systemAction")
    if not systemaction_def:
        raise RuntimeError(
            "systemAction activity definition not found in plugin — "
            "the engine should have registered it at startup"
        )

    await execute_activity(
        plugin=plugin,
        activity_def=systemaction_def,
        repo=repo,
        dossier_id=dossier_id,
        activity_id=uuid4(),
        user=SYSTEM_USER,
        role="systeem",
        used_items=[],
        generated_items=[
            {
                "entity": new_task_ref,
                "content": new_content,
                "derivedFrom": prev_task_ref,
            },
            {
                "entity": note_ref,
                "content": {"text": note_text},
            },
        ],
        informed_by=informed_by,
        caller=Caller.SYSTEM,
    )



async def _process_recorded(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
) -> None:
    """Type 2 — recorded task: call a plugin function and record
    completion. The function may do anything (side effects, external
    calls, reading entities through the ActivityContext) but its
    return value is ignored — completion is recorded as a status
    transition on the task entity, not as a separate result row.
    """
    fn_name = task.content.get("function")
    fn = plugin.task_handlers.get(fn_name) if fn_name else None
    if fn:
        all_latest = await repo.get_all_latest_entities(dossier_id)
        resolved = {e.type: e for e in all_latest}
        triggering_user = await _resolve_triggering_user(
            repo, task.generated_by,
        )
        ctx = ActivityContext(
            repo, dossier_id, resolved, plugin.entity_models, plugin=plugin,
            triggering_activity_id=task.generated_by,
            # Worker-run task: executor is the system, attribution is
            # the agent of the triggering activity. See
            # ``ActivityContext`` for the two-field model.
            user=SYSTEM_USER,
            triggering_user=triggering_user,
        )
        await fn(ctx)
    else:
        logger.warning(f"Task {task.id}: function '{fn_name}' not found")

    await complete_task(repo, plugin, dossier_id, task, status="completed")
    logger.info(f"Task {task.id}: recorded task '{fn_name}' completed")


async def _process_scheduled_activity(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
) -> None:
    """Type 3 — scheduled activity: execute an activity in the same
    dossier at the scheduled time, then record completion.
    """
    target_activity_type = task.content.get("target_activity")
    result_activity_id = UUID(task.content["result_activity_id"])

    act_def = plugin.find_activity_def(target_activity_type)
    if not act_def:
        raise ValueError(
            f"Activity definition not found: {target_activity_type}"
        )

    await execute_activity(
        plugin=plugin,
        activity_def=act_def,
        repo=repo,
        dossier_id=dossier_id,
        activity_id=result_activity_id,
        user=SYSTEM_USER,
        role="systeem",
        used_items=[],
        generated_items=[],
        informed_by=str(task.generated_by) if task.generated_by else None,
        caller=Caller.SYSTEM,
    )
    await repo.session.flush()

    await complete_task(
        repo, plugin, dossier_id, task,
        status="completed",
        informed_by=str(result_activity_id),
    )
    logger.info(
        f"Task {task.id}: scheduled activity "
        f"{target_activity_type} executed"
    )


async def _process_cross_dossier(
    repo: Repository,
    plugin,
    registry,
    dossier_id: UUID,
    task: EntityRow,
) -> None:
    """Type 4 — cross-dossier activity: call a plugin function to
    determine the target dossier, execute the target activity there,
    then record completion in the source dossier.

    PROV links the source and target via URIs: the target activity's
    `used` block carries a `urn:dossier:{source_id}` reference, and
    its `informed_by` points at the source activity URI. The source
    dossier's completeTask in turn points at the target activity URI
    so the graph closes both ways.
    """
    fn_name = task.content.get("function")
    fn = plugin.task_handlers.get(fn_name) if fn_name else None
    if not fn:
        raise ValueError(f"Task function not found: {fn_name}")

    triggering_user = await _resolve_triggering_user(repo, task.generated_by)
    ctx = ActivityContext(
        repo, dossier_id, {}, plugin.entity_models, plugin=plugin,
        # Cross-dossier task: same executor/attribution split as the
        # recorded-task path. Source-dossier attribution survives the
        # hop to the target dossier's activity.
        user=SYSTEM_USER,
        triggering_user=triggering_user,
    )
    task_result = await fn(ctx)

    target_dossier_id = UUID(task_result.target_dossier_id)
    target_activity_type = task.content.get("target_activity")
    result_activity_id = UUID(task.content["result_activity_id"])

    target_dossier = await repo.get_dossier(target_dossier_id)
    target_plugin = registry.get(target_dossier.workflow) if target_dossier else plugin

    target_act_def = target_plugin.find_activity_def(target_activity_type)
    if not target_act_def:
        raise ValueError(f"Target activity not found: {target_activity_type}")

    source_uri = f"urn:dossier:{dossier_id}"
    from ..prov.iris import activity_full_iri
    informed_by_uri = (
        activity_full_iri(dossier_id, task.generated_by)
        if task.generated_by else None
    )

    generated_items: list[dict] = []
    if hasattr(task_result, "content") and task_result.content:
        generates = target_act_def.get("generates", [])
        if generates:
            generated_items = [{
                "entity": str(EntityRef(
                    type=generates[0],
                    entity_id=uuid4(),
                    version_id=uuid4(),
                )),
                "content": task_result.content,
            }]

    await execute_activity(
        plugin=target_plugin,
        activity_def=target_act_def,
        repo=repo,
        dossier_id=target_dossier_id,
        activity_id=result_activity_id,
        user=SYSTEM_USER,
        role="systeem",
        used_items=[{"entity": source_uri}],
        generated_items=generated_items,
        informed_by=informed_by_uri,
        caller=Caller.SYSTEM,
    )
    await repo.session.flush()

    result_uri = activity_full_iri(target_dossier_id, result_activity_id)
    await complete_task(
        repo, plugin, dossier_id, task,
        status="completed",
        result_uri=result_uri,
        informed_by=result_uri,
    )
    logger.info(
        f"Task {task.id}: cross-dossier activity "
        f"{target_activity_type} in {target_dossier_id}"
    )


async def _resolve_triggering_user(
    repo: Repository,
    activity_id: UUID | None,
) -> User:
    """Resolve the triggering-user attribution for a worker-run context.

    The worker *executes as* the system (``SYSTEM_USER``), but audit
    events emitted by task handlers should be attributed to the person
    whose activity caused the task to be scheduled — the aanvrager whose
    cross-dossier ``file_id`` triggered a 403, the behandelaar whose
    decision scheduled a notification task, and so on. That's what
    ``ActivityContext.triggering_user`` carries; this helper builds it
    from the triggering activity's first association row.

    Returns a skeletal ``User`` (id/type/name from the association,
    empty roles/properties) because the audit emitter uses identity
    only — it doesn't need a live permission view of the user's
    current role set. See the "identity only" design note on
    ``AssociationRow`` semantics: that row is the PROV record of
    "who did this activity," and PROV is the source of truth for
    attribution even if the user's current role set has drifted.

    Falls back to ``SYSTEM_USER`` if:
    * ``activity_id`` is ``None`` (no triggering activity — e.g. a task
      synthesised by a bootstrap migration), or
    * the activity has no association row (should not happen in
      practice — every activity writes an association — but the
      defensive fallback keeps task handlers running under a valid
      User instead of crashing with AttributeError deep in emit code).
    """
    if activity_id is None:
        return SYSTEM_USER
    result = await repo.session.execute(
        select(AssociationRow)
        .where(AssociationRow.activity_id == activity_id)
        .limit(1)
    )
    assoc = result.scalar_one_or_none()
    if assoc is None:
        return SYSTEM_USER
    return User(
        id=assoc.agent_id,
        type=assoc.agent_type or "unknown",
        name=assoc.agent_name or assoc.agent_id,
        roles=[],
        properties={},
    )
