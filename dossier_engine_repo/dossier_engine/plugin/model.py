"""
Plugin dataclass + registry + FieldValidator.

The ``Plugin`` dataclass carries all of a plugin's concrete
configuration: the workflow dict, the eight callable registries
(built by ``registries.py``), entity models, constants, and the
optional post-activity-hook / search-route-factory / common-doc
builders.

``PluginRegistry`` holds the set of loaded plugins for a single app.

``FieldValidator`` is a small dataclass describing a named field
validator with optional request/response Pydantic models for
OpenAPI typing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from pydantic import BaseModel

from .normalize import _normalize_plugin_activity_names


@dataclass
class FieldValidator:
    """A field-level validator with optional request/response models
    for OpenAPI documentation.

    When ``request_model`` and ``response_model`` are provided, the
    engine generates a typed endpoint with proper schema documentation
    in the Swagger UI. Without them, the endpoint accepts/returns
    generic JSON.

    Example::

        FieldValidator(
            fn=validate_erfgoedobject,
            request_model=ErfgoedobjectRequest,
            response_model=ErfgoedobjectResponse,
            summary="Valideer erfgoedobject URI",
            description="Controleer of de URI verwijst naar een gekend erfgoedobject.",
        )
    """
    fn: Callable
    request_model: type[BaseModel] | None = None
    response_model: type[BaseModel] | None = None
    summary: str | None = None
    description: str | None = None


@dataclass

class Plugin:
    """A workflow plugin registration."""

    name: str  # workflow name, e.g. "toelatingen"
    workflow: dict  # parsed workflow YAML
    entity_models: dict[str, type[BaseModel]]  # entity_type_name → Pydantic model (legacy/default)

    # Versioned schemas: (entity_type, schema_version) → Pydantic model.
    # Optional. When an entity row has a non-NULL schema_version, the engine
    # routes lookups through this registry first. NULL schema_version always
    # falls back to entity_models (legacy path). Plugins that don't version
    # anything can leave this empty.
    entity_schemas: dict[tuple[str, str], type[BaseModel]] = field(default_factory=dict)

    handlers: dict[str, Callable] = field(default_factory=dict)  # handler_name → async function
    validators: dict[str, Callable] = field(default_factory=dict)  # validator_name → async function
    task_handlers: dict[str, Callable] = field(default_factory=dict)  # task_name → async function

    # Split-style hooks, opt-in via YAML activity declarations. An
    # activity can declare a `status_resolver: "name"` and/or
    # `task_builders: [...]` to lift those concerns out of the
    # handler into dedicated, single-responsibility functions.
    #
    # When an activity declares a status_resolver, its handler MUST
    # NOT return `status` — the engine raises ActivityError(500) if
    # both are set. Same rule for task_builders + handler `tasks`.
    # This keeps "who decides X" unambiguous for every activity.
    #
    # Signatures:
    #   async def resolver(context: ActivityContext) -> str | None
    #   async def task_builder(context: ActivityContext) -> list[dict]
    #
    # Both styles coexist indefinitely — legacy handlers that return
    # content + status + tasks keep working untouched. See the plugin
    # guidebook for the decision criteria ("when to split").
    status_resolvers: dict[str, Callable] = field(default_factory=dict)
    task_builders: dict[str, Callable] = field(default_factory=dict)

    # Named predicates for gating side-effect execution. YAML-declared
    # side effects can reference these via ``condition_fn: "name"`` as
    # an alternative to the inline ``condition: {entity_type, field,
    # value}`` dict form. The function receives the same
    # ``ActivityContext`` that handlers see and returns a bool: True
    # means "run the side effect," False means skip.
    #
    # ``condition`` and ``condition_fn`` are mutually exclusive per
    # side-effect entry — the engine raises at plugin load if both
    # are set. Choose the dict form for simple ``field == value``
    # checks (reads at a glance in YAML); the function form for
    # anything else (entity existence, date comparisons, value-in-set,
    # boolean combinations, anything testable as a pure function).
    side_effect_conditions: dict[str, Callable] = field(default_factory=dict)

    # Validators for custom PROV-extension relations (e.g. oe:neemtAkteVan).
    # Keyed by relation type string. Each validator receives the full
    # activity context (resolved used rows, pending generated items, the
    # relation entries of its type) and raises ActivityError to reject the
    # request. Returning normally means "accepted". The engine imposes no
    # semantics on the return value — validators own their own failure
    # conditions and payload shapes. Signature:
    #   async def validator(*, plugin, repo, dossier_id, activity_def,
    #                       entries, used_rows_by_ref, generated_items) -> None
    relation_validators: dict[str, Callable] = field(default_factory=dict)

    # Lightweight field-level validators callable between activities
    # via POST /{workflow}/validate/{name}. Each entry is either a
    # bare async callable (legacy) or a FieldValidator with request/
    # response models for OpenAPI documentation.
    field_validators: dict[str, "Callable | FieldValidator"] = field(default_factory=dict)

    # Called after each activity completes (inside the transaction).
    # Signature: async def hook(repo, dossier_id, activity_type, status, entities) -> None
    # Use to update Elasticsearch indices.
    post_activity_hook: Callable | None = None

    # Called after persistence but BEFORE the cached_status / eligible_activities
    # projection and BEFORE transaction commit. Unlike post_activity_hook,
    # exceptions raised here are NOT swallowed — they propagate and roll the
    # whole activity back. Use for synchronous validation / side effects that
    # MUST succeed or the activity should be rejected: PKI signature checks,
    # external ID reservations, mandatory file service operations, etc.
    #
    # Signature:
    #   async def hook(*, repo, dossier_id, plugin, activity_def,
    #                     generated_items, used_rows, user) -> None
    #
    # Hooks run in declaration order. First raise wins — subsequent hooks
    # don't run. Raise ActivityError for structured HTTP responses; any other
    # exception becomes a 500.
    pre_commit_hooks: list[Callable] = field(default_factory=list)

    # Called during route registration. Receives (app, get_user) and should
    # register workflow-specific search endpoints like /dossiers/{workflow_name}/...
    search_route_factory: Callable | None = None

    # Plugin-owned builder for the engine-level common-index document.
    # Signature: ``async def build(repo, dossier_id) -> dict | None``.
    #
    # Invoked by ``dossier_engine.search.common_index.reindex_all``
    # when the engine walks every dossier. Each plugin that owns
    # dossiers of its workflow supplies this so the engine-level
    # reindex writes rich docs (with onderwerp + full per-dossier
    # ACL) instead of the bare-minimum fallback. Without this, the
    # fallback emits docs with empty onderwerp and only global-access
    # roles in ``__acl__`` — which makes every non-global user
    # invisible from search after a reindex. Return None to skip the
    # dossier (counted as "skipped" in the reindex summary).
    build_common_doc_for_dossier: Callable | None = None

    # Workflow-scoped constants/config. A Pydantic BaseSettings instance
    # populated at plugin load from (in precedence order, highest wins):
    #   1. Environment variables — operator escape hatch, secrets
    #   2. workflow.yaml's `constants.values` block — plugin author's
    #      domain-level tuning
    #   3. Pydantic class defaults
    # Handlers access this via context.constants; hooks and factories
    # access via plugin.constants. None if the plugin doesn't declare
    # a constants class.
    constants: Any = None

    # Defaults for engine-provided types. system:task and system:note are
    # multi-cardinality (many per dossier); oe:dossier_access is a singleton.
    # These are overlaid by plugin workflow declarations if present.
    _ENGINE_CARDINALITIES: dict = field(
        default_factory=lambda: {
            "system:task": "multiple",
            "system:note": "multiple",
            "oe:dossier_access": "single",
            "external": "multiple",
        },
        repr=False,
    )

    def cardinality_of(self, entity_type: str) -> str:
        """Return the declared cardinality of an entity type: 'single' or
        'multiple'. Checks the workflow's `entity_types` block first, then
        falls back to engine defaults for system/external types, then
        defaults to 'single' for anything unknown."""
        for et in self.workflow.get("entity_types", []):
            if et.get("type") == entity_type:
                c = et.get("cardinality", "single")
                return c if c in ("single", "multiple") else "single"
        return self._ENGINE_CARDINALITIES.get(entity_type, "single")

    def is_singleton(self, entity_type: str) -> bool:
        return self.cardinality_of(entity_type) == "single"

    def resolve_schema(
        self, entity_type: str, schema_version: str | None
    ) -> type[BaseModel] | None:
        """Resolve the Pydantic model class for an entity of a given type
        and schema version.

        Resolution rules:
        - If `schema_version` is set, look it up in `entity_schemas`. If not
          found there, fall back to `entity_models[entity_type]` — this keeps
          the legacy path available when a plugin introduces versioning for
          some types but not others.
        - If `schema_version` is None (legacy/unversioned row, or a plugin
          that doesn't version this type), use `entity_models[entity_type]`.
        - Returns None if nothing matches, in which case callers should skip
          content validation / typed access.
        """
        if schema_version is not None:
            model = self.entity_schemas.get((entity_type, schema_version))
            if model is not None:
                return model
        return self.entity_models.get(entity_type)

    def find_activity_def(self, activity_type: str) -> dict | None:
        """Return the activity definition dict for `activity_type`, or
        None if this plugin's workflow doesn't declare it.

        Accepts bare or qualified input. Compares by the *local
        name* portion since the stored YAML may have been registered
        via ``PluginRegistry.register`` (which qualifies everything
        to ``oe:``) or may still be bare if the plugin wasn't
        registered (test fixtures constructing Plugin directly).

        A linear scan — workflows have a few dozen activities at
        most, so the cost is negligible compared to caching.
        """
        from ..prov.activity_names import local_name
        target_local = local_name(activity_type)
        for act in self.workflow.get("activities", []):
            if local_name(act.get("name", "")) == target_local:
                return act
        return None


class PluginRegistry:
    """Registry of all loaded plugins."""

    def __init__(self):
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin):
        """Register a plugin.

        Normalizes all activity names to qualified form (``oe:foo``
        instead of bare ``foo``). This runs on every registration
        path — ``create_app`` and direct test fixtures — so the rest
        of the engine always sees consistent qualified names.
        """
        _normalize_plugin_activity_names(plugin)
        self._plugins[plugin.name] = plugin

    def get(self, workflow_name: str) -> Plugin | None:
        return self._plugins.get(workflow_name)

    def get_for_activity(self, activity_type: str) -> tuple[Plugin, dict] | None:
        """Find which plugin owns an activity type. Returns (plugin, activity_def).

        Accepts both bare (``submit``) and qualified (``oe:submit``)
        forms — bare names are qualified to the default prefix first.
        The registry stores activities with qualified names, so the
        lookup always compares qualified-to-qualified.
        """
        from ..prov.activity_names import qualify
        qualified = qualify(activity_type)
        for plugin in self._plugins.values():
            for act in plugin.workflow.get("activities", []):
                if act["name"] == qualified:
                    return plugin, act
        return None

    def all_plugins(self) -> list[Plugin]:
        return list(self._plugins.values())

    def all_workflow_names(self) -> list[str]:
        return list(self._plugins.keys())


