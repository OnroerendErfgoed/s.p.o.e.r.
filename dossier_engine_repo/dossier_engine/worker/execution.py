"""
Task execution wrapper.

Three functions that wrap a single claimed task's execution:

* ``process_task`` — the high-level entry point called by the loop.
* ``_execute_claimed_task`` — mid-level: opens the transaction,
  delegates to the task_kinds.py handlers by kind, rolls back on error.
* ``_refetch_task`` — re-read the task row inside the transaction
  (handles cancel/supersede races).

``_resolve_triggering_user`` lives in ``task_kinds.py`` (it's consumed
by the per-kind handlers and would create a circular import if kept
here).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select

from ..auth import User, SYSTEM_USER
from ..db.models import AssociationRow, EntityRow, Repository
from ..engine import ActivityContext, Caller, execute_activity

from .task_kinds import (
    complete_task, _process_recorded,
    _process_scheduled_activity, _process_cross_dossier,
)
from .failure import _record_failure

logger = logging.getLogger("dossier.worker")


async def process_task(task: EntityRow, registry, config):
    """Legacy entry point — opens its own session and calls the
    session-aware inner function. Kept for callers and tests that
    want to process a task without owning the transaction themselves.
    The production worker loop uses `_execute_claimed_task` directly
    so the claim-lock-execute dance all happens in one transaction.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        async with session.begin():
            await _execute_claimed_task(session, task, registry)


async def _execute_claimed_task(session, task: EntityRow, registry) -> None:
    """Execute a task within an already-open transaction.

    The caller is responsible for the `async with session.begin()`
    block. This lets the worker loop hold the row-level lock acquired
    by `_claim_one_due_task` through the entire execution — if the
    caller opened a new transaction per task, the lock would be
    released between claim and execute and two workers could race.

    Responsibilities inside this function:
    * Acquire the dossier `FOR UPDATE` lock **before** doing anything
      that could trigger INSERTs into `entities` (which take FK-induced
      `FOR KEY SHARE` locks on referenced rows). Bug 74: user-facing
      activities take the dossier lock first, then insert entities.
      If the worker does the reverse — entity lock from
      ``_claim_one_due_task``'s ``FOR UPDATE OF entities``, then the
      dossier lock via the pipeline's ``ensure_dossier`` — the two
      lock orders invert and two concurrent transactions on the same
      dossier can deadlock. Grabbing the dossier lock here forces the
      worker into the same order as user activities
      (dossier → entities) so both paths are deadlock-free.
      The later ``get_dossier_for_update`` inside the pipeline is a
      no-op in the same transaction — Postgres is idempotent about
      re-locking a row you already hold.
    * Resolve the dossier and plugin.
    * Re-fetch the task for latest version. The re-fetch is how we
      observe cancellations: the pipeline's `cancel_matching_tasks`
      runs synchronously as part of every activity that could cancel
      a task, so if the task was cancelled between when the poll
      selected it and when we got the row lock, the latest version's
      status will be `cancelled` (not `scheduled`) and we return
      early. No separate cancel check is needed — the status guard
      below handles it uniformly with other "status already changed"
      cases.
    * Dispatch on `kind` to the appropriate `_process_*` handler.

    Raises on execution failure. The caller's error handler in the
    worker loop catches the exception and routes it through
    `_record_failure`, which decides retry-vs-dead-letter and writes
    the new task version via `complete_task → execute_activity`.
    """
    repo = Repository(session)
    dossier_id = task.dossier_id

    # Acquire the dossier lock in the same order user-facing
    # activities do. See Bug 74 in the review and the block-level
    # docstring above for the full deadlock explanation.
    dossier = await repo.get_dossier_for_update(dossier_id)
    if not dossier:
        logger.error(f"Task {task.id}: dossier {dossier_id} not found")
        return

    plugin = registry.get(dossier.workflow)
    if not plugin:
        logger.error(
            f"Task {task.id}: plugin not found for "
            f"workflow {dossier.workflow}"
        )
        return

    current_task = await _refetch_task(repo, dossier_id, task.entity_id)
    if current_task is None:
        logger.warning(f"Task {task.id}: not found in re-fetch")
        return
    if current_task.content.get("status") != "scheduled":
        logger.info(
            f"Task {task.id}: already "
            f"{current_task.content.get('status')}, skipping"
        )
        return

    kind = current_task.content.get("kind")
    logger.info(
        f"Task {task.id}: processing kind={kind} "
        f"function={current_task.content.get('function')}"
    )

    if kind == "recorded":
        await _process_recorded(repo, plugin, dossier_id, current_task)
    elif kind == "scheduled_activity":
        await _process_scheduled_activity(
            repo, plugin, dossier_id, current_task,
        )
    elif kind == "cross_dossier_activity":
        await _process_cross_dossier(
            repo, plugin, registry, dossier_id, current_task,
        )
    else:
        logger.warning(f"Task {task.id}: unknown kind '{kind}'")


async def _refetch_task(
    repo: Repository,
    dossier_id: UUID,
    task_entity_id: UUID,
) -> EntityRow | None:
    """Pull the latest version of one logical task entity inside the
    current transaction.

    Returns None if the task doesn't exist or has no content.

    History note: an earlier implementation used
    `get_entities_by_type(dossier_id, "system:task")` and then looped
    through the results in Python looking for a matching entity_id.
    That version was buggy in two ways — it fetched every task row
    in the dossier just to find one, and
    `get_entities_by_type` orders by `created_at ASC` and the loop
    returned the first match, so for any task with multiple versions
    it returned the OLDEST version instead of the latest. The bug
    was invisible for a long time because the completion path was
    only reached by single-version tasks in the test suite, and the
    retry path in `_record_failure` doesn't go through `_refetch_task`
    at all — it gets the claimed (latest) task from the outer loop
    directly. The bug only surfaced when the requeue feature created
    a multi-version task that then hit the success path.
    """
    task = await repo.get_latest_entity_by_id(dossier_id, task_entity_id)
    if task is None or not task.content:
        return None
    return task


