"""
Relation YAML-introspection helpers.

These functions answer "what does the workflow YAML say about this
relation type?" — which activities declare it, what kind it is, what
operations (add/remove) are allowed, which validator to run. Pure
read-only queries over the workflow dict.

Plus ``_validate_ref_types`` — the domain-relation from/to-types gate,
run at activity-relation-processing time. Placed here because it's a
declaration-driven check (reads from/to_types from the workflow-level
declaration) even though it's called from process.py.
"""
from __future__ import annotations

from ...errors import ActivityError
from ...refs import EntityRef
from ....plugin import Plugin


def _relation_declarations(activity_def: dict) -> dict[str, dict]:
    """Parse the activity's ``relations:`` block into a dict of
    type → declaration (with kind, operations, etc.)."""
    decls = {}
    for entry in activity_def.get("relations", []) or []:
        if isinstance(entry, dict):
            t = entry.get("type")
            if t:
                decls[t] = entry
        elif isinstance(entry, str):
            decls[entry] = {"type": entry, "kind": "process_control"}
    return decls


def allowed_relation_types_for_activity(
    plugin: Plugin, activity_def: dict,
) -> set[str]:
    """Return the set of relation types this activity may carry on its
    request body (the permission gate)."""
    workflow = set()
    for e in plugin.workflow.get("relations", []):
        if isinstance(e, dict) and e.get("type"):
            workflow.add(e["type"])
        elif isinstance(e, str):
            workflow.add(e)
    activity = set(_relation_declarations(activity_def).keys())
    return workflow | activity


def _allowed_operations(activity_def: dict, rel_type: str) -> set[str]:
    """Return the set of operations (add, remove) this activity permits
    for the given relation type. Defaults to {"add"}."""
    decls = _relation_declarations(activity_def)
    decl = decls.get(rel_type, {})
    ops = decl.get("operations")
    if ops:
        return set(ops)
    return {"add"}


def _relation_kind(
    plugin: Plugin, activity_def: dict, rel_type: str,
) -> str:
    """Resolve a relation type's kind (``"domain"`` or
    ``"process_control"``) from the workflow-level declaration.

    Post-Bug-78 (Round 26): activity-level ``kind:`` is forbidden at
    load time (the load-time validator fails the plugin registration
    if any activity-level relation declaration includes it). Only
    workflow-level declarations carry ``kind:``, and the load-time
    validator guarantees every declared type has a valid kind.

    Therefore this function consults the workflow-level ``relations:``
    block only. Returns the kind string; raises ``KeyError`` if the
    type isn't declared (shouldn't happen — the permission gate in
    ``_parse_relations`` catches undeclared types before this runs,
    and the load-time validator catches them at plugin load). The
    raise is a defensive assertion, not an expected path.

    Before Bug 78 this function existed but was never called —
    dispatch guessed kind from request item shape, making the
    ``kind:`` field effectively decorative. It is now the
    authoritative dispatch key in ``_parse_relations``.
    """
    for e in plugin.workflow.get("relations", []) or []:
        if isinstance(e, dict) and e.get("type") == rel_type:
            kind = e.get("kind")
            if kind not in ("domain", "process_control"):
                # Load-time validator should have caught this — if we
                # get here, either validation was bypassed or the
                # workflow was mutated post-load. Raise loudly rather
                # than silently defaulting.
                raise ValueError(
                    f"Workflow-level declaration for relation "
                    f"{rel_type!r} has invalid kind={kind!r}. "
                    f"Load-time validation should have caught this; "
                    f"this indicates a validator bypass or a "
                    f"post-load mutation."
                )
            return kind
    raise KeyError(
        f"Relation type {rel_type!r} not declared at workflow level. "
        f"The permission gate in _parse_relations should have "
        f"rejected this before dispatch; reaching _relation_kind "
        f"means validation was bypassed."
    )


def _relation_type_declaration(
    plugin: Plugin, activity_def: dict, rel_type: str,
) -> dict:
    """Look up the full declaration dict for a relation type.

    Checks the activity-level ``relations:`` block first, then the
    workflow-level ``relations:`` block. Returns an empty dict if
    the type isn't declared anywhere (shouldn't happen — the
    permission gate catches undeclared types before this runs).
    """
    # Activity level
    decls = _relation_declarations(activity_def)
    if rel_type in decls:
        return decls[rel_type]
    # Workflow level
    for e in plugin.workflow.get("relations", []):
        if isinstance(e, dict) and e.get("type") == rel_type:
            return e
    return {}


def _validate_ref_types(
    rel_type: str,
    from_ref: str,
    to_ref: str,
    declaration: dict,
) -> None:
    """Validate that ``from_ref`` and ``to_ref`` match the declared
    ``from_types`` and ``to_types`` on a domain relation type.

    Uses ``classify_ref`` on the *original* (pre-expansion) ref
    so that both shorthand and expanded forms work.

    Skips validation if ``from_types`` / ``to_types`` are not
    declared — the constraint is opt-in per relation type.

    Raises ``ActivityError(422)`` on mismatch.
    """
    from ....prov.iris import classify_ref

    from_types = declaration.get("from_types")
    if from_types:
        actual = classify_ref(from_ref)
        if actual not in from_types:
            raise ActivityError(
                422,
                f"Relation '{rel_type}': 'from' ref must be one of "
                f"{from_types}, got '{actual}' "
                f"(ref: {from_ref}).",
            )

    to_types = declaration.get("to_types")
    if to_types:
        actual = classify_ref(to_ref)
        if actual not in to_types:
            raise ActivityError(
                422,
                f"Relation '{rel_type}': 'to' ref must be one of "
                f"{to_types}, got '{actual}' "
                f"(ref: {to_ref}).",
            )


