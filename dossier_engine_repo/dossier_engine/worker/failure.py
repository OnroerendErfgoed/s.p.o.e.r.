"""
Failure handling — retry scheduling, dead-lettering, and requeue.

Five functions covering the failure path:

* ``_compute_next_attempt_at`` — exponential backoff for retries.
* ``_record_failure`` — write a failure outcome to the task, with
  retry/dead-letter decision.
* ``_is_missing_schema_error`` — classify exceptions that mean
  "schema is out of date" (so we don't retry indefinitely).
* ``_select_dead_lettered_tasks`` — the query for requeue operations.
* ``requeue_dead_letters`` — manual requeue entry point used by ops.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select

from ..auth import User, SYSTEM_USER
from ..db.models import AssociationRow, EntityRow, Repository
from ..engine import ActivityContext, Caller, execute_activity
from ..engine.refs import EntityRef
from ..observability.sentry import (
    capture_task_retry,
    capture_task_dead_letter,
)

from .task_kinds import complete_task

logger = logging.getLogger("dossier.worker")


def _compute_next_attempt_at(
    attempt_count: int,
    base_delay_seconds: int,
    now: datetime,
) -> datetime:
    """Compute the next retry time for a task that just failed its
    `attempt_count`'th attempt.

    Uses exponential backoff with ±10% jitter:
        delay = base * 2**(attempt_count - 1) * (1 + random(-0.1, 0.1))

    `attempt_count` is the count AFTER the failure — so a task that
    just failed its first attempt passes `attempt_count=1` and gets
    a delay of ~base, a second failure (attempt_count=2) gets ~2×base,
    a third gets ~4×base, and so on. The jitter prevents the thundering
    herd effect where many tasks that failed at the same time all
    retry at the same moment.
    """
    exponent = max(0, attempt_count - 1)
    base_delay = base_delay_seconds * (2 ** exponent)
    jitter = random.uniform(-0.1, 0.1)
    delay = base_delay * (1 + jitter)
    return now + timedelta(seconds=delay)


async def _record_failure(
    repo: Repository,
    plugin,
    dossier_id: UUID,
    task: EntityRow,
    error: Exception,
) -> None:
    """Record a task execution failure by writing a new task version
    through the engine's `systemAction` pathway.

    Increments `attempt_count`. If the new count has reached the
    task's `max_attempts` budget, the new task version is written
    with `status = "dead_letter"` — the task is terminal and won't
    be picked up by the poll loop again. Otherwise, the new version
    stays in `status = "scheduled"` but gains a `next_attempt_at`
    field set by `_compute_next_attempt_at`, so the poll loop skips
    it until the retry delay elapses.

    Error telemetry goes to the Python logging system via
    `logger.exception(...)`, which captures the full traceback and
    sends it through whatever handlers are installed — in production
    that's typically Sentry via `sentry_sdk`'s logging integration.
    The task content itself carries only operational state
    (`attempt_count`, `last_attempt_at`, `next_attempt_at`); the
    full error history for a task is reconstructed from the
    telemetry backend keyed by `task_id`.

    The new task version is written via `complete_task` (which itself
    goes through `execute_activity` — see sub-step 5), so the
    failure write path inherits all the engine's invariants and the
    retry is visible in the PROV graph as a regular `systemAction`.
    """
    now = datetime.now(timezone.utc)
    current_count = task.content.get("attempt_count")
    attempt_count = (current_count if current_count is not None else 0) + 1
    max_attempts_val = task.content.get("max_attempts")
    max_attempts = max_attempts_val if max_attempts_val is not None else 3
    base_delay_val = task.content.get("base_delay_seconds")
    base_delay = base_delay_val if base_delay_val is not None else 60

    # Context carried into log records so Sentry (or whatever backend)
    # can index events by task/dossier/attempt.
    log_extra = {
        "task_id": str(task.id),
        "task_entity_id": str(task.entity_id),
        "dossier_id": str(dossier_id),
        "function": task.content.get("function"),
        "kind": task.content.get("kind"),
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
    }

    extra_content = {
        "attempt_count": attempt_count,
        "last_attempt_at": now.isoformat(),
    }

    if attempt_count >= max_attempts:
        # ERROR-level with exc_info=True so the stack trace rides
        # along to whatever log backend is configured. Sentry's
        # logging integration promotes ERROR+exc_info events to
        # full Sentry events with structured tags from `extra`.
        logger.error(
            "Task %s: attempt %d/%d failed, moving to dead_letter",
            task.id, attempt_count, max_attempts,
            exc_info=error, extra=log_extra,
        )
        # Explicit Sentry event with per-task fingerprint: each
        # dead-lettered task is its own issue (operator needs to
        # investigate/fix/requeue individually).
        capture_task_dead_letter(
            exc=error,
            task_id=task.id,
            task_entity_id=task.entity_id,
            dossier_id=dossier_id,
            function=task.content.get("function"),
            attempt_count=attempt_count,
            max_attempts=max_attempts,
        )
        await complete_task(
            repo, plugin, dossier_id, task,
            status="dead_letter",
            extra_content=extra_content,
        )
    else:
        next_attempt_at = _compute_next_attempt_at(
            attempt_count, base_delay, now,
        )
        extra_content["next_attempt_at"] = next_attempt_at.isoformat()
        # WARNING-level for transient retries — Sentry typically
        # drops warnings by default so the noise floor stays
        # reasonable during flaky-infrastructure events. ERROR comes
        # only when we actually give up (dead_letter branch above).
        logger.warning(
            "Task %s: attempt %d/%d failed, retry at %s",
            task.id, attempt_count, max_attempts, next_attempt_at.isoformat(),
            exc_info=error, extra=log_extra,
        )
        # Explicit Sentry event with per-function fingerprint: all
        # retries of the same task function collapse into ONE issue
        # (event count reflects retry rate — signal, not noise).
        capture_task_retry(
            exc=error,
            task_id=task.id,
            task_entity_id=task.entity_id,
            dossier_id=dossier_id,
            function=task.content.get("function"),
            attempt_count=attempt_count,
            max_attempts=max_attempts,
        )
        await complete_task(
            repo, plugin, dossier_id, task,
            status="scheduled",  # back to scheduled for retry
            extra_content=extra_content,
        )



def _is_missing_schema_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is Postgres' ``undefined_table``
    (SQLSTATE 42P01).

    We check via the standard SQLSTATE rather than matching on the
    error message text, which varies across locales and versions.
    SQLAlchemy wraps the asyncpg exception in ``DBAPIError``; the
    original exception is reachable via ``.orig`` (and, in some
    driver/wrapper combinations, via ``__cause__``).

    Returning True here during startup means "ignore the error, try
    again after the poll interval." It must stay tightly scoped —
    any wider and we'd mask real data-integrity bugs as "schema
    not ready."
    """
    # Local import so this module doesn't become a hard dependency
    # on SQLAlchemy's exception hierarchy if callers import it
    # without having used the engine yet.
    from sqlalchemy.exc import DBAPIError

    if not isinstance(exc, DBAPIError):
        return False
    for candidate in (getattr(exc, "orig", None), exc.__cause__):
        sqlstate = getattr(candidate, "sqlstate", None)
        if sqlstate == "42P01":
            return True
    return False


async def _select_dead_lettered_tasks(
    session,
    dossier_id: UUID | None = None,
    task_entity_id: UUID | None = None,
) -> list[EntityRow]:
    """Select dead-lettered tasks (latest version per logical task
    where `status = 'dead_letter'`), optionally filtered by dossier
    and/or task entity id.

    Extracted from `requeue_dead_letters` so integration tests can
    exercise the selection logic against a real database without
    triggering the config-load / `init_db` bootstrap that the main
    entry point does. The two callers — `requeue_dead_letters` and
    the test suite — share the same query shape, so a regression in
    one is a regression in both.

    The query mirrors `_build_scheduled_task_query`: a `MAX(created_at)
    per entity_id` subquery identifies the latest version of each
    logical task, and the outer query joins against it and filters
    on the JSONB status field. The FOR UPDATE variant isn't used
    here because the requeue is a single administrative operation —
    no concurrent-worker locking is needed.
    """
    latest_per_entity = (
        select(
            EntityRow.entity_id.label("eid"),
            func.max(EntityRow.created_at).label("latest_at"),
        )
        .where(EntityRow.type == "system:task")
        .group_by(EntityRow.entity_id)
        .subquery()
    )
    stmt = (
        select(EntityRow)
        .join(
            latest_per_entity,
            (EntityRow.entity_id == latest_per_entity.c.eid)
            & (EntityRow.created_at == latest_per_entity.c.latest_at),
        )
        .where(EntityRow.type == "system:task")
        .where(EntityRow.content["status"].as_string() == "dead_letter")
    )
    if dossier_id is not None:
        stmt = stmt.where(EntityRow.dossier_id == dossier_id)
    if task_entity_id is not None:
        stmt = stmt.where(EntityRow.entity_id == task_entity_id)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def requeue_dead_letters(
    config_path: str,
    dossier_id: UUID | None = None,
    task_entity_id: UUID | None = None,
) -> int:
    """Requeue dead-lettered tasks by writing fresh task revisions
    with reset retry state.

    Scope filters (mutually inclusive):
    * `dossier_id=None, task_entity_id=None` — every dead-lettered
      task across every dossier
    * `dossier_id=X` — every dead-lettered task in one dossier
    * `task_entity_id=Y` — one specific task by its logical entity id
    * `dossier_id=X, task_entity_id=Y` — both filters applied (the
      task must be in the given dossier AND match the entity id)

    Semantics. Each matching task gets a new version written via a
    `systemAction` activity per dossier. The new version has:

    * `status = "scheduled"`  — claimable again by the poll loop
    * `attempt_count = 0`     — fresh retry budget
    * `next_attempt_at = None` — no retry delay, immediately due
    * `last_attempt_at` preserved from the dead-lettered version so
      operators can still see when the task last tried
    * `scheduled_for` preserved from the original task — it's the
      historical record of when the task was first queued, not a
      retry-scheduling field

    The requeue goes through `execute_activity` (like every other
    task-content write now) so each dossier's requeue operation is
    auditable in its PROV graph as a `systemAction` with N task
    revisions plus one `system:note` explaining the bulk requeue.

    Returns the total number of tasks requeued across all dossiers.
    """
    config, registry = load_config_and_registry(config_path)
    db_url = config.get("database", {}).get("url")
    if not db_url:
        raise RuntimeError(
            "database.url is required in config (Postgres connection string)"
        )
    await init_db(db_url)
    # Schema is managed by Alembic migrations (run via the API startup
    # or `alembic upgrade head`). The worker does not create or migrate
    # tables — it only needs the engine connection.

    session_factory = get_session_factory()

    async with session_factory() as session:
        dead_letters = await _select_dead_lettered_tasks(
            session, dossier_id=dossier_id, task_entity_id=task_entity_id,
        )

    if not dead_letters:
        logger.info("requeue_dead_letters: no dead-lettered tasks match")
        return 0

    # Group by dossier so each dossier's requeue is a single
    # systemAction. Writing the requeue as one activity per dossier
    # matches the auditing story — an operator running a bulk requeue
    # against the whole database gets one PROV event per dossier
    # touched, listing every task that was requeued.
    by_dossier: dict[UUID, list[EntityRow]] = {}
    for task in dead_letters:
        by_dossier.setdefault(task.dossier_id, []).append(task)

    logger.info(
        "requeue_dead_letters: requeuing %d task(s) across %d dossier(s)",
        len(dead_letters), len(by_dossier),
    )

    total = 0
    for d_id, tasks in by_dossier.items():
        async with session_factory() as session:
            async with session.begin():
                repo = Repository(session)
                dossier = await repo.get_dossier(d_id)
                if not dossier:
                    logger.error(
                        "requeue_dead_letters: dossier %s not found, "
                        "skipping %d tasks", d_id, len(tasks),
                    )
                    continue
                plugin = registry.get(dossier.workflow)
                if not plugin:
                    logger.error(
                        "requeue_dead_letters: plugin not found for "
                        "workflow %s, skipping %d tasks",
                        dossier.workflow, len(tasks),
                    )
                    continue

                systemaction_def = plugin.find_activity_def("systemAction")
                if not systemaction_def:
                    raise RuntimeError(
                        "systemAction activity definition not found — "
                        "engine should register it at startup"
                    )

                generated_items: list[dict] = []
                task_refs_for_note: list[str] = []
                for task in tasks:
                    # Build a fresh-start revision: status back to
                    # scheduled, attempt_count reset, next_attempt_at
                    # cleared so the poll loop treats it as immediately
                    # due on the scheduled_for axis. last_attempt_at
                    # preserved for the "when did this last try"
                    # diagnostic query. scheduled_for preserved as
                    # historical record.
                    new_content = dict(task.content)
                    new_content["status"] = "scheduled"
                    new_content["attempt_count"] = 0
                    new_content["next_attempt_at"] = None
                    new_version_id = uuid4()
                    generated_items.append({
                        "entity": str(EntityRef(
                            type="system:task",
                            entity_id=task.entity_id,
                            version_id=new_version_id,
                        )),
                        "content": new_content,
                        "derivedFrom": str(EntityRef(
                            type="system:task",
                            entity_id=task.entity_id,
                            version_id=task.id,
                        )),
                    })
                    task_refs_for_note.append(str(task.entity_id))

                # One system:note per bulk requeue, describing the
                # scope and listing the task entity ids.
                note_entity_id = uuid4()
                note_version_id = uuid4()
                scope_desc = []
                if dossier_id is not None:
                    scope_desc.append(f"dossier={dossier_id}")
                if task_entity_id is not None:
                    scope_desc.append(f"task={task_entity_id}")
                scope_str = ", ".join(scope_desc) if scope_desc else "all dossiers"
                generated_items.append({
                    "entity": str(EntityRef(
                        type="system:note",
                        entity_id=note_entity_id,
                        version_id=note_version_id,
                    )),
                    "content": {
                        "text": (
                            f"Operator requeue of {len(tasks)} dead-lettered "
                            f"task(s) (scope: {scope_str}). Task entity ids: "
                            f"{task_refs_for_note}"
                        ),
                    },
                })

                await execute_activity(
                    plugin=plugin,
                    activity_def=systemaction_def,
                    repo=repo,
                    dossier_id=d_id,
                    activity_id=uuid4(),
                    user=SYSTEM_USER,
                    role="systeem",
                    used_items=[],
                    generated_items=generated_items,
                    caller=Caller.SYSTEM,
                )

        logger.info(
            "requeue_dead_letters: dossier %s — requeued %d task(s)",
            d_id, len(tasks),
        )
        total += len(tasks)

    logger.info("requeue_dead_letters: done, %d task(s) requeued total", total)
    return total


