"""
Polling + due-task claiming.

Five functions that decide "what's due, and which one do I claim?":

* ``_parse_scheduled_for`` — ISO datetime parsing with tolerance for
  Z-suffix and +00:00 forms.
* ``_build_scheduled_task_query`` — the base SELECT for scheduled
  tasks (optionally FOR UPDATE SKIP LOCKED for claiming).
* ``_is_task_due`` — point-in-time due check used for deciding
  whether to advance to execute.
* ``find_due_tasks`` — read-only snapshot of due tasks.
* ``_claim_one_due_task`` — the atomic claim for execution, using
  SKIP LOCKED to avoid worker contention.
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

logger = logging.getLogger("dossier.worker")



def _parse_scheduled_for(value: str | None) -> datetime | None:
    """Parse a `scheduled_for` value into an aware datetime.

    The engine writes `scheduled_for` as an ISO 8601 string. Depending
    on who produced it, the string can look like `2026-05-01T00:00:00Z`
    (Python-ish with trailing Z), `2026-05-01T00:00:00+00:00` (also
    Python-ish, datetime.isoformat with UTC tz), or `2026-05-01T00:00:00`
    (naive, which we treat as UTC). Comparing these as strings is
    wrong — `"Z" > "+"` lexically, so a "+00:00"-formatted now can
    compare greater than a "Z"-formatted scheduled_for even when
    they're the same instant.

    Three return shapes, all consumed by the ``> now`` comparison in
    ``_is_task_due``:

    * ``None`` — value is None/empty. Represents "no scheduling
      constraint was set"; the caller treats this as "immediately
      due" (the common case — most tasks have no ``scheduled_for``
      and fire on the next poll).

    * ``datetime.max`` (aware, UTC) — value was a non-empty string
      that didn't parse as ISO 8601. This is **Bug 12**: until this
      change, the malformed branch returned ``None``, which collapsed
      into case 1 and caused a task intended for next week to fire
      immediately. The engine always writes via ``resolve_scheduled_for``
      which validates before persisting, so if we see a malformed
      string here, it's either row corruption, a pre-migration legacy
      row, or a tampered dump — none of which should silently advance
      the clock. Returning ``datetime.max`` defers the task indefinitely
      (``max > now`` is always true), keeping the row visible to
      operator tooling while refusing to execute it until the data is
      fixed. We log loudly so corruption is visible in Sentry.

    * aware ``datetime`` — value parsed. Compared against ``now`` in
      the caller as usual.
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        # Whitespace-only or empty — treat as "no scheduling
        # constraint set," same as None. Not data corruption, just
        # a trivially-empty value.
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Bug 12: log-and-defer. See docstring for rationale.
        logger.error(
            "Malformed scheduled_for / next_attempt_at value %r; "
            "deferring task indefinitely until the row is fixed.",
            value,
        )
        return datetime.max.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _build_scheduled_task_query(for_update: bool = False):
    """Shared SQLAlchemy query builder for due-task selection.

    Filters at the SQL layer to: `type = 'system:task'`, latest version
    per logical entity_id (via a `MAX(created_at)` subquery), and
    `status = 'scheduled'` (via JSONB field extraction that translates
    to `content ->> 'status' = 'scheduled'` on Postgres).

    The `scheduled_for` and `next_attempt_at` fields live in JSONB but
    are compared in Python after hydration because ISO 8601 lexical
    comparison is incorrect (`"Z"` > `"+"` is wrong but string-true).
    `_parse_scheduled_for` handles the parsing.

    When `for_update=True`, adds `FOR UPDATE OF entities SKIP LOCKED`
    so the worker's poll transaction locks the candidate rows and
    concurrent workers skip over them. The `OF entities` clause is
    required because Postgres rejects `FOR UPDATE` on a query whose
    set includes an aggregated subquery — `OF` tells Postgres to lock
    only the outer `entities` table, leaving the subquery's aggregate
    rows alone.
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
        .where(EntityRow.content["status"].as_string() == "scheduled")
    )
    if for_update:
        stmt = stmt.with_for_update(skip_locked=True, of=EntityRow)
    return stmt


def _is_task_due(task: EntityRow, now: datetime) -> tuple[bool, datetime]:
    """Return (is_due, sort_key) for a task row.

    A task is due when:
    * It has no `scheduled_for` (treated as immediately due), AND
    * It has no `next_attempt_at` (first-attempt, no retry delay), OR
    * Both `scheduled_for <= now` and `next_attempt_at <= now` when
      either is present.

    `sort_key` is used to order the due set so the oldest overdue
    task drains first. Priority (earliest first):
    1. `next_attempt_at` if set (retry delay has priority — we want to
       drain retries as soon as they're ready so they don't pile up).
    2. `scheduled_for` if set.
    3. `datetime.min` otherwise (unscheduled = treat as ancient).
    """
    if not task.content:
        return False, datetime.min.replace(tzinfo=timezone.utc)
    scheduled_for = _parse_scheduled_for(task.content.get("scheduled_for"))
    next_attempt_at = _parse_scheduled_for(task.content.get("next_attempt_at"))

    if scheduled_for is not None and scheduled_for > now:
        return False, scheduled_for
    if next_attempt_at is not None and next_attempt_at > now:
        return False, next_attempt_at

    sort_key = (
        next_attempt_at
        or scheduled_for
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return True, sort_key


async def find_due_tasks(session) -> list[EntityRow]:
    """Find all scheduled task entities that are due — read-only,
    non-locking. Used by `--once` drain mode and by observability
    tooling that wants to inspect the backlog without interfering
    with running workers.

    Returns a list sorted by "most overdue first" — see
    `_is_task_due` for the sort-key rule.
    """
    now = datetime.now(timezone.utc)
    stmt = await _build_scheduled_task_query(for_update=False)
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())

    due: list[tuple[datetime, EntityRow]] = []
    for task in candidates:
        is_due, sort_key = _is_task_due(task, now)
        if is_due:
            due.append((sort_key, task))

    due.sort(key=lambda pair: pair[0])
    return [task for _, task in due]


async def _claim_one_due_task(session) -> EntityRow | None:
    """Select and lock one due task row inside the caller's
    transaction.

    Strategy: `SELECT ... FOR UPDATE OF entities SKIP LOCKED LIMIT 5`
    to pull a small batch of candidate rows from the SQL layer, then
    Python-filter through `_is_task_due` and return the first
    actually-due row. Rows that don't pass the Python filter stay
    locked until the transaction commits or rolls back, but the
    bounded `LIMIT 5` caps the over-lock blast radius to 5 rows per
    worker per cycle. Acceptable for a system with many more due
    tasks than concurrent workers.

    Returns None if the query returned nothing or if no candidate
    passes the `scheduled_for` / `next_attempt_at` time filters.
    `None` signals the poll loop "nothing claimable this cycle" — it
    may mean the backlog is empty or it may mean everything that
    was SKIP-LOCKED skippable was genuinely locked; either way the
    loop moves on to the next poll interval.
    """
    now = datetime.now(timezone.utc)
    stmt = (await _build_scheduled_task_query(for_update=True)).limit(5)
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())

    for task in candidates:
        is_due, _ = _is_task_due(task, now)
        if is_due:
            return task
    return None


