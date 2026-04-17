"""
Workflow-scoped utility endpoints — reference data and validation.

* ``GET /{workflow}/reference/{list_name}`` — static reference lists
  (bijlagetypes, documenttypes, etc.) served from the plugin's YAML.
  Sub-millisecond, no DB hit, freely cacheable.

* ``POST /{workflow}/validate/{validator_name}`` — lightweight field
  validation between activities. Plugin-registered callables that
  check one thing (URI resolution, cross-field rules) without
  touching the activity pipeline.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..plugin import PluginRegistry


def register(
    app: FastAPI,
    *,
    registry: PluginRegistry,
) -> None:
    """Register reference-data and validation endpoints."""

    @app.get(
        "/{workflow}/reference",
        tags=["reference"],
        summary="All reference data for a workflow",
        description=(
            "Returns every reference-data list defined in the "
            "workflow's YAML. One HTTP call populates all dropdowns."
        ),
    )
    async def get_all_reference_data(workflow: str):
        plugin = registry.get(workflow)
        if not plugin:
            raise HTTPException(404, detail=f"Unknown workflow: {workflow}")
        ref_data = plugin.workflow.get("reference_data", {})
        return ref_data

    @app.get(
        "/{workflow}/reference/{list_name}",
        tags=["reference"],
        summary="Reference data list",
        description=(
            "Returns a single reference-data list by name. "
            "Served from in-memory plugin config — sub-millisecond, "
            "no DB query."
        ),
    )
    async def get_reference_list(workflow: str, list_name: str):
        plugin = registry.get(workflow)
        if not plugin:
            raise HTTPException(404, detail=f"Unknown workflow: {workflow}")
        ref_data = plugin.workflow.get("reference_data", {})
        items = ref_data.get(list_name)
        if items is None:
            available = sorted(ref_data.keys()) if ref_data else []
            raise HTTPException(
                404,
                detail=f"No reference list '{list_name}' in workflow "
                       f"'{workflow}'. Available: {available}",
            )
        return {"items": items}

    # --- Validation endpoints ---

    @app.get(
        "/{workflow}/validate",
        tags=["validation"],
        summary="List available validators",
        description=(
            "Returns the names of all field-level validators "
            "registered by this workflow's plugin."
        ),
    )
    async def list_validators(workflow: str):
        plugin = registry.get(workflow)
        if not plugin:
            raise HTTPException(404, detail=f"Unknown workflow: {workflow}")
        names = sorted(plugin.field_validators.keys())
        return {"validators": names}

    @app.post(
        "/{workflow}/validate/{validator_name}",
        tags=["validation"],
        summary="Run a field-level validator",
        description=(
            "Lightweight validation between activities. Runs a "
            "plugin-registered callable that checks one thing "
            "(URI resolution, cross-field rules, etc.) without "
            "touching the activity pipeline. No DB writes, no "
            "PROV records."
        ),
    )
    async def run_validator(workflow: str, validator_name: str, body: dict):
        plugin = registry.get(workflow)
        if not plugin:
            raise HTTPException(404, detail=f"Unknown workflow: {workflow}")

        validator = plugin.field_validators.get(validator_name)
        if validator is None:
            available = sorted(plugin.field_validators.keys())
            raise HTTPException(
                404,
                detail=f"No validator '{validator_name}' in workflow "
                       f"'{workflow}'. Available: {available}",
            )

        result = await validator(body)
        return result
