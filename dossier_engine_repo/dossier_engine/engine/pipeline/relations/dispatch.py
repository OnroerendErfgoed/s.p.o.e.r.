"""
Per-kind dispatch + validator firing.

Two concerns bundled here because they share utilities (the state-row
construction boilerplate) and share callers (both are invoked from
``process.py`` at different points in the parse loop).

* ``_handle_domain_add`` / ``_handle_process_control`` — persist the
  relation into ``state.relations_to_persist`` with the right shape
  for each kind. No validation here; permission-gating already ran
  in ``process.py``.

* ``_resolve_validator`` / ``_dispatch_validators`` — find the
  registered callable for a relation type and invoke it with the
  collected relation set. Validators are activity-level opt-in:
  only types listed in the activity's own ``relations:`` block get
  their validator fired.
"""
from __future__ import annotations

from ...errors import ActivityError
from ...refs import EntityRef
from ...state import ActivityState, ValidatedRelation, DomainRelationEntry
from ....plugin import Plugin

from .declarations import (
    _relation_declarations,
    _relation_type_declaration,
    _validate_ref_types,
)


async def _handle_domain_add(
    state: ActivityState,
    rel_item: dict,
    rel_type: str,
    from_ref: str,
) -> None:
    """Validate and stage a domain relation for persistence.

    Validates ``from_types`` / ``to_types`` constraints on the
    *original* refs (before expansion), then expands shorthand refs
    to full IRIs for storage."""
    from ....prov.iris import expand_ref

    to_ref = rel_item.get("to")
    if not from_ref or not to_ref:
        raise ActivityError(
            422,
            f"Domain relation '{rel_type}' requires both 'from' "
            f"and 'to': {rel_item}",
        )

    # Validate ref kinds against declared from_types / to_types.
    decl = _relation_type_declaration(
        state.plugin, state.activity_def, rel_type,
    )
    _validate_ref_types(rel_type, from_ref, to_ref, decl)

    # Expand shorthand → full IRI.
    from_iri = expand_ref(from_ref, state.dossier_id)
    to_iri = expand_ref(to_ref, state.dossier_id)

    state.validated_domain_relations.append(DomainRelationEntry(
        relation_type=rel_type,
        from_ref=from_iri,
        to_ref=to_iri,
    ))
    state.relations_by_type.setdefault(rel_type, []).append({
        "from_ref": from_iri,
        "to_ref": to_iri,
        "raw": rel_item,
    })


async def _handle_process_control(
    state: ActivityState,
    rel_item: dict,
    rel_type: str,
) -> None:
    """Validate and stage a process-control relation for persistence."""
    rel_ref = rel_item.get("entity", "")
    parsed = EntityRef.parse(rel_ref)
    if parsed is None:
        raise ActivityError(
            422,
            f"Invalid entity reference in relation: {rel_ref} "
            f"(process-control relations cannot reference external URIs)",
        )
    rel_entity = await state.repo.get_entity(parsed.version_id)
    if rel_entity is None or rel_entity.dossier_id != state.dossier_id:
        raise ActivityError(
            422, f"Relation entity not found in dossier: {rel_ref}",
        )
    state.relations_by_type.setdefault(rel_type, []).append({
        "ref": rel_ref,
        "entity_row": rel_entity,
        "raw": rel_item,
    })
    state.validated_relations.append(ValidatedRelation(
        version_id=rel_entity.id,
        relation_type=rel_type,
        ref=rel_ref,
    ))



def _resolve_validator(
    plugin: Plugin, activity_def: dict, rel_type: str, operation: str,
):
    """Find the validator callable for a relation type + operation.

    Lookup order:
    1. Activity-level YAML ``validators:`` dict with per-operation
       keys (``add`` and ``remove``). Domain relations only — load-
       time validation (Bug 78) forbids this form on process_control
       relations::

           relations:
             - type: "oe:betreft"
               validators:
                 add: "validate_betreft_target"
                 remove: "validate_betreft_removable"

    2. Activity-level YAML ``validator:`` string (single-validator
       form, fires for all operations). Works for both kinds::

           relations:
             - type: "oe:neemtAkteVan"
               validator: "validate_neemtAkteVan"

    Returns None if no validator is registered.

    **Bug 78 (Round 26) removed Style 3** — the prior plugin-level
    ``relation_validators[rel_type]`` fallback. Activities must now
    declare the validator explicitly via style 1 or 2, or run without
    validation. The load-time
    ``validate_relation_validator_registrations`` rejects plugins
    whose ``relation_validators`` dict uses a declared relation type
    name as a key, to prevent Style 3 from being silently re-created
    by convention.
    """
    decls = _relation_declarations(activity_def)
    decl = decls.get(rel_type, {})

    # Style 1: per-operation validators dict.
    validators_dict = decl.get("validators")
    if isinstance(validators_dict, dict):
        validator_name = validators_dict.get(operation)
        if validator_name:
            fn = plugin.relation_validators.get(validator_name)
            if fn:
                return fn

    # Style 2: single validator string on the declaration.
    validator_name = decl.get("validator")
    if validator_name:
        fn = plugin.relation_validators.get(validator_name)
        if fn:
            return fn

    # No validator declared for this type+operation — return None.
    # The caller (``_dispatch_validators``) treats None as "skip
    # validation," consistent with opt-in semantics.
    return None


async def _dispatch_validators(
    state: ActivityState, allowed: set[str],
) -> None:
    """Invoke registered validators for activity-level opt-in types.

    For domain relations, validators are resolved per-operation:
    add-entries use the ``add`` validator, remove-entries use the
    ``remove`` validator. If no per-operation validator is declared,
    falls back to the type-level validator.

    For process-control relations (which are always adds), the
    type-level validator fires as before.
    """
    activity_level_types = set(
        _relation_declarations(state.activity_def).keys()
    )

    for rel_type in activity_level_types:
        if rel_type not in allowed:
            raise ActivityError(
                500,
                f"Activity {state.activity_def.get('name')!r} opts into "
                f"relation type {rel_type!r} which is not in the workflow's "
                f"allowed relation set {sorted(allowed)}",
                payload={
                    "error": "relation_type_not_permitted",
                    "activity": state.activity_def.get("name"),
                    "relation_type": rel_type,
                },
            )

        # Collect add-entries (from relations_by_type) and
        # remove-entries (from validated_remove_relations).
        # Note: validated_remove_relations holds DomainRelationEntry
        # frozen dataclasses — use attribute access, not dict subscript.
        # Matches the persistence-phase reader at persistence.py:208-213.
        add_entries = state.relations_by_type.get(rel_type, [])
        remove_entries = [
            r for r in state.validated_remove_relations
            if r.relation_type == rel_type
        ]

        # Dispatch add validator. Fires even with empty entries —
        # the validator may enforce "at least one relation required."
        add_validator = _resolve_validator(
            state.plugin, state.activity_def, rel_type, "add",
        )
        if add_validator:
            await add_validator(
                plugin=state.plugin,
                repo=state.repo,
                dossier_id=state.dossier_id,
                activity_def=state.activity_def,
                entries=add_entries,
                used_rows_by_ref=state.used_rows_by_ref,
                generated_items=state.generated,
            )

        # Dispatch remove validator.
        if remove_entries:
            remove_validator = _resolve_validator(
                state.plugin, state.activity_def, rel_type, "remove",
            )
            if remove_validator:
                await remove_validator(
                    plugin=state.plugin,
                    repo=state.repo,
                    dossier_id=state.dossier_id,
                    activity_def=state.activity_def,
                    entries=remove_entries,
                    used_rows_by_ref=state.used_rows_by_ref,
                    generated_items=state.generated,
                )
