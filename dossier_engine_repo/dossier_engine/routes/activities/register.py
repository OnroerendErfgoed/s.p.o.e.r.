"""
The ``register()`` entry point — called by ``routes/__init__.py``.

Registers the activity execution endpoints at both URL families
(workflow-agnostic ``/dossiers/...`` and workflow-scoped
``/{workflow}/dossiers/...``) plus per-workflow typed wrappers via
``typed.py``.

Contains nested closures ``_handle_single`` and ``_handle_batch``
that share FastAPI-level state (registry, get_user) across the
endpoints. The typed-route registrar lives in ``typed.py``; the
pure-function helpers (``_run_activity``, etc.) live in ``run.py``.
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
from .typed import _register_typed_route, _register_workflow_scoped_generic


def register(
    app: FastAPI,
    *,
    registry: PluginRegistry,
    get_user,
    global_access,
) -> None:
    """Register activity execution endpoints on the FastAPI app.

    Each endpoint is registered at both URL families:

    * Workflow-agnostic: ``/dossiers/{did}/...``
    * Workflow-scoped: ``/{workflow}/dossiers/{did}/...`` (resolves
      plugin from the URL instead of from the body or DB)

    Plus per-workflow typed wrappers (workflow-scoped only).
    """

    # --- Shared handler logic (used by both URL families) ---

    async def _handle_single(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User,
        workflow_override: str | None = None,
    ):
        """Execute a single activity. If workflow_override is set
        (from the URL), it takes precedence over request.workflow."""
        wf = workflow_override or request.workflow
        if not request.type:
            raise HTTPException(
                422, detail="'type' is required on the generic endpoint",
            )
        plugin, act_def = _resolve_plugin_and_def(
            registry, request.type, wf,
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
                workflow_name=wf,
                informed_by=request.informed_by,
            )

        result = await run_with_deadlock_retry(_work)
        # Post-commit: the transaction owned by run_with_deadlock_retry
        # has committed successfully. Emit the success audit event now.
        # On exception (HTTPException, ActivityError, or deadlock-retry
        # exhaustion) we never reach this line, so the audit log never
        # claims an activity that didn't actually commit.
        _emit_activity_success(
            user=user,
            dossier_id=dossier_id,
            act_def=act_def,
            activity_id=activity_id,
        )
        return result

    async def _handle_batch(
        dossier_id: UUID,
        request: BatchActivityRequest,
        user: User,
        workflow_override: str | None = None,
    ):
        """Execute a batch of activities atomically.

        Audit emission is deferred until after the outer
        ``run_with_deadlock_retry`` commit. Inside ``_work`` we
        accumulate one descriptor per successful ``_run_activity``
        return; if the batch rolls back (any item raises) or
        deadlock-retries (the retry starts fresh and rebuilds the
        list from scratch), no audit events are emitted for the
        failed attempt. Only the descriptors from the committing
        attempt reach the audit log. This is what Bug 7 called for.
        """
        wf = workflow_override or request.workflow
        # Per-item success descriptors collected during _work. The
        # list is reset on every retry attempt via the closure — the
        # outer helper above allocates it fresh inside _work so a
        # deadlock retry starts with an empty buffer. Each entry is
        # ``(act_def, activity_id)``; the caller user and dossier_id
        # are constant for the whole batch.
        pending_emits: list[tuple[dict, UUID]] = []

        async def _work(session):
            # Reset the buffer on every attempt. A deadlock retry
            # starts a fresh session/transaction and re-runs the
            # whole loop; the previous attempt's descriptors must be
            # discarded or we'd double-emit after the retry commits.
            pending_emits.clear()
            repo = Repository(session)
            results = []
            for item in request.activities:
                plugin, act_def = _resolve_plugin_and_def(
                    registry, item.type, wf,
                )
                item_activity_id = UUID(item.activity_id)
                try:
                    response = await _run_activity(
                        repo=repo,
                        plugin=plugin,
                        act_def=act_def,
                        dossier_id=dossier_id,
                        activity_id=item_activity_id,
                        user=user,
                        role=item.role,
                        used=item.used,
                        generated=item.generated,
                        relations=item.relations,
                        remove_relations=item.remove_relations,
                        workflow_name=wf,
                        informed_by=item.informed_by,
                    )
                except HTTPException as e:
                    prefix = (
                        f"Activity '{item.type}' "
                        f"(#{len(results) + 1}) failed: "
                    )
                    if isinstance(e.detail, dict):
                        new_detail = {
                            **e.detail,
                            "detail": f"{prefix}{e.detail.get('detail', '')}",
                        }
                        raise HTTPException(e.status_code, detail=new_detail)
                    raise HTTPException(
                        e.status_code, detail=f"{prefix}{e.detail}",
                    )
                await repo.session.flush()
                results.append(response)
                # Record the successful item for post-commit emit.
                # Recorded AFTER the successful _run_activity return
                # so a raise from execute_activity leaves no entry.
                pending_emits.append((act_def, item_activity_id))
            return {
                "activities": results,
                "dossier": results[-1]["dossier"] if results else None,
            }

        # A deadlock anywhere in the batch retries the whole batch with
        # a fresh transaction. This matches the existing atomicity
        # contract — either all items commit or none do.
        result = await run_with_deadlock_retry(_work)
        # Post-commit: emit one audit event per item that was part of
        # the committed attempt. ``pending_emits`` was rebuilt from
        # scratch on the final attempt (see the clear() at the top of
        # _work), so we don't double-emit for items that ran on an
        # earlier deadlock-retried attempt.
        for act_def, emitted_activity_id in pending_emits:
            _emit_activity_success(
                user=user,
                dossier_id=dossier_id,
                act_def=act_def,
                activity_id=emitted_activity_id,
            )
        return result

    # --- Workflow-agnostic routes ---

    @app.put(
        "/dossiers/{dossier_id}/activities/{activity_id}",
        response_model=FullResponse,
        tags=["activities"],
        summary="Execute an activity",
    )
    async def put_activity(
        dossier_id: UUID,
        activity_id: UUID,
        request: ActivityRequest,
        user: User = Depends(get_user),
    ):
        return await _handle_single(dossier_id, activity_id, request, user)

    @app.put(
        "/dossiers/{dossier_id}/activities",
        tags=["activities"],
        summary="Execute multiple activities atomically",
    )
    async def execute_batch_activities(
        dossier_id: UUID,
        request: BatchActivityRequest,
        user: User = Depends(get_user),
    ):
        return await _handle_batch(dossier_id, request, user)

    # --- Workflow-scoped routes (registered per plugin so the
    # workflow name appears literally in the URL, not as a
    # {workflow} placeholder in the OpenAPI schema) ---

    for plugin in registry.all_plugins():
        _register_workflow_scoped_generic(
            app=app,
            workflow_name=plugin.name,
            handle_single=_handle_single,
            handle_batch=_handle_batch,
            get_user=get_user,
        )

    # --- Per-workflow typed wrappers ---

    for plugin in registry.all_plugins():
        workflow_name = plugin.name
        for act_def in plugin.workflow.get("activities", []):
            if act_def.get("client_callable") is False:
                continue
            _register_typed_route(
                app=app,
                registry=registry,
                get_user=get_user,
                workflow_name=workflow_name,
                act_name=act_def["name"],
                act_label=act_def.get("label", act_def["name"]),
                act_desc=build_activity_description(act_def, plugin),
            )


