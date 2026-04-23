"""
Side-effect orchestration — the execute loop and per-effect dispatch.

``execute_side_effects`` is the recursive entry point. It's called once
per top-level activity with the activity's ``side_effects`` list and
depth=0, and re-enters itself for nested side effects.

``_execute_one_side_effect`` handles a single entry: evaluates the
condition gate, calls the side-effect handler, and persists any
generated entities via the helpers in ``helpers.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from ...context import ActivityContext, HandlerResult
from ...lookups import lookup_singleton, resolve_from_prefetched
from ....auth import User, SYSTEM_USER
from ....db.models import Repository
from ....plugin import Plugin
from ...pipeline._helpers.identity import resolve_handler_generated_identity
from ..authorization import _resolve_field
from ..generated import _resolve_schema_version

from .helpers import _condition_met, _auto_resolve_used, _persist_se_generated


async def execute_side_effects(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    trigger_activity_id: UUID,
    side_effects: list[dict],
    triggering_user: User,
    depth: int = 0,
    max_depth: int = 10,
) -> None:
    """Recursively execute side effect activities.

    ``triggering_user`` is the agent attributed with the *original*
    user-facing activity that started this pipeline run. It's passed
    through unchanged during recursion — nested side effects still
    attribute their work to the original request-maker, even though
    the nested chain is entirely system-generated. See
    ``ActivityContext`` for the two-field attribution model and why
    this matters for task/side-effect audit emits.

    For each side effect entry:
    1. Check its condition (if any) — skip if condition not met.
    2. Look up the activity definition + handler.
    3. Create the side effect activity row + system association.
    4. Auto-resolve its used entities from the trigger's scope,
       falling back to singleton lookup.
    5. Run the handler.
    6. Persist any handler-generated entities, with schema_version
       resolved per the side-effect activity's declarations.
    7. Recursively execute the side effect's own side effects.

    The trigger's generated + used entity lists are prefetched once
    and reused for every side effect in this call, avoiding 2N redundant
    queries when the chain auto-resolves N entities of trigger types.

    Errors are not swallowed — a side effect raising an `ActivityError`
    will propagate up and abort the entire chain. Side effects are part
    of the activity's transaction, so failure rolls back the whole
    activity.
    """
    if depth >= max_depth:
        return  # safety limit
    if not side_effects:
        return  # nothing to do — skip the agent ensure and prefetch

    await repo.ensure_agent("system", "systeem", "Systeem", {})

    # Prefetch the trigger activity's generated + used entities ONCE for
    # the whole side-effects pass. Every side effect inside this call
    # uses the same trigger, so without this we'd redundantly query
    # these for each auto-resolved used entry. Two queries here instead
    # of 2N queries.
    trigger_generated = await repo.get_entities_generated_by_activity(trigger_activity_id)
    trigger_used = await repo.get_used_entities_for_activity(trigger_activity_id)

    for side_effect in side_effects:
        await _execute_one_side_effect(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            trigger_activity_id=trigger_activity_id,
            trigger_generated=trigger_generated,
            trigger_used=trigger_used,
            side_effect=side_effect,
            depth=depth,
            max_depth=max_depth,
            triggering_user=triggering_user,
        )


async def _execute_one_side_effect(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    trigger_activity_id: UUID,
    trigger_generated: list,
    trigger_used: list,
    side_effect: dict,
    depth: int,
    max_depth: int,
    triggering_user: User,
) -> None:
    """Execute a single side effect entry. See `execute_side_effects`
    for the high-level contract."""
    se_activity_name = side_effect.get("activity")
    if not se_activity_name:
        return

    # Skip if the conditional gate fails. Two forms:
    #   * `condition: {entity_type, field, value}` — dict form,
    #     equality on an entity's content field.
    #   * `condition_fn: "name"` — references a named predicate
    #     registered on the plugin; receives ActivityContext, returns
    #     bool. Mutually exclusive with `condition` (enforced at
    #     plugin load).
    if not await _condition_met(
        plugin=plugin,
        repo=repo,
        dossier_id=dossier_id,
        trigger_generated=trigger_generated,
        trigger_used=trigger_used,
        condition=side_effect.get("condition"),
        condition_fn_name=side_effect.get("condition_fn"),
        triggering_user=triggering_user,
    ):
        return

    se_def = plugin.find_activity_def(se_activity_name)
    if se_def is None:
        return

    # Side effects must compute their output via a handler — they have
    # no client `generated` block to fall back on.
    se_handler_name = se_def.get("handler")
    if not se_handler_name:
        return
    se_handler_fn = plugin.handlers.get(se_handler_name)
    if se_handler_fn is None:
        return

    # Create the activity row + system association.
    se_activity_id = uuid4()
    se_now = datetime.now(timezone.utc)

    se_activity_row = await repo.create_activity(
        activity_id=se_activity_id,
        dossier_id=dossier_id,
        type=se_activity_name,
        started_at=se_now,
        ended_at=se_now,
        informed_by=str(trigger_activity_id),
    )
    await repo.create_association(
        association_id=uuid4(),
        activity_id=se_activity_id,
        agent_id="system",
        agent_name="Systeem",
        agent_type="systeem",
        role="systeem",
    )

    # Auto-resolve used entities from the trigger's scope.
    se_resolved = await _auto_resolve_used(
        plugin=plugin,
        repo=repo,
        dossier_id=dossier_id,
        se_def=se_def,
        se_activity_id=se_activity_id,
        trigger_generated=trigger_generated,
        trigger_used=trigger_used,
    )

    # Run the handler.
    se_ctx = ActivityContext(
        repo=repo,
        dossier_id=dossier_id,
        used_entities=se_resolved,
        entity_models=plugin.entity_models,
        plugin=plugin,
        # Side-effect handler: executor is the system, attribution is
        # the user who initiated the pipeline run (whose activity's
        # declared side_effects we're walking right now).
        user=SYSTEM_USER,
        triggering_user=triggering_user,
    )
    se_result = await se_handler_fn(se_ctx, None)

    # Stamp computed status, if the handler returned one.
    if isinstance(se_result, HandlerResult) and se_result.status:
        se_activity_row.computed_status = se_result.status

    # Persist any handler-generated entities.
    if isinstance(se_result, HandlerResult) and se_result.generated:
        await _persist_se_generated(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            se_def=se_def,
            se_activity_id=se_activity_id,
            handler_generated=se_result.generated,
        )

    # Recurse into nested side effects, if any.
    nested = se_def.get("side_effects", [])
    if nested:
        # Flush so nested side effects can see entities we just created.
        await repo.session.flush()
        await execute_side_effects(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            trigger_activity_id=se_activity_id,
            side_effects=nested,
            depth=depth + 1,
            max_depth=max_depth,
            # Pass-through: nested side effects are still attributed
            # to the user whose original request started this chain,
            # not to the immediate triggering side-effect activity
            # (which ran as the system).
            triggering_user=triggering_user,
        )


