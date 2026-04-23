"""
Plugin registry construction — resolves dotted paths from YAML into
concrete callables and builds the eight Plugin registries.

Public entry points:
* ``build_entity_registries_from_workflow`` — builds ``entity_models``
  and ``entity_schemas`` from ``entity_types[*].model`` / ``schemas``
  dotted paths.
* ``build_callable_registries_from_workflow`` — builds the eight
  Plugin callable registries (handlers, validators, task_handlers,
  etc.) from dotted paths scattered through the workflow YAML.

Private helpers:
* ``_import_dotted`` / ``_import_dotted_callable`` — dotted-path
  resolution with clear error messages.
"""
from __future__ import annotations

import importlib
from typing import Any

from pydantic import BaseModel


def _import_dotted(path: str) -> type[BaseModel]:
    """Resolve a fully-qualified 'pkg.module.ClassName' string to a class.

    Raises ValueError with a clear message on failure — callers should let
    this propagate at plugin load time so misconfiguration fails fast.
    """
    if "." not in path:
        raise ValueError(
            f"Invalid model path {path!r}: must be a fully-qualified "
            f"'package.module.ClassName' string"
        )
    module_path, _, class_name = path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ValueError(
            f"Cannot import module {module_path!r} for model {path!r}: {e}"
        ) from e
    try:
        cls = getattr(module, class_name)
    except AttributeError as e:
        raise ValueError(
            f"Module {module_path!r} has no class {class_name!r} "
            f"(referenced as {path!r})"
        ) from e
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise ValueError(
            f"{path!r} does not resolve to a Pydantic BaseModel subclass"
        )
    return cls


def _import_dotted_callable(path: str, *, context: str = "") -> Any:
    """Resolve a fully-qualified 'pkg.module.name' string to any Python
    object (function, FieldValidator instance, etc.).

    Parallel to ``_import_dotted`` but without the BaseModel-subclass check.
    Used by ``build_callable_registries_from_workflow`` to resolve the
    eight Plugin Callable registries (handlers, validators, task_handlers,
    status_resolvers, task_builders, side_effect_conditions,
    relation_validators, field_validators) from workflow YAML at plugin
    load time. See Obs 95 / Round 28 for the migration rationale — prior
    to this, the registries were keyed by short names that only resolved
    at first-lookup runtime, causing typos to fail late.

    The resolved object is not type-checked here — some registries hold
    async callables, some hold ``FieldValidator`` instances, some hold
    relation-validator callables with a specific signature. Call sites
    do their own signature / type validation where they care.

    Raises ValueError with a clear message on failure, including the
    optional ``context`` string for call-site attribution ("activity
    'dienAanvraagIn' handler" is more useful than just the path).
    """
    if not isinstance(path, str) or "." not in path:
        where = f" (in {context})" if context else ""
        raise ValueError(
            f"Invalid dotted path {path!r}{where}: must be a fully-qualified "
            f"'package.module.name' string"
        )
    module_path, _, attr_name = path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        where = f" (in {context})" if context else ""
        raise ValueError(
            f"Cannot import module {module_path!r} for {path!r}{where}: {e}"
        ) from e
    try:
        obj = getattr(module, attr_name)
    except AttributeError as e:
        where = f" (in {context})" if context else ""
        raise ValueError(
            f"Module {module_path!r} has no attribute {attr_name!r} "
            f"(referenced as {path!r}{where})"
        ) from e
    return obj


def build_entity_registries_from_workflow(
    workflow: dict,
) -> tuple[dict[str, type[BaseModel]], dict[tuple[str, str], type[BaseModel]]]:
    """Walk the workflow's `entity_types` block and build the plugin's
    `entity_models` and `entity_schemas` registries by resolving dotted
    paths via importlib.

    YAML shape:

        entity_types:
          - type: "oe:aanvraag"
            model: "dossier_toelatingen.entities.Aanvraag"  # default/unversioned
            schemas:                                            # optional
              v1: "dossier_toelatingen.entities.Aanvraag"
              v2: "dossier_toelatingen.entities.AanvraagV2"

    Rules:
      * `model` is optional. If present, it populates `entity_models[type]`
        and serves as the legacy-path default for this type.
      * `schemas` is optional. Each entry populates
        `entity_schemas[(type, version)]`. Types without `schemas` stay
        unversioned and fall back to `model`.
      * Either `model` or `schemas` must be present for a type to contribute
        anything. Types with neither are structural-only (cardinality decl
        only) and are silently skipped here.
      * Paths must resolve via `_import_dotted` or plugin load fails.

    After this function returns, the engine may still inject additional
    models (e.g. `system:task`) into the returned `entity_models` dict —
    that's fine, the dict is plain.
    """
    entity_models: dict[str, type[BaseModel]] = {}
    entity_schemas: dict[tuple[str, str], type[BaseModel]] = {}

    for et in workflow.get("entity_types", []):
        type_name = et.get("type")
        if not type_name:
            continue

        model_path = et.get("model")
        if model_path:
            entity_models[type_name] = _import_dotted(model_path)

        schemas = et.get("schemas") or {}
        for version, path in schemas.items():
            entity_schemas[(type_name, str(version))] = _import_dotted(path)

    return entity_models, entity_schemas

# Keys in the per-relation-type validator dict form. Used by both the
# workflow-level ``relation_types`` block and the activity-level
# ``relations`` block. Mirrors the accepted shape documented in
# validate_relation_declarations.
_RELATION_VALIDATOR_DICT_KEYS = frozenset({"add", "remove"})


def build_callable_registries_from_workflow(
    workflow: dict,
) -> dict[str, dict[str, Any]]:
    """Walk the workflow YAML and build the eight Plugin Callable registries
    by resolving dotted paths via importlib.

    Obs 95 / Round 28: prior to this, each plugin's ``create_plugin()``
    built eight ``dict[str, Callable]`` by hand, keyed by short names that
    the YAML referenced (``handler: "set_dossier_access"``). Typos failed
    at runtime-of-first-lookup, not load time. This function removes the
    short-name indirection — YAML now carries a fully-qualified Python
    path (``handler: "dossier_toelatingen.handlers.set_dossier_access"``)
    and the registries are built once at plugin load time.

    Returns a dict with keys: ``handlers``, ``validators``,
    ``task_handlers``, ``status_resolvers``, ``task_builders``,
    ``side_effect_conditions``, ``relation_validators``,
    ``field_validators``.

    **Registry keys are the dotted paths themselves.** Engine lookup
    sites do ``plugin.handlers.get(handler_name)`` where
    ``handler_name`` is read from the same YAML key that seeded the
    registry — so the dict lookup still works. The indirection layer
    ("short name → callable via a hand-built dict") is what's gone.

    Exception: ``field_validators``. Its key becomes part of the
    ``POST /{workflow}/validate/{name}`` URL, so it cannot be a dotted
    Python path. The workflow YAML's ``field_validators:`` block is
    therefore a mapping of ``url_key → dotted_path``; the returned
    registry is keyed by the url_key with the resolved object as value.

    Missing YAML blocks are fine — the corresponding registry comes back
    empty. Bad paths raise ValueError at plugin load with a clear
    per-reference context string.
    """
    handlers: dict[str, Any] = {}
    validators: dict[str, Any] = {}
    task_handlers: dict[str, Any] = {}
    status_resolvers: dict[str, Any] = {}
    task_builders: dict[str, Any] = {}
    side_effect_conditions: dict[str, Any] = {}
    relation_validators: dict[str, Any] = {}
    field_validators: dict[str, Any] = {}

    def _resolve_validator_ref(ref: Any, context: str) -> None:
        """Resolve a validator reference — either a single dotted path
        string or a ``{add: path, remove: path}`` dict — into the
        ``validators`` registry. Silent-ok on non-str/non-dict input;
        shape validation is the job of validate_relation_declarations.
        """
        if isinstance(ref, str):
            if ref not in validators:
                validators[ref] = _import_dotted_callable(
                    ref, context=context,
                )
        elif isinstance(ref, dict):
            for op_key in _RELATION_VALIDATOR_DICT_KEYS:
                op_path = ref.get(op_key)
                if isinstance(op_path, str) and op_path not in validators:
                    validators[op_path] = _import_dotted_callable(
                        op_path, context=f"{context} [{op_key}]",
                    )

    def _resolve_relation_validator_ref(ref: Any, context: str) -> None:
        """Mirror of ``_resolve_validator_ref`` for relation-level
        validators. Relation validators live in a separate registry from
        activity-level ``validators:`` so the two don't collide — Bug 78
        structurally prevents the name-collision that Bug 66 patched.
        """
        if isinstance(ref, str):
            if ref not in relation_validators:
                relation_validators[ref] = _import_dotted_callable(
                    ref, context=context,
                )
        elif isinstance(ref, dict):
            for op_key in _RELATION_VALIDATOR_DICT_KEYS:
                op_path = ref.get(op_key)
                if isinstance(op_path, str) and op_path not in relation_validators:
                    relation_validators[op_path] = _import_dotted_callable(
                        op_path, context=f"{context} [{op_key}]",
                    )

    # Workflow-level relation_types block. Bug 78's "types declared once
    # at workflow level" contract — each entry may carry a validator
    # (single string) or validators (dict with add/remove).
    for rel in workflow.get("relation_types", []) or []:
        if not isinstance(rel, dict):
            continue
        rel_type = rel.get("type", "<unknown>")
        if "validator" in rel:
            _resolve_relation_validator_ref(
                rel["validator"],
                context=f"relation_type {rel_type!r} validator",
            )
        if "validators" in rel:
            _resolve_relation_validator_ref(
                rel["validators"],
                context=f"relation_type {rel_type!r} validators",
            )

    # Activity-level scan. Most of the eight registries source from here.
    for act in workflow.get("activities", []) or []:
        if not isinstance(act, dict):
            continue
        act_name = act.get("name", "<unknown>")

        # handler: single dotted path.
        handler_path = act.get("handler")
        if isinstance(handler_path, str):
            if handler_path not in handlers:
                handlers[handler_path] = _import_dotted_callable(
                    handler_path,
                    context=f"activity {act_name!r} handler",
                )

        # status_resolver: single dotted path.
        sr_path = act.get("status_resolver")
        if isinstance(sr_path, str):
            if sr_path not in status_resolvers:
                status_resolvers[sr_path] = _import_dotted_callable(
                    sr_path,
                    context=f"activity {act_name!r} status_resolver",
                )

        # task_builders: list of dotted paths.
        for tb_path in act.get("task_builders") or []:
            if isinstance(tb_path, str) and tb_path not in task_builders:
                task_builders[tb_path] = _import_dotted_callable(
                    tb_path,
                    context=f"activity {act_name!r} task_builders",
                )

        # validators: list of dicts with "name" key (dotted path).
        for v_entry in act.get("validators") or []:
            if not isinstance(v_entry, dict):
                continue
            v_path = v_entry.get("name")
            if isinstance(v_path, str) and v_path not in validators:
                validators[v_path] = _import_dotted_callable(
                    v_path,
                    context=f"activity {act_name!r} validator",
                )

        # tasks: list of dicts, each with "function" dotted path.
        for t_entry in act.get("tasks") or []:
            if not isinstance(t_entry, dict):
                continue
            t_path = t_entry.get("function")
            if isinstance(t_path, str) and t_path not in task_handlers:
                task_handlers[t_path] = _import_dotted_callable(
                    t_path,
                    context=f"activity {act_name!r} task function",
                )

        # side_effects[*].condition_fn: dotted path.
        for se_entry in act.get("side_effects") or []:
            if not isinstance(se_entry, dict):
                continue
            cfn_path = se_entry.get("condition_fn")
            if isinstance(cfn_path, str) and cfn_path not in side_effect_conditions:
                side_effect_conditions[cfn_path] = _import_dotted_callable(
                    cfn_path,
                    context=(
                        f"activity {act_name!r} side-effect "
                        f"{se_entry.get('activity')!r} condition_fn"
                    ),
                )

        # Activity-level relations: each may carry its own validator /
        # validators. These go to the relation_validators registry, not
        # the activity-level validators registry — keeping them separate
        # is what Bug 78 enforces.
        for rel_entry in act.get("relations") or []:
            if not isinstance(rel_entry, dict):
                continue
            rel_type = rel_entry.get("type", "<unknown>")
            if "validator" in rel_entry:
                _resolve_relation_validator_ref(
                    rel_entry["validator"],
                    context=(
                        f"activity {act_name!r} relation "
                        f"{rel_type!r} validator"
                    ),
                )
            if "validators" in rel_entry:
                _resolve_relation_validator_ref(
                    rel_entry["validators"],
                    context=(
                        f"activity {act_name!r} relation "
                        f"{rel_type!r} validators"
                    ),
                )

    # field_validators: top-level YAML block, shape is url_key → dotted.
    # Separate from the other seven because the key is part of the URL
    # (POST /{workflow}/validate/{url_key}) so it has to stay a short,
    # user-facing string rather than a Python dotted path.
    fv_block = workflow.get("field_validators") or {}
    if isinstance(fv_block, dict):
        for url_key, fv_path in fv_block.items():
            if not isinstance(fv_path, str):
                continue
            field_validators[url_key] = _import_dotted_callable(
                fv_path,
                context=f"field_validator {url_key!r}",
            )

    return {
        "handlers": handlers,
        "validators": validators,
        "task_handlers": task_handlers,
        "status_resolvers": status_resolvers,
        "task_builders": task_builders,
        "side_effect_conditions": side_effect_conditions,
        "relation_validators": relation_validators,
        "field_validators": field_validators,
    }

