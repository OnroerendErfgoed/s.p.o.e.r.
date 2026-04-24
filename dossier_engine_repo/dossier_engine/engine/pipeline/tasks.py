"""
Task scheduling and cancellation.

After persistence and side effects, the engine processes the activity's
task list — both YAML-declared (`activity_def.tasks`) and
handler-appended (`HandlerResult.tasks`). Tasks fall into four kinds:

* **fire_and_forget** — execute a `task_handler` function inline, no
  record. Errors are swallowed (the name is literal: fire-and-forget).
* **recorded** — execute a `task_handler` function and record a
  `system:task` entity capturing the result. (Note: the recorded
  variant is currently scheduled but the actual execution is left to
  the worker — this engine phase only writes the task entity.)
* **scheduled_activity** — schedule a future activity to run via the
  worker. The task entity carries `target_activity` and
  `scheduled_for`, plus `cancel_if_activities` controlling when it
  should be cancelled.
* **cross_dossier_activity** — same as scheduled_activity but the
  worker is expected to dispatch it against a different dossier.

Two pieces of cross-cutting machinery apply to recorded /
scheduled / cross-dossier tasks:

1. **Supersession.** Unless `allow_multiple: true`, scheduling a new
   task with the same `target_activity` as an existing scheduled task
   in this dossier supersedes the old one — its content is rewritten
   with `status: superseded` so it won't be picked up by the worker.
   Only one scheduled instance of a given `target_activity` per dossier
   can be queued at a time.

2. **Cancellation** (step 16). After the new tasks are written, walk
   every existing `system:task` entity in the dossier and check
   whether the canceling activity (the one we just ran) is in its
   `cancel_if_activities` list. If so, AND the task was scheduled
   before this activity started, mark it cancelled. `allow_multiple`
   does not affect cancellation — a task being allowed to coexist
   with others of its type doesn't change whether the event it's
   waiting on has fired.
"""

from __future__ import annotations

import logging
from datetime import timezone
from uuid import UUID, uuid4

from ..context import ActivityContext, HandlerResult
from ..errors import ActivityError
from ..state import ActivityState
from ...db.models import EntityRow
from ...entities import TaskEntity

_log = logging.getLogger("dossier.engine.tasks")


async def process_tasks(state: ActivityState) -> None:
    """Schedule every task the activity declared (YAML + handler-appended).

    Walks `state.activity_def["tasks"]` and `state.handler_result.tasks`
    in order. For each task:

    * **fire_and_forget**: invoke the registered task_handler function
      inline, swallowing any exception.
    * **other kinds**: supersede any existing scheduled task with the
      same `target_activity` (unless `allow_multiple`), then write a
      new `system:task` entity carrying the full task descriptor.

    Reads:  state.activity_def, state.handler_result, state.plugin,
            state.repo, state.dossier_id, state.activity_id,
            state.resolved_entities
    Writes: nothing on `state` directly; persists `system:task`
            entities to the database.
    """
    all_task_defs = list(state.activity_def.get("tasks", []))
    if isinstance(state.handler_result, HandlerResult):
        all_task_defs.extend(state.handler_result.tasks)

    for task_def in all_task_defs:
        task_kind = task_def.get("kind", "recorded")

        if task_kind == "fire_and_forget":
            await _fire_and_forget(state, task_def)
        else:
            await _schedule_recorded_task(state, task_def, task_kind)


async def _fire_and_forget(state: ActivityState, task_def: dict) -> None:
    """Execute a fire-and-forget task handler inline.

    Errors are swallowed by design — fire_and_forget is for things
    like "send a notification" where a transient failure shouldn't
    bring down the entire activity.
    """
    fn_name = task_def.get("function")
    if not fn_name:
        return
    fn = state.plugin.task_handlers.get(fn_name)
    if fn is None:
        return

    ctx = ActivityContext(
        repo=state.repo,
        dossier_id=state.dossier_id,
        used_entities=state.resolved_entities,
        entity_models=state.plugin.entity_models,
        plugin=state.plugin,
        # fire_and_forget runs during the request pipeline (not in the
        # worker) — executor and trigger are both the request-maker.
        user=state.user,
        triggering_user=state.user,
    )
    try:
        await fn(ctx)
    except Exception:
        # Fire-and-forget: swallow by design (see docstring). Log with
        # traceback so "a notification never arrived" is investigable
        # rather than silent. Logged at WARNING, not ERROR, because the
        # activity itself did succeed — this handler's failure doesn't
        # change any invariant the caller cared about. Sentry's
        # LoggingIntegration picks WARNING up as a breadcrumb and (if
        # promoted) an event; either way it stops being invisible.
        _log.warning(
            f"fire_and_forget task '{fn_name}' raised (swallowed by design)",
            exc_info=True,
        )


async def _schedule_recorded_task(
    state: ActivityState, task_def: dict, task_kind: str,
) -> None:
    """Handle supersession and persist the task entity."""
    # Resolve scheduled_for: accepts "+20d"/"+2h"/"+45m"/"+3w" relative
    # offsets (resolved against state.now) or absolute ISO 8601. Raises
    # ValueError on a malformed value so YAML typos fail loudly at
    # activity execution time instead of silently scheduling for "now".
    from ..scheduling import resolve_scheduled_for
    try:
        resolved_scheduled_for = resolve_scheduled_for(
            task_def.get("scheduled_for"), state.now,
        )
    except ValueError as e:
        raise ActivityError(
            500,
            f"Bad task declaration in workflow YAML: {e}",
        ) from None

    task_content = TaskEntity(
        kind=task_kind,
        function=task_def.get("function"),
        target_activity=task_def.get("target_activity"),
        scheduled_for=resolved_scheduled_for,
        cancel_if_activities=task_def.get("cancel_if_activities", []),
        allow_multiple=task_def.get("allow_multiple", False),
        result_activity_id=str(uuid4()),
        status="scheduled",
    )

    if not task_content.allow_multiple and task_content.target_activity:
        await _supersede_matching(state, task_content)

    await state.repo.create_entity(
        version_id=uuid4(),
        entity_id=uuid4(),
        dossier_id=state.dossier_id,
        type="system:task",
        generated_by=state.activity_id,
        content=task_content.model_dump(),
        attributed_to="system",
    )


async def _supersede_matching(
    state: ActivityState, new_task: TaskEntity,
) -> None:
    """Mark any existing scheduled task with the same target_activity
    as superseded.

    Two tasks supersede each other when they share `target_activity`
    within the same dossier. The supersession writes a new revision of
    the existing task entity with `status: superseded`, so only one
    scheduled instance of a given target per dossier is ever on the
    worker's queue at a time.

    Uses a flat `get_entities_by_type` query and dedupes in Python
    instead of a SQL GROUP BY — faster for the small task lists we
    typically deal with.
    """
    rows = await state.repo.get_entities_by_type(state.dossier_id, "system:task")
    latest: dict[UUID, EntityRow] = {}
    for row in rows:
        existing = latest.get(row.entity_id)
        if existing is None or row.created_at > existing.created_at:
            latest[row.entity_id] = row

    for existing in latest.values():
        if not existing.content:
            continue
        if existing.content.get("status") != "scheduled":
            continue
        if existing.content.get("target_activity") != new_task.target_activity:
            continue

        # Same target → supersede.
        superseded_content = dict(existing.content)
        superseded_content["status"] = "superseded"
        await state.repo.create_entity(
            version_id=uuid4(),
            entity_id=existing.entity_id,
            dossier_id=state.dossier_id,
            type="system:task",
            generated_by=state.activity_id,
            content=superseded_content,
            derived_from=existing.id,
            attributed_to="system",
        )


async def cancel_matching_tasks(state: ActivityState) -> None:
    """Walk every existing scheduled task and cancel those whose
    `cancel_if_activities` includes the activity we just ran.

    Cancellation fires whenever the canceling activity runs in the same
    dossier. `allow_multiple` does not affect cancellation — a task
    being allowed to coexist with others of its type doesn't change
    whether the event it's waiting on has fired.

    Tasks created at-or-after this activity's start time are skipped
    — we don't cancel tasks the activity itself just scheduled.

    Reads:  state.repo, state.dossier_id, state.activity_def,
            state.activity_id, state.now
    Writes: nothing on `state`; persists cancellation revisions of
            `system:task` entities.
    """
    rows = await state.repo.get_entities_by_type(state.dossier_id, "system:task")
    latest_by_eid: dict[UUID, EntityRow] = {}
    for row in rows:
        existing = latest_by_eid.get(row.entity_id)
        if existing is None or row.created_at > existing.created_at:
            latest_by_eid[row.entity_id] = row

    for task_entity in latest_by_eid.values():
        if not task_entity.content:
            continue
        if task_entity.content.get("status") != "scheduled":
            continue

        cancel_list = task_entity.content.get("cancel_if_activities", [])
        # Compare by local name so bare names from handler code
        # (``cancel_if_activities: ["vervolledigAanvraag"]``) match
        # the qualified name on the current activity definition
        # (``oe:vervolledigAanvraag``). Plugin authors can write
        # either form without caring about normalization.
        from ...prov.activity_names import local_name
        current_local = local_name(state.activity_def["name"])
        cancel_locals = {local_name(n) for n in cancel_list}
        if current_local not in cancel_locals:
            continue

        # Skip tasks created at-or-after this activity's start. Don't
        # cancel things this activity itself just scheduled.
        task_created = task_entity.created_at
        if task_created is None:
            continue
        if task_created.tzinfo is None:
            task_created = task_created.replace(tzinfo=timezone.utc)
        if task_created >= state.now:
            continue

        cancelled_content = dict(task_entity.content)
        cancelled_content["status"] = "cancelled"
        await state.repo.create_entity(
            version_id=uuid4(),
            entity_id=task_entity.entity_id,
            dossier_id=state.dossier_id,
            type="system:task",
            generated_by=state.activity_id,
            content=cancelled_content,
            derived_from=task_entity.id,
            attributed_to="system",
        )
