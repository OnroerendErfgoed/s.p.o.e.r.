"""
Activity-execution endpoints — single, batch, and per-workflow typed.

Two URL families:

**Workflow-scoped** (the workflow is in the URL, no DB lookup needed
to resolve the plugin):

* ``PUT /{workflow}/dossiers/{id}/activities/{aid}/{type}`` — typed.
* ``PUT /{workflow}/dossiers/{id}/activities/{aid}`` — generic single.
* ``PUT /{workflow}/dossiers/{id}/activities`` — generic batch.

**Workflow-agnostic** (the engine resolves the workflow from the
dossier's DB row or from ``request.workflow`` on creation):

* ``PUT /dossiers/{id}/activities/{aid}`` — generic single.
* ``PUT /dossiers/{id}/activities`` — generic batch.

All call into the same ``execute_activity`` engine entry point.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException

from ..auth import User
from ..audit import emit_dossier_audit
from ..db import Repository, run_with_deadlock_retry
from ..engine import ActivityError, execute_activity
from ..plugin import Plugin, PluginRegistry
from ._errors import activity_error_to_http
from ._models import ActivityRequest, BatchActivityRequest, FullResponse
from ._typed_doc import build_activity_description


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


def _resolve_plugin_and_def(
    registry: PluginRegistry,
    activity_type: str,
    workflow_name: str | None,
) -> tuple[Plugin, dict]:
    """Find the plugin and activity definition for `activity_type`.

    ``activity_type`` can arrive in bare (``dienAanvraagIn``) or
    qualified (``oe:dienAanvraagIn``) form — both resolve to the
    same activity. The registry stores activities by their qualified
    name (guaranteed by ``_normalize_activity_names`` at plugin
    load), so we qualify the incoming value before lookup.

    Two paths:

    1. **Registered activity** — `registry.get_for_activity` knows
       which plugin owns this activity type. Returns immediately.
    2. **First-activity-on-new-dossier** — the activity hasn't been
       registered with the registry yet (because the dossier doesn't
       exist), so we fall back to the explicit `workflow_name` from
       the request body, look up the plugin, and walk its activity
       list for a matching `name`.

    Raises 404 if neither path resolves.
    """
    from ..activity_names import qualify

    qualified_type = qualify(activity_type)

    result = registry.get_for_activity(qualified_type)
    if result is not None:
        return result

    if not workflow_name:
        raise HTTPException(
            404, detail=f"Unknown activity type: {activity_type}",
        )

    plugin = registry.get(workflow_name)
    if plugin is None:
        raise HTTPException(
            404, detail=f"Unknown workflow: {workflow_name}",
        )

    for a in plugin.workflow.get("activities", []):
        if a["name"] == qualified_type:
            return plugin, a
    raise HTTPException(
        404, detail=f"Unknown activity: {activity_type}",
    )


def _emit_activity_success(
    *,
    user: User,
    dossier_id: UUID,
    act_def: dict,
    activity_id: UUID,
) -> None:
    """Emit the success audit event for one activity execution.

    Extracted from ``_run_activity`` for Bug 7 fix: the event must
    fire *after* the DB transaction has committed, not during the
    transactional work. Callers invoke this only after
    ``run_with_deadlock_retry`` has returned success; on rollback or
    deadlock-retry this function is never called, so the audit log
    never claims an activity that didn't commit.

    The action name is derived from the activity definition:
    ``can_create_dossier: true`` (the entry-point activity, e.g.
    ``dienAanvraagIn``) emits ``dossier.created``; everything else
    emits ``dossier.updated``. The distinction matters to SIEM rules
    that track dossier lifecycle.

    Best-effort emission: ``emit_dossier_audit`` never raises, so a
    misbehaving audit sink cannot invalidate a committed DB
    transaction. See ``dossier_engine.audit`` for the log-sink
    contract.
    """
    is_root = bool(act_def.get("can_create_dossier"))
    action = "dossier.created" if is_root else "dossier.updated"
    emit_dossier_audit(
        action=action,
        user=user,
        dossier_id=dossier_id,
        outcome="allowed",
        activity_type=act_def.get("name"),
        activity_id=str(activity_id),
    )


async def _run_activity(
    *,
    repo: Repository,
    plugin: Plugin,
    act_def: dict,
    dossier_id: UUID,
    activity_id: UUID,
    user: User,
    role: str | None,
    used: list,
    generated: list,
    relations: list,
    remove_relations: list,
    workflow_name: str | None,
    informed_by: str | None,
) -> dict:
    """Call `execute_activity` with the standard argument set,
    forwarding any `ActivityError` to an `HTTPException` so FastAPI
    serializes it correctly.

    Centralizes the `[item.model_dump() for item in ...]` pattern
    that all three endpoints repeat — the engine takes plain dicts,
    not Pydantic models.

    Audit emission on writes:

    * **Denial path** (``ActivityError`` with code 403) emits
      ``dossier.denied`` directly here, in-transaction. This is
      correct on rollback: the denial decision *is* the auditable
      fact; whether the transaction rolled back or committed doesn't
      change that. The denial event reflects that the user attempted
      the action and was refused — independent of any DB state.
    * **Success path** does NOT emit here. The caller invokes
      ``_emit_activity_success`` after ``run_with_deadlock_retry``
      returns success, so the audit event fires only once per
      committed activity (not per deadlock-retry attempt, and never
      for an item that was part of a batch that later rolled back).

    Non-authorization errors (validation, 422, etc.) are not audited
    — those belong in the application log / Sentry, not the SIEM
    audit trail.
    """
    try:
        result = await execute_activity(
            plugin=plugin,
            activity_def=act_def,
            repo=repo,
            dossier_id=dossier_id,
            activity_id=activity_id,
            user=user,
            role=role,
            used_items=[u.model_dump() for u in used],
            generated_items=[g.model_dump() for g in generated],
            relation_items=[r.model_dump(by_alias=True) for r in relations],
            remove_relation_items=[r.model_dump(by_alias=True) for r in remove_relations],
            workflow_name=workflow_name,
            informed_by=informed_by,
        )
    except ActivityError as e:
        # Write-side authorization denial: emit dossier.denied so this
        # shows up in the SIEM stream alongside read-side denials from
        # routes/access.py. Non-403 errors (validation, business rule
        # violations) are NOT audited — those are app-level concerns,
        # not security events.
        #
        # This emit happens in-transaction, which is correct: the
        # denial decision is the auditable fact regardless of whether
        # downstream DB work rolls back. A 403 means no material DB
        # writes happened yet anyway — ``authorize`` runs before
        # ``resolve_used`` / ``process_generated`` / ``persistence``
        # in ``execute_activity``.
        #
        # Attribute names: ``ActivityError`` stores ``status_code`` and
        # ``detail`` (see ``engine/errors.py``). Earlier versions of
        # this code read ``code`` and ``message`` via getattr-with-
        # default, which silently returned ``None`` for every denial —
        # meaning write-side ``dossier.denied`` events had never
        # reached the SIEM at all. Fixed under Bug 77; the regression
        # test in ``TestAuditEmitIsPostCommit.test_denial_still_emits_in_transaction``
        # pins the emit path and the attribute names.
        if e.status_code == 403:
            emit_dossier_audit(
                action="dossier.denied",
                user=user,
                dossier_id=dossier_id,
                outcome="denied",
                reason=str(e.detail),
                activity_type=act_def.get("name"),
                activity_id=str(activity_id),
            )
        raise activity_error_to_http(e)

    return result
