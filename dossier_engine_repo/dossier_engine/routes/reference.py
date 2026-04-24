"""
Workflow-scoped utility endpoints — reference data and validation.

* ``GET /{workflow}/reference/{list_name}`` — static reference lists
  (bijlagetypes, documenttypes, etc.) served from the plugin's YAML.
  Sub-millisecond, no DB hit, freely cacheable. **Public.** By product
  decision these are shared dropdown data that's freely available to
  any caller — they don't leak dossier state or enumerable references.

* ``POST /{workflow}/validate/{validator_name}`` and
  ``GET /{workflow}/validate`` (validator list) — lightweight field
  validation between activities. Plugin-registered callables that
  check one thing (URI resolution, cross-field rules) without
  touching the activity pipeline. **Require authentication** (Bug 58).
  Any authenticated user — regardless of role — can call these;
  the rationale for auth isn't role-based access control but
  reducing the attack surface: the validators effectively act as
  inventaris-lookup oracles (an ``erfgoedobject`` URI resolves to
  a label/type/gemeente, a ``handeling`` validator maps type →
  allowed-handelingen set), so gating on "has a valid session"
  closes an unauthenticated enumeration / DoS surface without
  adding any dossier scoping or permission logic.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException

from ..auth import User
from ..plugin import PluginRegistry, FieldValidator


def register(
    app: FastAPI,
    *,
    registry: PluginRegistry,
    get_user,
) -> None:
    """Register reference-data and validation endpoints.

    All routes are registered per-workflow so the workflow name
    appears literally in the URL (e.g. ``/toelatingen/reference``)
    rather than as a ``{workflow}`` placeholder.

    ``get_user`` is the FastAPI dependency that extracts the
    authenticated user from the request. It's only applied to the
    validate routes (Bug 58); the reference routes stay public by
    product decision (see module docstring).
    """

    for plugin in registry.all_plugins():
        _register_reference_routes(
            app=app,
            workflow_name=plugin.name,
            plugin=plugin,
            get_user=get_user,
        )

    # Per-validator typed routes.
    for plugin in registry.all_plugins():
        workflow_name = plugin.name
        for validator_name, validator_entry in plugin.field_validators.items():
            _register_validator_route(
                app=app,
                workflow_name=workflow_name,
                validator_name=validator_name,
                validator_entry=validator_entry,
                get_user=get_user,
            )


def _register_reference_routes(
    *,
    app: FastAPI,
    workflow_name: str,
    plugin,
    get_user,
) -> None:
    """Register GET reference + GET validate list endpoints for one workflow.

    ``get_user`` is only applied to the ``GET /{workflow}/validate``
    (validator-list) endpoint. The reference-data endpoints stay
    public — see module docstring.
    """
    ref_data = plugin.workflow.get("reference_data", {})

    @app.get(
        f"/{workflow_name}/reference",
        tags=[workflow_name],
        summary="All reference data",
        description=(
            "Returns every reference-data list defined in the "
            "workflow's YAML. One HTTP call populates all dropdowns."
        ),
    )
    async def get_all_reference_data():
        return ref_data

    get_all_reference_data.__name__ = f"reference_all_{workflow_name}"
    get_all_reference_data.__qualname__ = f"reference_all_{workflow_name}"

    @app.get(
        f"/{workflow_name}/reference/{{list_name}}",
        tags=[workflow_name],
        summary="Single reference data list",
        description=(
            "Returns a single reference-data list by name. "
            "Served from in-memory plugin config — sub-millisecond, "
            "no DB query."
        ),
    )
    async def get_reference_list(list_name: str):
        items = ref_data.get(list_name)
        if items is None:
            available = sorted(ref_data.keys()) if ref_data else []
            raise HTTPException(
                404,
                detail=f"No reference list '{list_name}' in workflow "
                       f"'{workflow_name}'. Available: {available}",
            )
        return {"items": items}

    get_reference_list.__name__ = f"reference_one_{workflow_name}"
    get_reference_list.__qualname__ = f"reference_one_{workflow_name}"

    @app.get(
        f"/{workflow_name}/validate",
        tags=[workflow_name],
        summary="List available validators",
        description=(
            "Returns the names of all field-level validators "
            "registered by this workflow's plugin. Authenticated "
            "users of any role may call this (Bug 58)."
        ),
    )
    async def list_validators(user: User = Depends(get_user)):
        names = sorted(plugin.field_validators.keys())
        return {"validators": names}

    list_validators.__name__ = f"validators_{workflow_name}"
    list_validators.__qualname__ = f"validators_{workflow_name}"


def _register_validator_route(
    *,
    app: FastAPI,
    workflow_name: str,
    validator_name: str,
    validator_entry,
    get_user,
) -> None:
    """Register one typed validation endpoint with proper OpenAPI
    schema. If the entry is a bare callable (legacy), falls back
    to generic dict input/output.

    Every registered endpoint takes an authenticated user (Bug 58)
    via ``Depends(get_user)`` — the validators are lookup oracles
    (``erfgoedobject`` → label/type/gemeente, ``handeling`` →
    allowed-actions), so auth-required keeps the inventaris surface
    from being trivially scraped by unauthenticated callers. Any
    authenticated user of any role may call these; there's no
    per-validator role gate."""
    from ..plugin import FieldValidator
    import inspect

    if isinstance(validator_entry, FieldValidator):
        fv = validator_entry
        fn = fv.fn
        req_model = fv.request_model
        resp_model = fv.response_model
        summary = fv.summary or f"Validate {validator_name}"
        description = fv.description or ""
    else:
        fn = validator_entry
        req_model = None
        resp_model = None
        summary = f"Validate {validator_name}"
        description = ""

    # Capture fn via closure (not default arg, which leaks into
    # the OpenAPI schema as a non-serializable default). The `user`
    # param is a FastAPI dependency, not a data field, so FastAPI
    # handles it without it appearing in the request/response schema.
    _fn = fn

    if req_model:
        async def endpoint(body, user: User = Depends(get_user)):
            return await _fn(body.model_dump())

        endpoint.__annotations__ = {
            "body": req_model,
            "user": User,
            "return": resp_model or dict,
        }
    else:
        async def endpoint(body: dict, user: User = Depends(get_user)):
            return await _fn(body)

    endpoint.__name__ = f"validate_{workflow_name}_{validator_name}"
    endpoint.__qualname__ = f"validate_{workflow_name}_{validator_name}"

    kwargs = {
        "tags": [workflow_name],
        "summary": summary,
        "description": description,
    }
    if resp_model:
        kwargs["response_model"] = resp_model

    app.post(
        f"/{workflow_name}/validate/{validator_name}",
        **kwargs,
    )(endpoint)
