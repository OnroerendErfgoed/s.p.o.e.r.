"""
Side-effect helper functions — conditions, auto-resolution, persistence.

* ``_condition_met`` — evaluate the YAML-declared condition gate against
  the current dossier state.
* ``_auto_resolve_used`` — fill in a side-effect's ``used:`` references
  by consulting the triggering activity's scope first, then falling back
  to dossier-wide singleton lookup.
* ``_persist_se_generated`` — write the side-effect handler's generated
  entities with correct schema-version stamping.
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


async def _condition_met(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    trigger_generated: list,
    trigger_used: list,
    condition: dict | None,
    condition_fn_name: str | None = None,
    triggering_user: User,
) -> bool:
    """Check a side effect's conditional gate.

    Two forms, mutually exclusive (enforced at plugin load):

    * Dict form — ``condition: {entity_type, field, value}``. Looks
      up the named entity (in trigger scope, falling back to dossier
      singleton), reads the field via dot-notation, compares to
      ``value``. Returns True iff equal.

    * Function form — ``condition_fn: "name"``. Invokes the named
      predicate registered on ``plugin.side_effect_conditions`` with
      a fresh ``ActivityContext`` scoped to the triggering activity.
      The function returns bool verbatim; no fallback semantics.

    Returns True if neither form is declared (no gate).
    """
    # Function form takes precedence if the validator let both through
    # somehow — but normally the load-time check rules that out.
    if condition_fn_name:
        fn = plugin.side_effect_conditions.get(condition_fn_name)
        if fn is None:
            # Defensive: load-time validation should prevent this.
            # Failing closed (False) is safer than raising inside the
            # pipeline, which would abort the parent activity for a
            # configuration mistake downstream.
            import logging
            logging.getLogger(__name__).error(
                "Side-effect condition_fn %r is not registered on plugin "
                "%r. Skipping side effect.",
                condition_fn_name, plugin.name,
            )
            return False

        # Build a context matching what handlers see. used_entities
        # is the trigger's scope (generated + used), keyed by type —
        # the predicate can look up "oe:beslissing" the same way a
        # handler would. Both trigger_generated and trigger_used are
        # lists of EntityRow at this point.
        resolved = {}
        for row in trigger_generated or []:
            if row.type and row.type not in resolved:
                resolved[row.type] = row
        for row in trigger_used or []:
            if row.type and row.type not in resolved:
                resolved[row.type] = row

        ctx = ActivityContext(
            repo=repo,
            dossier_id=dossier_id,
            used_entities=resolved,
            entity_models=plugin.entity_models,
            plugin=plugin,
            # Side-effect condition: executor is the system, but
            # attribution stays with the user who initiated the
            # pipeline run.
            user=SYSTEM_USER,
            triggering_user=triggering_user,
        )
        result = await fn(ctx)
        return bool(result)

    if not condition:
        return True

    cond_entity_type = condition.get("entity_type")
    cond_field = condition.get("field")
    cond_expected = condition.get("value")

    cond_entity = await resolve_from_prefetched(
        repo, dossier_id, trigger_generated, trigger_used, cond_entity_type,
    )
    if cond_entity is None and plugin.is_singleton(cond_entity_type):
        cond_entity = await lookup_singleton(
            plugin, repo, dossier_id, cond_entity_type,
        )
    if not cond_entity:
        return False
    return _resolve_field(cond_entity.content, cond_field) == cond_expected


async def _auto_resolve_used(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    se_def: dict,
    se_activity_id: UUID,
    trigger_generated: list,
    trigger_used: list,
) -> dict:
    """Auto-resolve a side effect's used entities from the trigger's scope.

    For each `used:` declaration on the side effect activity:
    * Skip externals (side effects don't get external inputs).
    * Skip entries without `auto_resolve: latest` (side effects don't
      take explicit refs from anywhere — the only way to populate used
      is auto-resolve).
    * Look in the trigger's generated entities first, then the
      trigger's used entities, then fall back to dossier-wide singleton
      lookup if the type is singleton-cardinality.
    * If found, write the `used` link row and add to the resolved dict
      that gets passed to the handler.

    Multi-cardinality types only resolve from trigger scope — never
    fall back to "latest of type" from the dossier, because that
    would be ambiguous when several instances exist.

    Returns the dict mapping `entity_type` to the resolved row, ready
    to hand to ActivityContext.
    """
    resolved: dict = {}
    for se_used_def in se_def.get("used", []):
        if se_used_def.get("external"):
            continue
        if se_used_def.get("auto_resolve") != "latest":
            continue

        se_type = se_used_def["type"]
        se_entity = await resolve_from_prefetched(
            repo, dossier_id, trigger_generated, trigger_used, se_type,
        )
        if se_entity is None and plugin.is_singleton(se_type):
            se_entity = await lookup_singleton(
                plugin, repo, dossier_id, se_type,
            )

        if se_entity is not None:
            resolved[se_type] = se_entity
            await repo.create_used(se_activity_id, se_entity.id)

    return resolved


async def _persist_se_generated(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    se_def: dict,
    se_activity_id: UUID,
    handler_generated: list[dict],
) -> None:
    """Persist the entities a side-effect handler returned in its
    `HandlerResult.generated` list.

    For each entry:
    * Default the type from `se_def["generates"][0]` if not set.
    * Resolve identity: explicit entity_id+derived_from override,
      else singleton revise-or-mint, else fresh entity_id.
    * Stamp schema_version using `_resolve_schema_version` against the
      side-effect activity's declarations and the parent row (if any).
    * Persist with `attributed_to="system"`.
    """
    se_generates = se_def.get("generates", [])

    for gen_item in handler_generated:
        identity = await resolve_handler_generated_identity(
            plugin=plugin,
            repo=repo,
            dossier_id=dossier_id,
            gen_item=gen_item,
            allowed_types=se_generates,
        )
        if identity is None:
            continue

        # Resolve schema_version: revisions inherit the parent's sticky
        # version; fresh entities get the side-effect activity's
        # `entities.<type>.new_version` declaration.
        se_parent_row = None
        if identity.derived_from_id is not None:
            se_parent_row = await repo.get_entity(identity.derived_from_id)
        se_schema_version = _resolve_schema_version(
            se_def, identity.gen_type, se_parent_row,
        )

        await repo.create_entity(
            version_id=uuid4(),
            entity_id=identity.entity_id,
            dossier_id=dossier_id,
            type=identity.gen_type,
            generated_by=se_activity_id,
            content=gen_item["content"],
            derived_from=identity.derived_from_id,
            attributed_to="system",
            schema_version=se_schema_version,
        )
