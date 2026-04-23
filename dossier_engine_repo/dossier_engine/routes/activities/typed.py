"""
Per-workflow typed-route registrars.

* ``_register_typed_route`` — one route per (workflow, activity type)
  with typed request/response schemas and activity-specific OpenAPI
  description.
* ``_register_workflow_scoped_generic`` — one generic route per
  workflow, accepting any activity type in the body.

Both close over the shared handler logic in ``register.register()``
via the ``get_user`` + ``registry`` parameters threaded through.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException

from ...auth import User
from ...db import Repository, run_with_deadlock_retry
from ...engine import ActivityError, execute_activity
from ...plugin import Plugin, PluginRegistry
from .._helpers.errors import activity_error_to_http
from .._helpers.models import ActivityRequest, BatchActivityRequest, FullResponse
from .._helpers.typed_doc import build_activity_description

from .run import _resolve_plugin_and_def, _run_activity, _emit_activity_success


def _register_workflow_scoped_generic(
    *,
    app: FastAPI,
    workflow_name: str,
    handle_single,
    handle_batch,
    get_user,
) -> None:
    """Register generic single + batch activity routes for one workflow.

    The workflow name is baked into the URL literally (not as a
    path parameter) so the OpenAPI schema shows
    ``/toelatingen/dossiers/...`` instead of
    ``/{workflow}/dossiers/...``.
    """

    @app.put(
        f"/{workflow_name}/dossiers/{{dossier_id}}/activities/{{activity_id}}",
        response_model=FullResponse,
        tags=[workflow_name],
        summary="Execute an activity",
    )
    async def put_activity_scoped(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        return await handle_single(
            dossier_id, activity_id, request, user,
            workflow_override=workflow_name,
        )

    put_activity_scoped.__name__ = f"put_activity_{workflow_name}"
    put_activity_scoped.__qualname__ = f"put_activity_{workflow_name}"

    @app.put(
        f"/{workflow_name}/dossiers/{{dossier_id}}/activities",
        tags=[workflow_name],
        summary="Execute multiple activities atomically",
    )
    async def execute_batch_scoped(
        dossier_id: UUID,
        request: BatchActivityRequest,
        user: User = Depends(get_user),
    ):
        return await handle_batch(
            dossier_id, request, user,
            workflow_override=workflow_name,
        )

    execute_batch_scoped.__name__ = f"batch_{workflow_name}"
    execute_batch_scoped.__qualname__ = f"batch_{workflow_name}"


def _register_typed_route(
    *,
    app: FastAPI,
    registry: PluginRegistry,
    get_user,
    workflow_name: str,
    act_name: str,
    act_label: str,
    act_desc: str,
) -> None:
    """Register one per-workflow typed route for `act_name`.

    ``act_name`` is the *qualified* activity name (e.g.
    ``oe:dienAanvraagIn``) and appears directly in the URL path
    segment. This mirrors the entity URL convention where type
    segments are also qualified (``/entities/oe:aanvraag/...``), so
    the platform has one consistent rule: type-like path segments
    always carry the full qualified name.

    A URL with a qualified name looks like::

        PUT /toelatingen/dossiers/{did}/activities/{aid}/oe:dienAanvraagIn

    FastAPI accepts colons in path segments without issue. Clients
    that would rather use bare names can use the generic endpoint
    (``PUT /{workflow}/dossiers/{did}/activities/{aid}``) with
    ``"type": "dienAanvraagIn"`` in the body — the engine qualifies
    that to ``oe:dienAanvraagIn`` before resolution.
    """

    @app.put(
        f"/{workflow_name}/dossiers/{{dossier_id}}/activities/{{activity_id}}/{act_name}",
        response_model=FullResponse,
        tags=[workflow_name],
        summary=act_label,
        description=act_desc,
    )
    async def endpoint(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        # Stamp the qualified activity type on the request, so the
        # engine's resolve-plugin-and-def code sees the canonical
        # form regardless of what (if anything) the client supplied.
        request.type = act_name
        if not request.workflow:
            request.workflow = workflow_name

        plugin, act_def = _resolve_plugin_and_def(
            registry, act_name, workflow_name,
        )

        async def _work(session):
            repo = Repository(session)
            return await _run_activity(
                repo=repo,
                plugin=plugin,
                act_def=act_def,
                dossier_id=dossier_id,
                activity_id=activity_id,
                user=user,
                role=request.role,
                used=request.used,
                generated=request.generated,
                relations=request.relations,
                remove_relations=request.remove_relations,
                workflow_name=request.workflow,
                informed_by=request.informed_by,
            )

        result = await run_with_deadlock_retry(_work)
        # Post-commit emit (Bug 7): see _handle_single for the
        # rationale. The typed endpoint is a thin wrapper; emission
        # timing follows the same rule.
        _emit_activity_success(
            user=user,
            dossier_id=dossier_id,
            act_def=act_def,
            activity_id=activity_id,
        )
        return result

    # FastAPI uses function name for route uniqueness. Strip the
    # colon from the name since it's invalid in Python identifiers.
    safe_name = act_name.replace(":", "_")
    endpoint.__name__ = f"typed_{workflow_name}_{safe_name}"
    endpoint.__qualname__ = f"typed_{workflow_name}_{safe_name}"


