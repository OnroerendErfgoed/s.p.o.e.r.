"""
Integration tests for `_supersede_matching` in
`engine.pipeline.tasks`.

The supersede machinery runs during `process_tasks`: when an
activity schedules a new task, any existing scheduled task with
the same `target_activity` gets marked `superseded`. This
prevents duplicate scheduled tasks for the same logical action —
a reminder scheduled twice shouldn't fire twice. Only one
scheduled instance of a given `target_activity` per dossier is
ever on the worker's queue at a time.

`allow_multiple` bypasses the supersede step entirely — that's
the caller's responsibility in `process_tasks`, not in
`_supersede_matching` itself, so we don't cover it here.

Branches:

* `no_existing_tasks_noop` — baseline, nothing to supersede.
* `different_target_activity_not_superseded` — target differs.
  The two tasks are about different things, both should live.
* `same_target_superseded` — the core match. Old task gets
  `status: superseded`, new task is unaffected (the caller
  writes it afterward).
* `already_superseded_task_ignored` — an old task with
  `status: superseded` doesn't get a second superseded revision
  written over it. Status check should skip.
* `completed_task_ignored` — an already-completed task isn't
  touched. Not quite the same as "already superseded" but the
  same `status != scheduled` guard covers it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import EntityRow, Repository, AssociationRow
from dossier_engine.engine.pipeline.tasks import _supersede_matching
from dossier_engine.engine.state import ActivityState, Caller
from dossier_engine.entities import TaskEntity


UTC = timezone.utc
D1 = UUID("11111111-1111-1111-1111-111111111111")


async def _bootstrap_dossier_with_activity(repo: Repository) -> UUID:
    """Create a dossier and one systemAction that existing tasks
    can point at as their generator. Returns the activity_id."""
    await repo.create_dossier(D1, "toelatingen")
    act_id = uuid4()
    now = datetime.now(UTC)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _persist_new_activity_for_state(repo: Repository) -> UUID:
    """When `_supersede_matching` writes a supersede revision, it
    uses `state.activity_id` as the `generated_by`. That FK must
    point at a real activity row. This helper creates one and
    returns its id for use in the state stub."""
    act_id = uuid4()
    now = datetime.now(UTC)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="scheduleFollowUp",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _seed_scheduled_task(
    repo: Repository,
    bootstrap_activity_id: UUID,
    *,
    target_activity: str,
    status: str = "scheduled",
) -> tuple[UUID, UUID]:
    """Seed one task entity directly. Returns (entity_id, version_id).
    1ms sleep after insert to ensure distinct created_at if the
    test does multiple seeds."""
    eid = uuid4()
    vid = uuid4()
    content = {
        "kind": "scheduled_activity",
        "target_activity": target_activity,
        "status": status,
        "cancel_if_activities": [],
    }
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type="system:task", generated_by=bootstrap_activity_id,
        content=content, attributed_to="system",
    )
    await repo.session.flush()
    await asyncio.sleep(0.002)
    return eid, vid


async def _latest_task_status(
    repo: Repository, task_entity_id: UUID,
) -> str | None:
    row = await repo.get_latest_entity_by_id(D1, task_entity_id)
    if row is None or not row.content:
        return None
    return row.content.get("status")


def _state(repo: Repository, activity_id: UUID) -> ActivityState:
    """Minimal state for `_supersede_matching`. The phase reads
    `repo`, `dossier_id`, `activity_id`. That's it."""
    return ActivityState(
        plugin=None,
        activity_def={"name": "scheduleFollowUp"},
        repo=repo,
        dossier_id=D1,
        activity_id=activity_id,
        user=None,
        role="",
        used_items=[],
        generated_items=[],
        relation_items=[],
        caller=Caller.CLIENT,
    )


def _new_task(target_activity: str) -> TaskEntity:
    """Build a fully-constructed TaskEntity to pass to
    `_supersede_matching`. The function takes the NEW task's
    shape and uses it to match against existing scheduled tasks."""
    return TaskEntity(
        kind="scheduled_activity",
        target_activity=target_activity,
        status="scheduled",
    )


class TestSupersedeMatching:

    async def test_no_existing_tasks_noop(self, repo):
        """Baseline: no scheduled tasks exist. The function walks
        an empty result set and returns without doing anything.
        Not a correctness test so much as a "doesn't crash on
        empty DB" check."""
        await _bootstrap_dossier_with_activity(repo)
        scheduling_act = await _persist_new_activity_for_state(repo)
        state = _state(repo, scheduling_act)

        new = _new_task("sendReminder")
        await _supersede_matching(state, new)
        await repo.session.flush()
        # No assertion target — just "didn't raise".

    async def test_different_target_activity_not_superseded(self, repo):
        """Existing task has target `sendReminder`, new task has
        target `sendEscalation`. Targets don't match, so no
        supersede fires — the two tasks are about different
        actions."""
        boot = await _bootstrap_dossier_with_activity(repo)
        existing_eid, _ = await _seed_scheduled_task(
            repo, boot,
            target_activity="sendReminder",
        )
        scheduling_act = await _persist_new_activity_for_state(repo)
        state = _state(repo, scheduling_act)

        new = _new_task("sendEscalation")
        await _supersede_matching(state, new)
        await repo.session.flush()

        assert await _latest_task_status(repo, existing_eid) == "scheduled"

    async def test_same_target_superseded(self, repo):
        """THE core match. Target agrees. Old task gets marked
        superseded; its latest version now has
        `status: superseded`."""
        boot = await _bootstrap_dossier_with_activity(repo)
        existing_eid, _ = await _seed_scheduled_task(
            repo, boot,
            target_activity="sendReminder",
        )
        scheduling_act = await _persist_new_activity_for_state(repo)
        state = _state(repo, scheduling_act)

        new = _new_task("sendReminder")
        await _supersede_matching(state, new)
        await repo.session.flush()

        assert await _latest_task_status(repo, existing_eid) == "superseded"

    async def test_already_superseded_task_ignored(self, repo):
        """An existing task that's already superseded must not
        get a second supersede revision. The `status ==
        'scheduled'` guard in the matcher skips it."""
        boot = await _bootstrap_dossier_with_activity(repo)
        existing_eid, _ = await _seed_scheduled_task(
            repo, boot,
            target_activity="sendReminder",
            status="superseded",  # already gone
        )
        scheduling_act = await _persist_new_activity_for_state(repo)
        state = _state(repo, scheduling_act)

        new = _new_task("sendReminder")
        await _supersede_matching(state, new)
        await repo.session.flush()

        # Still superseded — no new revision written on top of it.
        # If a second supersede revision WAS written, the status
        # would still be "superseded" (same value), so we also
        # check the version count to catch the write-happened
        # case.
        row = await repo.get_latest_entity_by_id(D1, existing_eid)
        assert row.content["status"] == "superseded"
        # One version: the original. If supersede fired, there
        # would be two.
        versions = await repo.get_entity_versions(D1, existing_eid)
        assert len(versions) == 1

    async def test_completed_task_ignored(self, repo):
        """A task that's already completed is also skipped by the
        `status != 'scheduled'` guard. Not quite the same branch
        as 'already superseded', but the same code path — worth
        having so a future bug that narrows the guard to
        `!= 'superseded'` specifically (losing the 'completed'
        case) gets caught."""
        boot = await _bootstrap_dossier_with_activity(repo)
        existing_eid, _ = await _seed_scheduled_task(
            repo, boot,
            target_activity="sendReminder",
            status="completed",
        )
        scheduling_act = await _persist_new_activity_for_state(repo)
        state = _state(repo, scheduling_act)

        new = _new_task("sendReminder")
        await _supersede_matching(state, new)
        await repo.session.flush()

        row = await repo.get_latest_entity_by_id(D1, existing_eid)
        assert row.content["status"] == "completed"
        versions = await repo.get_entity_versions(D1, existing_eid)
        assert len(versions) == 1
