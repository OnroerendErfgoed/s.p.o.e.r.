"""
Split-style hooks: status_resolver and task_builders.

Activities can opt in to lifting status and task decisions out of
the handler into dedicated single-responsibility functions:

    activities:
      - name: "tekenBeslissing"
        handler: "compute_beslissing_content"
        status_resolver: "resolve_beslissing_status"
        task_builders:
          - "schedule_trekAanvraag_if_onvolledig"
          - "send_ontvangstbevestiging"

When these hooks are declared, the handler MUST NOT also return the
corresponding field:

* status_resolver declared → handler.status must be None
* task_builders declared  → handler.tasks must be None/empty

Both checks raise ActivityError(500) at execution. "Who decides X"
is always unambiguous: exactly one source per concern per activity.
Legacy activities that don't declare these hooks keep working as
before — the handler owns all three outputs.

Pipeline position: runs after run_handler, before persist_outputs,
so the resolved status and built tasks flow into the same downstream
phases as handler-returned values.
"""

from __future__ import annotations

from ..context import ActivityContext, HandlerResult
from ..errors import ActivityError
from ..state import ActivityState


async def run_split_hooks(state: ActivityState) -> None:
    """Invoke status_resolver and task_builders if declared.

    Reads:  state.activity_def, state.plugin, state.repo,
            state.dossier_id, state.resolved_entities,
            state.handler_result
    Writes: state.handler_result.status, state.handler_result.tasks
    """
    status_resolver_name = state.activity_def.get("status_resolver")
    task_builder_names = state.activity_def.get("task_builders") or []

    # Nothing to do for activities that don't use the split style.
    if not status_resolver_name and not task_builder_names:
        return

    # If an activity uses split hooks it must have a handler_result
    # to populate. Materialize an empty one if the handler didn't run
    # or didn't return a HandlerResult — the activity might compute
    # status and tasks without producing any content (e.g. a pure-
    # notification activity).
    if not isinstance(state.handler_result, HandlerResult):
        state.handler_result = HandlerResult()

    ctx = ActivityContext(
        repo=state.repo,
        dossier_id=state.dossier_id,
        used_entities=state.resolved_entities,
        entity_models=state.plugin.entity_models,
        plugin=state.plugin,
    )

    # --- status_resolver ---
    if status_resolver_name:
        if state.handler_result.status is not None:
            raise ActivityError(
                500,
                f"Activity '{state.activity_def['name']}' declares "
                f"status_resolver '{status_resolver_name}' but its "
                f"handler also returned status="
                f"{state.handler_result.status!r}. Remove one — the "
                f"same activity cannot have status come from both "
                f"sources.",
            )
        resolver = state.plugin.status_resolvers.get(status_resolver_name)
        if resolver is None:
            raise ActivityError(
                500,
                f"Activity '{state.activity_def['name']}' declares "
                f"status_resolver '{status_resolver_name}' but no "
                f"function by that name is registered on the plugin.",
            )
        state.handler_result.status = await resolver(ctx)

    # --- task_builders ---
    if task_builder_names:
        if state.handler_result.tasks:
            raise ActivityError(
                500,
                f"Activity '{state.activity_def['name']}' declares "
                f"task_builders {task_builder_names} but its handler "
                f"also returned {len(state.handler_result.tasks)} "
                f"tasks. Remove one — the same activity cannot have "
                f"tasks come from both sources.",
            )
        collected: list[dict] = []
        for builder_name in task_builder_names:
            builder = state.plugin.task_builders.get(builder_name)
            if builder is None:
                raise ActivityError(
                    500,
                    f"Activity '{state.activity_def['name']}' declares "
                    f"task_builder '{builder_name}' but no function by "
                    f"that name is registered on the plugin.",
                )
            produced = await builder(ctx)
            if produced:
                collected.extend(produced)
        state.handler_result.tasks = collected
