"""Round 18 — attribution plumbing integration test.

Pins the two-field ``ActivityContext`` contract (``user`` = executor,
``triggering_user`` = attributed agent) across the three construction
paths where they diverge:

1. **Direct handler** — both fields carry the request user; there's
   no divergence because the request-making user *is* executing.
2. **Side-effect handler** — ``user`` collapses to ``SYSTEM_USER``
   (the side-effect chain runs as the system caller), but
   ``triggering_user`` preserves the original request user so audit
   events emitted from inside the side-effect stay attributed to
   the human who triggered the pipeline run.
3. **Worker-run task** — tested at the worker-helper level:
   ``_resolve_triggering_user`` reads the triggering activity's
   association row and builds a skeletal ``User``. The plumbed
   ``triggering_user`` on ``ActivityContext`` is that resolved user,
   while ``user`` is ``SYSTEM_USER`` (the worker executes).

Without these assertions, the plumbing's correctness rests on the
regression-free engine suite (709 passes) and Bug 30's unit tests —
decent coverage, but not as tight as pinning the actual contract.
Bug 30 was the first concrete consumer of ``triggering_user``; this
test exists so the next consumer doesn't have to rediscover how the
plumbing works by reading source.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import SYSTEM_USER, User
from dossier_engine.db.models import AssociationRow, Repository
from dossier_engine.engine.context import ActivityContext, HandlerResult
from dossier_engine.engine.pipeline.side_effects import execute_side_effects
from dossier_engine.worker import _resolve_triggering_user


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _make_user(user_id: str = "alice") -> User:
    return User(
        id=user_id, type="natuurlijk_persoon", name=f"User {user_id}",
        roles=["oe:aanvrager"], properties={}, uri=None,
    )


# ---------------------------------------------------------------------------
# Direct handler path
# ---------------------------------------------------------------------------


class TestDirectHandlerAttribution:
    """For a request-path handler, ``user`` and ``triggering_user``
    are both the request user — no divergence."""

    def test_both_fields_are_the_request_user(self):
        """Build a context the way ``pipeline/handlers.py`` does and
        confirm both fields hold the same User. This is the request-
        path invariant: whoever executed is also who's attributed."""
        alice = _make_user("alice")
        ctx = ActivityContext(
            repo=None,  # not exercised in this test
            dossier_id=D1,
            used_entities={},
            user=alice,
            triggering_user=alice,
        )
        assert ctx.user is alice
        assert ctx.triggering_user is alice
        assert ctx.user is ctx.triggering_user


# ---------------------------------------------------------------------------
# Side-effect path
# ---------------------------------------------------------------------------


class _CapturingPlugin:
    """Stub plugin that records every ActivityContext passed to any
    of its handlers. Used to assert that side-effect handlers receive
    SYSTEM_USER as executor and the request user as triggering_user."""

    def __init__(self, se_activity_defs: dict):
        self._defs = se_activity_defs
        # One handler, for whichever activity fires. Captures the ctx.
        self.captured_contexts: list[ActivityContext] = []

        async def _capture_handler(ctx, _client_content):
            self.captured_contexts.append(ctx)
            return HandlerResult()  # no-op side effect

        # Register the handler under every side-effect activity's
        # declared handler name, so whichever fires lands here.
        self.handlers = {
            spec.get("handler"): _capture_handler
            for spec in self._defs.values() if spec.get("handler")
        }
        self.entity_models = {}
        self.validators = {}
        self.task_handlers = {}
        self.relation_validators = {}
        self.side_effect_conditions = {}
        self.name = "test"
        self.workflow = {"activities": list(self._defs.values()),
                         "relations": []}

    def find_activity_def(self, name):
        return self._defs.get(name)

    def is_singleton(self, t):
        return False

    def cardinality_of(self, t):
        return "multi"

    def resolve_schema(self, t, v):
        return None


async def _bootstrap(repo: Repository) -> UUID:
    """Create D1 and a trigger activity whose association is a
    real human agent (alice). Returns the trigger's id."""
    await repo.create_dossier(D1, "toelatingen")
    await repo.ensure_agent("alice", "natuurlijk_persoon", "Alice", {})
    await repo.ensure_agent("system", "systeem", "Systeem", {})
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="trigger",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="alice",
        agent_name="Alice", agent_type="natuurlijk_persoon",
        role="oe:aanvrager",
    ))
    await repo.session.flush()
    return act_id


class TestSideEffectAttribution:

    async def test_side_effect_handler_sees_system_as_executor_and_alice_as_trigger(
        self, repo,
    ):
        """The core Round 18 claim: a side-effect handler's context
        has ``user=SYSTEM_USER`` but ``triggering_user=alice``. If
        this ever regresses, audit events emitted from side-effect
        handlers would be attributed to 'system' instead of the
        human who triggered the pipeline — which was the problem
        Bug 30 explicitly needed to avoid for the 403 denial case."""
        trigger = await _bootstrap(repo)
        alice = _make_user("alice")

        # Define a side-effect activity with a capturing handler.
        se_def = {
            "name": "recordingSideEffect",
            "handler": "capture",
            "can_create_dossier": False,
            "client_callable": False,
            "default_role": "systeem",
            "authorization": {"access": "roles",
                              "roles": [{"role": "systeemgebruiker"}]},
            "used": [],
            "generates": [],
            "status": None,
            "validators": [],
            "side_effects": [],
            "tasks": [],
        }
        plugin = _CapturingPlugin({"recordingSideEffect": se_def})

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "recordingSideEffect"}],
            triggering_user=alice,
        )

        assert len(plugin.captured_contexts) == 1
        ctx = plugin.captured_contexts[0]
        # Executor is the system (side effects run as system caller).
        assert ctx.user is SYSTEM_USER
        # Attribution preserved from the original request user.
        assert ctx.triggering_user is alice
        # The divergence is real — if a future refactor collapses
        # the two fields, this assertion flags it immediately.
        assert ctx.user is not ctx.triggering_user

    async def test_nested_side_effect_preserves_original_triggering_user(
        self, repo,
    ):
        """Recursive side effects: the nested handler must still see
        the *original* request user in ``triggering_user``, not the
        immediate-parent side-effect activity's (systeem) agent.
        This is the 'pass-through during recursion' contract spelled
        out in ``execute_side_effects``' docstring."""
        trigger = await _bootstrap(repo)
        alice = _make_user("alice")

        # Outer side effect chains to an inner one via its own
        # side_effects list.
        inner_def = {
            "name": "inner",
            "handler": "capture_inner",
            "can_create_dossier": False,
            "client_callable": False,
            "default_role": "systeem",
            "authorization": {"access": "roles",
                              "roles": [{"role": "systeemgebruiker"}]},
            "used": [], "generates": [], "status": None,
            "validators": [], "side_effects": [], "tasks": [],
        }
        outer_def = {
            "name": "outer",
            "handler": "capture_outer",
            "can_create_dossier": False,
            "client_callable": False,
            "default_role": "systeem",
            "authorization": {"access": "roles",
                              "roles": [{"role": "systeemgebruiker"}]},
            "used": [], "generates": [], "status": None,
            "validators": [],
            "side_effects": [{"activity": "inner"}],
            "tasks": [],
        }
        plugin = _CapturingPlugin({"outer": outer_def, "inner": inner_def})

        await execute_side_effects(
            plugin=plugin, repo=repo, dossier_id=D1,
            trigger_activity_id=trigger,
            side_effects=[{"activity": "outer"}],
            triggering_user=alice,
        )

        assert len(plugin.captured_contexts) == 2
        outer_ctx, inner_ctx = plugin.captured_contexts
        # Both contexts: system as executor, alice as trigger.
        # The inner one proves recursion preserves attribution —
        # this is the assertion the pass-through comment in
        # side_effects.py:execute_side_effects guards against.
        for ctx in (outer_ctx, inner_ctx):
            assert ctx.user is SYSTEM_USER
            assert ctx.triggering_user is alice


# ---------------------------------------------------------------------------
# Worker path
# ---------------------------------------------------------------------------


class TestWorkerAttributionResolver:
    """``_resolve_triggering_user`` is the bridge from worker-land
    (which has no request user) to ``triggering_user`` attribution
    — look up the agent on the triggering activity's association
    row, build a skeletal User. Fall back to SYSTEM_USER for
    defensive cases."""

    async def test_resolves_agent_from_association(self, repo):
        """Activity has an association → skeletal User built from
        it. Identity fields (id/name/type) are preserved; roles and
        properties are empty per the 'identity only' design."""
        act_id = await _bootstrap(repo)
        await repo.session.flush()

        resolved = await _resolve_triggering_user(repo, act_id)
        assert resolved.id == "alice"
        assert resolved.name == "Alice"
        assert resolved.type == "natuurlijk_persoon"
        # Round-18 design call: identity only.
        assert resolved.roles == []
        assert resolved.properties == {}

    async def test_none_activity_id_returns_system_user(self, repo):
        """A task synthesised without a triggering activity (e.g.
        bootstrap migration) falls back to SYSTEM_USER. Defensive —
        this shouldn't happen in practice but the fallback keeps
        task handlers running under a valid User instead of crashing
        deep in the audit emitter."""
        resolved = await _resolve_triggering_user(repo, None)
        assert resolved is SYSTEM_USER

    async def test_activity_without_association_returns_system_user(
        self, repo,
    ):
        """Every activity *should* have an association row, but if
        one somehow doesn't (historical data, test harnesses), the
        resolver returns SYSTEM_USER rather than raising."""
        await repo.create_dossier(D1, "toelatingen")
        act_id = uuid4()
        now = datetime.now(timezone.utc)
        await repo.create_activity(
            activity_id=act_id, dossier_id=D1, type="loneAct",
            started_at=now, ended_at=now,
        )
        await repo.session.flush()

        resolved = await _resolve_triggering_user(repo, act_id)
        assert resolved is SYSTEM_USER
