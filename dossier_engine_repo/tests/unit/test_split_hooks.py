"""
Unit tests for the split-style hooks phase (`run_split_hooks`).

The phase is opt-in — an activity that declares `status_resolver`
or `task_builders` in YAML routes those concerns through named
plugin functions instead of the handler. The engine enforces
"exactly one source per concern": if the handler ALSO returned
the same field, raise.

Tests here use a stub Plugin with populated resolver/builder
registries. No DB, no HTTP — the phase is pure async function
dispatch driven by ActivityState.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from dossier_engine.engine.context import HandlerResult
from dossier_engine.engine.errors import ActivityError
from dossier_engine.engine.pipeline.split_hooks import run_split_hooks


D1 = UUID("11111111-1111-1111-1111-111111111111")


class _StubPlugin:
    """Minimal plugin stub with resolver/builder registries and the
    few attributes run_split_hooks reads via ActivityContext."""

    def __init__(self, status_resolvers=None, task_builders=None):
        self.status_resolvers = status_resolvers or {}
        self.task_builders = task_builders or {}
        self.entity_models = {}
        self.constants = None

    def is_singleton(self, t):
        return False

    def resolve_schema(self, t, v):
        return None


class _StubState:
    """A lightweight ActivityState substitute — only the fields the
    phase reads are populated. Using a namespace-ish object keeps the
    tests focused on behaviour instead of state construction."""

    def __init__(
        self, activity_def, plugin,
        handler_result=None, resolved_entities=None,
    ):
        self.activity_def = activity_def
        self.plugin = plugin
        self.handler_result = handler_result
        self.resolved_entities = resolved_entities or {}
        self.repo = None
        self.dossier_id = D1
        # user / triggering_user — split-hook phase builds an
        # ActivityContext from state.user (see the two-field attribution
        # model on ``ActivityContext`` docstring). For these unit tests
        # the ctx isn't used for audit emission so None is fine.
        self.user = None


class TestNoOpForLegacyActivities:
    """Activities without split-hook YAML declarations are untouched."""

    async def test_no_yaml_fields_is_noop(self):
        """A handler-style activity — no status_resolver, no
        task_builders. Phase runs, changes nothing."""
        before = HandlerResult(status="some_status", tasks=[{"kind": "recorded"}])
        state = _StubState(
            activity_def={"name": "legacy"},
            plugin=_StubPlugin(),
            handler_result=before,
        )
        await run_split_hooks(state)
        assert state.handler_result.status == "some_status"
        assert state.handler_result.tasks == [{"kind": "recorded"}]

    async def test_handler_result_none_stays_none(self):
        """If the handler didn't run at all and no split hooks are
        declared, handler_result remains None."""
        state = _StubState(
            activity_def={"name": "legacy"},
            plugin=_StubPlugin(),
            handler_result=None,
        )
        await run_split_hooks(state)
        assert state.handler_result is None


class TestStatusResolver:
    """status_resolver runs when declared and fills handler_result.status."""

    async def test_resolver_runs_and_sets_status(self):
        async def my_resolver(ctx):
            return "resolved_status"

        state = _StubState(
            activity_def={
                "name": "test",
                "status_resolver": "my_resolver",
            },
            plugin=_StubPlugin(status_resolvers={"my_resolver": my_resolver}),
            handler_result=HandlerResult(),
        )
        await run_split_hooks(state)
        assert state.handler_result.status == "resolved_status"

    async def test_resolver_may_return_none(self):
        """Returning None means 'don't change the status' — same
        semantics as a handler returning HandlerResult(status=None)."""
        async def my_resolver(ctx):
            return None

        state = _StubState(
            activity_def={
                "name": "test",
                "status_resolver": "my_resolver",
            },
            plugin=_StubPlugin(status_resolvers={"my_resolver": my_resolver}),
            handler_result=HandlerResult(),
        )
        await run_split_hooks(state)
        assert state.handler_result.status is None

    async def test_materializes_empty_handler_result(self):
        """When the handler didn't run but a resolver is declared,
        the phase creates an empty HandlerResult so downstream phases
        see the resolver's output."""
        async def my_resolver(ctx):
            return "resolved"

        state = _StubState(
            activity_def={
                "name": "test",
                "status_resolver": "my_resolver",
            },
            plugin=_StubPlugin(status_resolvers={"my_resolver": my_resolver}),
            handler_result=None,  # handler didn't run
        )
        await run_split_hooks(state)
        assert state.handler_result is not None
        assert state.handler_result.status == "resolved"


class TestTaskBuilders:
    """task_builders concatenate their returned lists."""

    async def test_single_builder(self):
        async def build(ctx):
            return [{"kind": "recorded", "function": "f1"}]

        state = _StubState(
            activity_def={
                "name": "test",
                "task_builders": ["build"],
            },
            plugin=_StubPlugin(task_builders={"build": build}),
            handler_result=HandlerResult(),
        )
        await run_split_hooks(state)
        assert state.handler_result.tasks == [
            {"kind": "recorded", "function": "f1"}
        ]

    async def test_multiple_builders_concatenate(self):
        async def build_a(ctx):
            return [{"kind": "recorded", "function": "a"}]

        async def build_b(ctx):
            return [
                {"kind": "recorded", "function": "b1"},
                {"kind": "recorded", "function": "b2"},
            ]

        state = _StubState(
            activity_def={
                "name": "test",
                "task_builders": ["build_a", "build_b"],
            },
            plugin=_StubPlugin(task_builders={
                "build_a": build_a,
                "build_b": build_b,
            }),
            handler_result=HandlerResult(),
        )
        await run_split_hooks(state)
        assert [t["function"] for t in state.handler_result.tasks] == [
            "a", "b1", "b2",
        ]

    async def test_builder_may_return_empty_list(self):
        """A builder that decides 'nothing to schedule this time' is
        a valid result — it represents the conditional path."""
        async def build(ctx):
            return []

        state = _StubState(
            activity_def={
                "name": "test",
                "task_builders": ["build"],
            },
            plugin=_StubPlugin(task_builders={"build": build}),
            handler_result=HandlerResult(),
        )
        await run_split_hooks(state)
        assert state.handler_result.tasks == []


class TestConflictDetection:
    """Declaring a split hook forbids the handler from returning the
    same field. Raising ActivityError(500) at activity execution
    makes bugs loud instead of silently choosing a winner."""

    async def test_resolver_plus_handler_status_raises(self):
        async def my_resolver(ctx):
            return "resolved"

        state = _StubState(
            activity_def={
                "name": "test",
                "status_resolver": "my_resolver",
            },
            plugin=_StubPlugin(status_resolvers={"my_resolver": my_resolver}),
            handler_result=HandlerResult(status="handler_said_this"),
        )
        with pytest.raises(ActivityError) as exc_info:
            await run_split_hooks(state)
        assert exc_info.value.status_code == 500
        assert "status_resolver" in str(exc_info.value.detail)
        assert "handler also returned" in str(exc_info.value.detail)

    async def test_builders_plus_handler_tasks_raises(self):
        async def build(ctx):
            return [{"kind": "recorded"}]

        state = _StubState(
            activity_def={
                "name": "test",
                "task_builders": ["build"],
            },
            plugin=_StubPlugin(task_builders={"build": build}),
            handler_result=HandlerResult(
                tasks=[{"kind": "recorded", "function": "handler_task"}],
            ),
        )
        with pytest.raises(ActivityError) as exc_info:
            await run_split_hooks(state)
        assert exc_info.value.status_code == 500
        assert "task_builders" in str(exc_info.value.detail)

    async def test_unknown_resolver_name_raises(self):
        """YAML referencing an unregistered resolver is a config bug
        that should fail loudly, not silently skip."""
        state = _StubState(
            activity_def={
                "name": "test",
                "status_resolver": "does_not_exist",
            },
            plugin=_StubPlugin(),
            handler_result=HandlerResult(),
        )
        with pytest.raises(ActivityError) as exc_info:
            await run_split_hooks(state)
        assert exc_info.value.status_code == 500
        assert "does_not_exist" in str(exc_info.value.detail)

    async def test_unknown_builder_name_raises(self):
        state = _StubState(
            activity_def={
                "name": "test",
                "task_builders": ["missing_builder"],
            },
            plugin=_StubPlugin(),
            handler_result=HandlerResult(),
        )
        with pytest.raises(ActivityError) as exc_info:
            await run_split_hooks(state)
        assert exc_info.value.status_code == 500
        assert "missing_builder" in str(exc_info.value.detail)


class TestCombinedSplit:
    """An activity can declare both a resolver and builders — both
    run, both populate handler_result."""

    async def test_resolver_and_builders_together(self):
        async def resolver(ctx):
            return "goedgekeurd"

        async def builder(ctx):
            return [{"kind": "recorded", "function": "notify"}]

        state = _StubState(
            activity_def={
                "name": "test",
                "status_resolver": "resolver",
                "task_builders": ["builder"],
            },
            plugin=_StubPlugin(
                status_resolvers={"resolver": resolver},
                task_builders={"builder": builder},
            ),
            handler_result=HandlerResult(),
        )
        await run_split_hooks(state)
        assert state.handler_result.status == "goedgekeurd"
        assert state.handler_result.tasks == [
            {"kind": "recorded", "function": "notify"}
        ]
