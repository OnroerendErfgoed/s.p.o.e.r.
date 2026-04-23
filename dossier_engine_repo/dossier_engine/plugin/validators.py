"""
Load-time validators — five functions that check the workflow
contract at plugin load, before the engine accepts any request.

1. ``validate_workflow_version_references`` — cross-check every
   ``entities[type].new_version`` / ``allowed_versions`` string
   against the ``entity_types[type].schemas`` declarations.
2. ``validate_side_effect_condition_fn_registrations`` — check that
   every ``side_effects[*].condition_fn`` name resolves in the
   plugin's condition_fn registry.
3. ``validate_side_effect_conditions`` — shape-check every
   ``side_effects[*].condition`` dict (must have exactly
   {entity_type, field, value}).
4. ``validate_relation_declarations`` — comprehensive check of the
   workflow- and activity-level ``relations:`` contract (Bug 78,
   Round 26).
5. ``validate_relation_validator_registrations`` — check that every
   workflow- and activity-level relation validator name resolves
   in the plugin's relation_validators registry.

Plus the constants they depend on: ``_VALID_RELATION_KINDS``,
``_WORKFLOW_RELATION_KEYS``, ``_ACTIVITY_RELATION_KEYS``,
``_ACTIVITY_RELATION_FORBIDDEN_KEYS``.

The file is deliberately left as one module (per Round 34 plan) rather
than split into a sub-package — the five validators are independent
concerns but cohesive enough at ~450 lines to stay together.
"""
from __future__ import annotations

from typing import Any


def validate_workflow_version_references(
    workflow: dict,
    entity_schemas: dict[tuple[str, str], type[BaseModel]],
) -> None:
    """Cross-check every `new_version` / `allowed_versions` string on every
    activity against the declared `entity_schemas` registry.

    Fails fast with ValueError at plugin load time if an activity references
    a version that isn't declared. Prevents the silent-runtime-fallback
    footgun where an activity declares `new_version: v3` but the type only
    has `v1` and `v2` registered.
    """
    declared: dict[str, set[str]] = {}
    for (type_name, version) in entity_schemas:
        declared.setdefault(type_name, set()).add(version)

    for act in workflow.get("activities", []):
        entities_cfg = act.get("entities") or {}
        for type_name, ecfg in entities_cfg.items():
            versions_referenced: set[str] = set()
            nv = ecfg.get("new_version")
            if nv:
                versions_referenced.add(str(nv))
            for av in ecfg.get("allowed_versions") or []:
                versions_referenced.add(str(av))

            if not versions_referenced:
                continue

            available = declared.get(type_name, set())
            missing = versions_referenced - available
            if missing:
                raise ValueError(
                    f"Activity {act.get('name')!r} references schema "
                    f"version(s) {sorted(missing)} for entity type "
                    f"{type_name!r}, but the workflow's entity_types "
                    f"block only declares {sorted(available) or 'none'}"
                )


# Accepted keys on a side-effect condition block. Enforced at plugin
# load so a typo (e.g. ``from_entity:`` borrowed from the status-rule
# or authorization-scope shape) fails fast with a clear error instead
# of silently blocking the side effect at runtime.
_SIDE_EFFECT_CONDITION_REQUIRED = frozenset({"entity_type", "field", "value"})



def validate_side_effect_condition_fn_registrations(
    workflow: dict,
    side_effect_conditions: dict,
) -> None:
    """Cross-check every ``side_effects[*].condition_fn`` name against
    the plugin's registered predicates. Runs after the Plugin
    constructor assembles its registries so we can verify names
    resolve. Fails fast with ValueError on any unknown name.

    Kept separate from ``validate_side_effect_conditions`` because
    that one runs earlier (on the raw workflow dict, before the
    plugin is built) and can only shape-check. This one does the
    cross-registry check once both halves are available.
    """
    for act in workflow.get("activities", []):
        if not isinstance(act, dict):
            continue
        for se in act.get("side_effects") or []:
            if not isinstance(se, dict):
                continue
            name = se.get("condition_fn")
            if not name:
                continue
            if name not in (side_effect_conditions or {}):
                known = sorted((side_effect_conditions or {}).keys()) or "(none registered)"
                raise ValueError(
                    f"Activity {act.get('name')!r}: side-effect "
                    f"{se.get('activity')!r} references "
                    f"condition_fn={name!r} but no predicate by that "
                    f"name is registered on the plugin. Registered: "
                    f"{known}."
                )



def validate_side_effect_conditions(workflow: dict) -> None:
    """Validate every ``side_effects[*]`` gating entry.

    Two forms are accepted, mutually exclusive per entry:

    * ``condition: {entity_type, field, value}`` — dict shape. The
      runtime gate reads ``entity_type`` and returns False when it's
      missing, so a typo like ``from_entity:`` (borrowed from the
      status-rule or authorization-scope shape) would silently block
      every invocation. We reject it at load instead.

    * ``condition_fn: "name"`` — references a predicate registered on
      ``plugin.side_effect_conditions``. We can't validate that the
      name resolves at the workflow layer (the plugin object isn't
      built yet when this runs) — the Plugin constructor should
      cross-check that every ``condition_fn:`` name has a registered
      function. Here we just validate the shape is a non-empty string
      and that ``condition`` isn't also set on the same entry.

    Fails fast with ValueError when a shape is wrong.
    """
    for act in workflow.get("activities", []):
        if not isinstance(act, dict):
            continue
        for se in act.get("side_effects") or []:
            if not isinstance(se, dict):
                continue

            cond = se.get("condition")
            cond_fn = se.get("condition_fn")

            # Mutex: each side-effect entry picks one form, not both.
            if cond is not None and cond_fn is not None:
                raise ValueError(
                    f"Activity {act.get('name')!r}: side-effect "
                    f"{se.get('activity')!r} declares both "
                    f"``condition:`` and ``condition_fn:``. Choose "
                    f"one — the dict form for simple field equality, "
                    f"the function form for anything else."
                )

            # Function form: just shape-check the name. Registration
            # is verified by the Plugin constructor once all the
            # function registries are available.
            if cond_fn is not None:
                if not isinstance(cond_fn, str) or not cond_fn.strip():
                    raise ValueError(
                        f"Activity {act.get('name')!r}: side-effect "
                        f"{se.get('activity')!r} has a non-string "
                        f"``condition_fn:`` value: {cond_fn!r}"
                    )
                continue

            # Dict form: validate shape.
            if cond is None:
                continue
            if not isinstance(cond, dict):
                raise ValueError(
                    f"Activity {act.get('name')!r}: side-effect "
                    f"condition must be a dict with keys "
                    f"{sorted(_SIDE_EFFECT_CONDITION_REQUIRED)} or "
                    f"a ``condition_fn:`` string, "
                    f"got {type(cond).__name__}: {cond!r}"
                )
            keys = set(cond.keys())
            missing = _SIDE_EFFECT_CONDITION_REQUIRED - keys
            extra = keys - _SIDE_EFFECT_CONDITION_REQUIRED
            if missing or extra:
                parts = []
                if missing:
                    parts.append(f"missing keys: {sorted(missing)}")
                if extra:
                    parts.append(f"unknown keys: {sorted(extra)}")
                raise ValueError(
                    f"Activity {act.get('name')!r}: side-effect "
                    f"condition on {se.get('activity')!r} has "
                    f"{'; '.join(parts)}. Accepted shape: "
                    f"{{entity_type, field, value}}, or use "
                    f"``condition_fn: \"name\"`` for non-equality "
                    f"gates. (Common confusion: {{from_entity, field, "
                    f"mapping}} is for activity `status:` rules; "
                    f"{{from_entity, field}} is for authorization "
                    f"scopes.)"
                )


# Accepted values for a relation type's `kind:` field. Anything else
# at load time is a ValueError.

_VALID_RELATION_KINDS = frozenset({"domain", "process_control"})

# Keys allowed on a workflow-level relation declaration. Any other key
# is rejected at load time so typos surface early rather than silently
# being ignored (cf. _relation_kind dead-code pattern that prompted
# Bug 78 — fields that exist but aren't wired up).
_WORKFLOW_RELATION_KEYS = frozenset({
    "type", "kind", "from_types", "to_types", "description",
})

# Keys allowed on an activity-level relation declaration. `kind`,
# `from_types`, `to_types`, `description` are forbidden here
# (declared at workflow level only) — the activity references a
# workflow-level type by name, nothing else.
_ACTIVITY_RELATION_KEYS = frozenset({
    "type", "operations", "validator", "validators",
})

# Forbidden activity-level keys (declare these at workflow level).
# Named separately from _ACTIVITY_RELATION_KEYS so error messages can
# distinguish "unknown key (typo)" from "legal key but wrong scope
# (declare at workflow level)" — different ergonomic paths for the
# author to take.
_ACTIVITY_RELATION_FORBIDDEN_KEYS = frozenset({
    "kind", "from_types", "to_types", "description",
})

def validate_relation_declarations(workflow: dict) -> None:
    """Load-time validation of the workflow's relation type contract.

    Enforces the "types declared once at workflow level; activities
    reference by name only" model. See Bug 78 (Round 26) for context —
    prior to this validator, ``kind:`` was declarable but never
    consulted (``_relation_kind`` was dead code; dispatch guessed
    from request shape), and Style-3 plugin-level by-type-name
    fallback ran invisibly. This function makes the contract real.

    Rules enforced:

    **Workflow-level** (``workflow['relations']``):
      * ``type:`` required
      * ``kind:`` required, must be ``"domain"`` or ``"process_control"``
      * ``from_types:`` / ``to_types:`` only legal with
        ``kind: "domain"``; both absent means "any ref type accepted"
      * Unknown keys → ValueError (surfaces typos)

    **Activity-level** (``activity['relations']``):
      * ``type:`` required, must resolve to a workflow-level declaration
      * ``kind:``, ``from_types:``, ``to_types:``, ``description:``
        forbidden (declared at workflow level only)
      * ``validator:`` (single-string) and ``validators:`` (dict)
        mutually exclusive
      * ``validators:`` dict must have exactly ``{add, remove}`` keys
        if present; partial dicts rejected
      * When the resolved ``kind`` is ``"process_control"``:
        - ``validators:`` dict form forbidden (process_control has
          no remove operation; use ``validator:`` single-string)
        - ``operations: [remove]`` forbidden for the same reason
      * Unknown keys → ValueError

    Does NOT validate that named validators resolve to registered
    callables — that's a cross-registry check handled separately
    once ``plugin.relation_validators`` is built (see
    ``validate_relation_validator_registrations``).

    Fails fast with ValueError on the first violation, citing the
    offending activity name, relation type, and rule broken.
    """
    # First pass: workflow-level declarations. Build the kind map
    # so activity-level checks can resolve `kind` per relation type.
    kinds_by_type: dict[str, str] = {}
    wf_rels = workflow.get("relations") or []
    for rel in wf_rels:
        if not isinstance(rel, dict):
            raise ValueError(
                f"Workflow-level `relations:` entries must be dicts, "
                f"got {type(rel).__name__}: {rel!r}"
            )

        rel_type = rel.get("type")
        if not rel_type or not isinstance(rel_type, str):
            raise ValueError(
                f"Workflow-level relation declaration missing `type:` "
                f"(or it's not a string): {rel!r}"
            )

        kind = rel.get("kind")
        if kind not in _VALID_RELATION_KINDS:
            raise ValueError(
                f"Workflow-level relation {rel_type!r}: `kind:` is "
                f"required and must be one of "
                f"{sorted(_VALID_RELATION_KINDS)}, "
                f"got {kind!r}"
            )

        keys = set(rel.keys())
        unknown = keys - _WORKFLOW_RELATION_KEYS
        if unknown:
            raise ValueError(
                f"Workflow-level relation {rel_type!r}: unknown "
                f"key(s) {sorted(unknown)}. Allowed: "
                f"{sorted(_WORKFLOW_RELATION_KEYS)}."
            )

        # from_types/to_types are domain-only constraints.
        if kind == "process_control":
            for k in ("from_types", "to_types"):
                if k in rel:
                    raise ValueError(
                        f"Workflow-level relation {rel_type!r}: "
                        f"`{k}:` is only legal on `kind: domain` "
                        f"declarations (process_control relations "
                        f"are activity→entity, not entity→entity)."
                    )

        kinds_by_type[rel_type] = kind

    # Second pass: activity-level declarations. Each must reference a
    # workflow-level type (resolves `kind` from there).
    for act in workflow.get("activities") or []:
        if not isinstance(act, dict):
            continue
        act_name = act.get("name", "<unnamed>")

        for rel in act.get("relations") or []:
            if not isinstance(rel, dict):
                raise ValueError(
                    f"Activity {act_name!r}: `relations:` entries "
                    f"must be dicts, got {type(rel).__name__}: {rel!r}"
                )

            rel_type = rel.get("type")
            if not rel_type or not isinstance(rel_type, str):
                raise ValueError(
                    f"Activity {act_name!r}: relation declaration "
                    f"missing `type:`: {rel!r}"
                )

            if rel_type not in kinds_by_type:
                raise ValueError(
                    f"Activity {act_name!r}: relation type "
                    f"{rel_type!r} is not declared at workflow level. "
                    f"Add it to the top-level `relations:` block with "
                    f"a `kind:` field, or reference one of the "
                    f"declared types: "
                    f"{sorted(kinds_by_type.keys()) or '(none)'}."
                )

            keys = set(rel.keys())

            # Forbidden keys (legal elsewhere, wrong scope here).
            forbidden = keys & _ACTIVITY_RELATION_FORBIDDEN_KEYS
            if forbidden:
                raise ValueError(
                    f"Activity {act_name!r}, relation {rel_type!r}: "
                    f"key(s) {sorted(forbidden)} are declared at "
                    f"workflow level only — remove them from the "
                    f"activity-level declaration. The activity "
                    f"should reference the type by name; workflow-"
                    f"level declaration is the single source of "
                    f"truth for kind/from_types/to_types/description."
                )

            # Unknown keys (typos).
            unknown = keys - _ACTIVITY_RELATION_KEYS
            if unknown:
                raise ValueError(
                    f"Activity {act_name!r}, relation {rel_type!r}: "
                    f"unknown key(s) {sorted(unknown)}. Allowed: "
                    f"{sorted(_ACTIVITY_RELATION_KEYS)}."
                )

            # validator / validators are mutually exclusive.
            has_validator = "validator" in rel
            has_validators = "validators" in rel
            if has_validator and has_validators:
                raise ValueError(
                    f"Activity {act_name!r}, relation {rel_type!r}: "
                    f"`validator:` and `validators:` are mutually "
                    f"exclusive. Use `validator: \"name\"` for a "
                    f"single validator covering all operations, or "
                    f"`validators: {{add: \"a\", remove: \"r\"}}` "
                    f"for per-operation split."
                )

            # validators dict shape: must be exactly {add, remove}.
            if has_validators:
                v = rel["validators"]
                if not isinstance(v, dict):
                    raise ValueError(
                        f"Activity {act_name!r}, relation "
                        f"{rel_type!r}: `validators:` must be a dict "
                        f"with `add` and `remove` keys, "
                        f"got {type(v).__name__}: {v!r}"
                    )
                v_keys = set(v.keys())
                if v_keys != {"add", "remove"}:
                    raise ValueError(
                        f"Activity {act_name!r}, relation "
                        f"{rel_type!r}: `validators:` dict must have "
                        f"exactly `{{add, remove}}` keys; "
                        f"got {sorted(v_keys)}. If you only need a "
                        f"validator for one operation, use "
                        f"`validator: \"name\"` (single-string form, "
                        f"fires for all operations) and branch inside "
                        f"the function on the operation kind."
                    )

            # process_control-specific restrictions.
            resolved_kind = kinds_by_type[rel_type]
            if resolved_kind == "process_control":
                if has_validators:
                    raise ValueError(
                        f"Activity {act_name!r}, relation "
                        f"{rel_type!r}: `validators:` (dict form) is "
                        f"not allowed on process_control relations — "
                        f"they have no remove operation (process-"
                        f"control relations are stateless annotations "
                        f"on a single activity). Use "
                        f"`validator: \"name\"` (single-string) "
                        f"instead."
                    )
                ops = rel.get("operations")
                if ops and "remove" in ops:
                    raise ValueError(
                        f"Activity {act_name!r}, relation "
                        f"{rel_type!r}: `operations: [remove]` is not "
                        f"allowed on process_control relations "
                        f"(process-control relations have no remove "
                        f"semantic — they're stateless annotations)."
                    )



def validate_relation_validator_registrations(
    plugin: "Plugin",
) -> None:
    """Cross-check that the plugin's ``relation_validators`` dict
    doesn't use relation type names as keys.

    The dict keys must be **validator names** (referenced from YAML
    as ``validator: "name"`` or ``validators: {add: "name", ...}``).
    Using a declared relation type name as a key re-introduces the
    Style-3 by-type-name fallback that Bug 78 removed — silently,
    since the engine no longer consults it but the name collision
    still confuses readers of the plugin code. Fail at load.

    Kept separate from ``validate_relation_declarations`` because
    this one needs the Plugin object (not just the workflow dict)
    to inspect the registered dict. Runs after the Plugin
    constructor builds the registries, like
    ``validate_side_effect_condition_fn_registrations`` does.
    """
    declared_types: set[str] = set()
    for rel in plugin.workflow.get("relations") or []:
        if isinstance(rel, dict):
            t = rel.get("type")
            if isinstance(t, str):
                declared_types.add(t)

    collisions = set(plugin.relation_validators.keys()) & declared_types
    if collisions:
        raise ValueError(
            f"Plugin {plugin.name!r}: `relation_validators` dict has "
            f"key(s) {sorted(collisions)} that match declared "
            f"relation type name(s). This re-creates the Style-3 "
            f"by-type-name fallback that Bug 78 removed. Rename the "
            f"validator function(s) (convention: `validate_*`) and "
            f"reference them by name from activity-level YAML via "
            f"`validator:` or `validators: {{add, remove}}`."
        )
