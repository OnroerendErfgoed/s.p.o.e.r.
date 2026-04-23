"""
Relation processing — the pipeline-phase entry point.

``process_relations`` is the phase called from the engine pipeline.
It drives two parsing passes (``_parse_relations`` for add-direction,
``_parse_remove_relations`` for domain removes) and then fires
validators via ``_dispatch_validators``.

The parsers do YAML permission-gating and request-shape validation;
per-kind persistence is delegated to the handlers in ``dispatch.py``.
"""
from __future__ import annotations

from ...errors import ActivityError
from ...refs import EntityRef
from ...state import ActivityState, ValidatedRelation, DomainRelationEntry
from ....plugin import Plugin

from .declarations import (
    _relation_declarations,
    allowed_relation_types_for_activity,
    _allowed_operations,
    _relation_kind,
    _relation_type_declaration,
    _validate_ref_types,
)
from .dispatch import (
    _handle_domain_add,
    _handle_process_control,
    _dispatch_validators,
)


async def process_relations(state: ActivityState) -> None:
    """Parse ``relations`` and ``remove_relations``, then dispatch
    validators.

    Reads:  state.relation_items, state.remove_relation_items,
            state.activity_def, state.plugin, state.repo,
            state.dossier_id, state.used_rows_by_ref, state.generated
    Writes: state.validated_relations (process-control),
            state.validated_domain_relations (domain adds),
            state.validated_remove_relations (domain removes),
            state.relations_by_type
    """
    allowed = allowed_relation_types_for_activity(
        state.plugin, state.activity_def,
    )
    await _parse_relations(state, allowed)
    await _parse_remove_relations(state, allowed)
    await _dispatch_validators(state, allowed)



async def _parse_relations(
    state: ActivityState, allowed: set[str],
) -> None:
    """Walk ``relations``, validate, resolve, and route to either
    process-control or domain state lists.

    **Bug 78 (Round 26): dispatch is driven by the workflow-level
    ``kind:`` declaration**, not by request-item shape. The request
    item's shape (``entity`` vs ``from+to``) is validated against the
    declared kind; mismatch is a 422 with an informative message
    naming the type and its declared kind. Prior behaviour guessed
    kind from shape, which meant a plugin author could declare
    ``kind: domain`` and a client could silently get process-control
    dispatch by sending the wrong shape — the ``kind:`` field was
    effectively decorative."""
    for rel_item in state.relation_items:
        rel_type = rel_item.get("type")
        if not rel_type:
            raise ActivityError(
                422, f"Relation item missing 'type': {rel_item}",
            )
        if rel_type not in allowed:
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"relation type '{rel_type}'. Allowed: {sorted(allowed)}",
            )
        if "add" not in _allowed_operations(state.activity_def, rel_type):
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"adding relations of type '{rel_type}'.",
            )

        # Bug 78: resolve kind from the workflow-level declaration
        # (single source of truth; load-time validator enforces it's
        # always present and ∈ {domain, process_control}). Then check
        # the request item's shape against the declared kind.
        kind = _relation_kind(state.plugin, state.activity_def, rel_type)
        from_ref = rel_item.get("from") or rel_item.get("from_ref")
        has_entity = rel_item.get("entity") is not None
        has_domain_shape = from_ref is not None

        if kind == "domain":
            if has_entity:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: domain` (entity→entity semantic). The "
                    f"request sent an `entity:` field (process-control "
                    f"shape). Use `from:` + `to:` for domain relations."
                )
            if not has_domain_shape:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: domain`. The request item requires "
                    f"`from:` + `to:` fields: {rel_item!r}"
                )
            await _handle_domain_add(state, rel_item, rel_type, from_ref)
        else:  # process_control
            if has_domain_shape:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: process_control` (activity→entity "
                    f"semantic). The request sent `from:`/`to:` fields "
                    f"(domain shape). Use `entity:` for process-"
                    f"control relations."
                )
            if not has_entity:
                raise ActivityError(
                    422,
                    f"Relation type {rel_type!r} is declared as "
                    f"`kind: process_control`. The request item "
                    f"requires an `entity:` field: {rel_item!r}"
                )
            await _handle_process_control(state, rel_item, rel_type)



async def _parse_remove_relations(
    state: ActivityState, allowed: set[str],
) -> None:
    """Walk ``remove_relations``, validate type + operation permission.

    Refs are expanded to full IRIs so the supersede query matches
    against the stored (expanded) values in domain_relations."""
    from ....prov.iris import expand_ref

    for item in state.remove_relation_items:
        rel_type = item.get("type")
        from_ref = item.get("from") or item.get("from_ref")
        to_ref = item.get("to")

        if not rel_type:
            raise ActivityError(
                422, f"remove_relations item missing 'type': {item}",
            )
        if not from_ref or not to_ref:
            raise ActivityError(
                422,
                f"remove_relations item requires 'from' and 'to': {item}",
            )
        if rel_type not in allowed:
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"relation type '{rel_type}'. Allowed: {sorted(allowed)}",
            )
        if "remove" not in _allowed_operations(state.activity_def, rel_type):
            raise ActivityError(
                422,
                f"Activity '{state.activity_def['name']}' does not allow "
                f"removing relations of type '{rel_type}'. "
                f"Allowed operations: "
                f"{sorted(_allowed_operations(state.activity_def, rel_type))}",
            )

        # Bug 78 defense-in-depth: remove operations are legal only on
        # domain relations. Load-time validation forbids
        # ``operations: [remove]`` on process_control activity
        # declarations, so the permission gate above already catches
        # the problem — but pinning the kind check here means a
        # future regression (e.g. someone loosens the load-time
        # validator) still fails loud rather than dispatching a
        # remove against a process_control relation.
        kind = _relation_kind(state.plugin, state.activity_def, rel_type)
        if kind != "domain":
            raise ActivityError(
                422,
                f"Relation type {rel_type!r} is declared as "
                f"`kind: {kind}`. Remove operations are only legal "
                f"on `kind: domain` relations (process_control "
                f"relations are stateless annotations with no remove "
                f"semantic)."
            )

        # Validate ref kinds against declared from_types / to_types.
        decl = _relation_type_declaration(
            state.plugin, state.activity_def, rel_type,
        )
        _validate_ref_types(rel_type, from_ref, to_ref, decl)

        # Expand shorthand → full IRI (must match what was stored).
        from_iri = expand_ref(from_ref, state.dossier_id)
        to_iri = expand_ref(to_ref, state.dossier_id)

        state.validated_remove_relations.append(DomainRelationEntry(
            relation_type=rel_type,
            from_ref=from_iri,
            to_ref=to_iri,
        ))


