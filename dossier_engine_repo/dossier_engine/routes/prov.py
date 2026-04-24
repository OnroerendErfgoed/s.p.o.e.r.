"""
PROV export and visualization endpoints.

- GET /dossiers/{id}/prov                  → PROV-JSON export (audit-level)
- GET /dossiers/{id}/prov/graph/timeline   → Timeline graph (honours per-user visibility)
- GET /dossiers/{id}/prov/graph/columns    → Column-layout graph (audit-level, in prov_columns.py)
- GET /dossiers/{id}/archive               → PDF/A archive export (audit-level)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from ..auth import User
from ..db import get_session_factory, Repository
from ..db.models import ActivityRow, EntityRow, AssociationRow, UsedRow
from ..plugin import PluginRegistry
from .access import (
    check_dossier_access, check_audit_access, get_visibility_from_entry,
)

from sqlalchemy import select

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


router = APIRouter(tags=["prov"])


def register_prov_routes(
    app,
    registry: PluginRegistry,
    get_user,
    global_access: list[dict] | None = None,
    global_audit_access: list[str] | None = None,
):
    """Register PROV export and visualization routes.

    The three audit-level endpoints (``/prov``, ``/prov/graph/columns``,
    ``/archive``) use ``check_audit_access`` with ``global_audit_access``
    — they bypass per-user activity/entity filtering and expose the
    complete provenance record. The ``/prov/graph/timeline`` endpoint
    uses ordinary ``check_dossier_access`` and honors per-user
    filtering — it's the user-facing view.
    """

    @app.get(
        "/dossiers/{dossier_id}/prov",
        tags=["prov"],
        summary="PROV-JSON export (audit view)",
        description=(
            "Audit-level export of the complete provenance graph in "
            "PROV-JSON format. Always includes system activities, "
            "tasks, and all entities regardless of per-user filtering. "
            "Requires a role in global_audit_access or the dossier's "
            "audit_access list."
        ),
    )
    async def get_prov_json(
        dossier_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            plugin = registry.get(dossier.workflow)

            # Audit-level access: no per-user filtering below.
            await check_audit_access(
                repo, dossier_id, user, global_audit_access,
            )

            # Build the PROV-JSON document. All the rowset loading
            # and graph construction lives in ``dossier_engine.prov.json_ld``
            # — the /prov endpoint and the /archive endpoint share
            # the same builder so their shapes can't drift apart.
            from ..prov.json_ld import build_prov_graph
            return await build_prov_graph(session, dossier_id)

    @app.get(
        "/dossiers/{dossier_id}/prov/graph/timeline",
        tags=["prov"],
        summary="PROV graph visualization (user view)",
        description=(
            "Interactive timeline visualization of the dossier's "
            "provenance graph. Honors per-user filtering from "
            "dossier_access (entity types and activity_view). This "
            "endpoint NEVER shows system activities or tasks — it's "
            "the day-to-day business view. For the full record, use "
            "the audit-level /prov or /prov/graph/columns endpoints."
        ),
        response_class=HTMLResponse,
    )
    async def get_prov_graph(
        dossier_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            plugin = registry.get(dossier.workflow)

            # Check access + determine visibility
            access_entry = await check_dossier_access(repo, dossier_id, user, global_access)
            visible_types, activity_view_mode = get_visibility_from_entry(access_entry)

            # Load the graph rowsets via the shared loader, same as
            # the audit-level endpoints. The per-user filtering is
            # applied in-Python below on the loaded rowsets — we
            # don't push it down into the query because the same
            # loader is used by four endpoints and the filter is a
            # timeline-specific concern.
            from ..db.graph_loader import load_dossier_graph_rows
            graph_rows = await load_dossier_graph_rows(session, dossier_id)
            activities = graph_rows.activities
            all_entities = graph_rows.entities

            # Filter entities by access visibility
            if visible_types is not None:
                all_entities = [e for e in all_entities if e.type in visible_types]

            assoc_by_activity = graph_rows.assoc_by_activity
            used_by_activity = graph_rows.used_by_activity

            # Build graph data for D3
            nodes = []
            edges = []
            node_ids = set()

            # Build set of system activity types (client_callable: false)
            system_activity_types = set()
            if plugin:
                for act_def in plugin.workflow.get("activities", []):
                    if act_def.get("client_callable") is False:
                        system_activity_types.add(act_def["name"])

            # Timeline is the user-facing view: always hide system
            # activities AND tasks. No query-param toggle — for the
            # full record, clients use the audit-level endpoints.
            skipped_activity_ids = set()
            for act in activities:
                if act.type in system_activity_types or act.type == "systemAction":
                    skipped_activity_ids.add(act.id)
            all_entities = [e for e in all_entities if e.type != "system:task"]

            # Apply per-user activity_view filtering from dossier_access.
            visible_entity_version_ids = set(e.id for e in all_entities)

            from ._helpers.activity_visibility import parse_activity_view, is_activity_visible
            parsed_view = parse_activity_view(activity_view_mode)

            async def _is_agent_graph(act_id, uid):
                assocs = assoc_by_activity.get(act_id, [])
                return any(a.agent_id == uid for a in assocs)

            async def _used_ids_graph(act_id):
                return set(u.entity_id for u in used_by_activity.get(act_id, []))

            if parsed_view.base != "all" or parsed_view.include:
                for act in activities:
                    if act.id in skipped_activity_ids:
                        continue
                    if not await is_activity_visible(
                        parsed_view,
                        activity_type=act.type,
                        activity_id=act.id,
                        user_id=user.id,
                        visible_entity_ids=visible_entity_version_ids,
                        lookup_is_agent=_is_agent_graph,
                        lookup_used_entity_ids=_used_ids_graph,
                    ):
                        skipped_activity_ids.add(act.id)

            # Hide entities generated by system activities (they're
            # already skipped above; drop their generated entities too
            # so the graph doesn't show orphan nodes).
            system_skipped = set(
                act.id for act in activities if act.type in system_activity_types
            )
            all_entities = [e for e in all_entities if e.generated_by not in system_skipped]

            entity_by_id = {e.id: e for e in all_entities}

            # Add activities as nodes
            order_idx = 0
            for act in activities:
                if act.id in skipped_activity_ids:
                    continue

                act_id = f"act-{act.id}"
                nodes.append({
                    "id": act_id,
                    "label": act.type,
                    "type": "activity",
                    "order": order_idx,
                    "time": act.started_at.isoformat() if act.started_at else "",
                    "detail": f"Activity: {act.type}\nTime: {act.started_at.strftime('%Y-%m-%d %H:%M:%S') if act.started_at else 'n/a'}\nID: {act.id}",
                    "informed_by": str(act.informed_by) if act.informed_by else None,
                })
                node_ids.add(act_id)
                order_idx += 1

                # wasInformedBy edges
                if act.informed_by and str(act.informed_by) not in [str(s) for s in skipped_activity_ids]:
                    informed_str = str(act.informed_by)
                    if informed_str.startswith("urn:"):
                        # Cross-dossier: create a phantom node for the external activity
                        ext_act_id = f"ext-{informed_str}"
                        if ext_act_id not in node_ids:
                            # Extract short label from URI
                            parts = informed_str.split("/")
                            short_label = parts[-1][:12] + "..." if len(parts[-1]) > 12 else parts[-1]
                            nodes.append({
                                "id": ext_act_id,
                                "label": short_label,
                                "type": "external_activity",
                                "order": -1,
                                "time": "",
                                "detail": f"Cross-dossier activity\n{informed_str}",
                                "url": None,
                            })
                            node_ids.add(ext_act_id)
                        edges.append({
                            "source": ext_act_id,
                            "target": act_id,
                            "label": "wasInformedBy",
                            "type": "informed",
                        })
                    else:
                        # Local: edge to existing activity node
                        source_id = f"act-{informed_str}"
                        if source_id in node_ids:
                            edges.append({
                                "source": source_id,
                                "target": act_id,
                                "label": "wasInformedBy",
                                "type": "informed",
                            })

                # wasAssociatedWith edges
                for assoc in assoc_by_activity.get(act.id, []):
                    agent_id = f"agent-{assoc.agent_id}"
                    if agent_id not in node_ids:
                        nodes.append({
                            "id": agent_id,
                            "label": assoc.agent_name or assoc.agent_id,
                            "type": "agent",
                            "detail": f"Agent: {assoc.agent_name}\nType: {assoc.agent_type}\nID: {assoc.agent_id}",
                        })
                        node_ids.add(agent_id)
                    edges.append({
                        "source": agent_id,
                        "target": act_id,
                        "label": f"wasAssociatedWith ({assoc.role})",
                        "type": "associated",
                    })

                # used edges
                for used in used_by_activity.get(act.id, []):
                    entity = entity_by_id.get(used.entity_id)
                    if entity:
                        ent_id = f"ent-{entity.id}"
                        if ent_id not in node_ids:
                            entity_url = f"/dossiers/{dossier_id}/entities/{entity.type}/{entity.entity_id}/{entity.id}"
                            nodes.append({
                                "id": ent_id,
                                "label": entity.content.get("uri", entity.type) if entity.type == "external" and entity.content else entity.type,
                                "type": "entity",
                                "entity_type": entity.type,
                                "logical_id": str(entity.entity_id),
                                "time": entity.created_at.isoformat() if entity.created_at else "",
                                "url": entity_url,
                                "detail": f"Entity: {entity.type}\nLogical ID: {entity.entity_id}\nVersion: {entity.id}\nAttributed to: {entity.attributed_to or 'n/a'}",
                            })
                            node_ids.add(ent_id)
                        edges.append({
                            "source": ent_id,
                            "target": act_id,
                            "label": "used",
                            "type": "used",
                        })

            # Build version ordering per logical entity
            # Group entities by (type, logical_id) to determine version_order
            logical_groups = defaultdict(list)
            for entity in all_entities:
                key = f"{entity.type}:{entity.entity_id}"
                logical_groups[key].append(entity)

            # Sort each group by created_at
            for key in logical_groups:
                logical_groups[key].sort(key=lambda e: e.created_at or datetime.min)

            # Assign version_order and row_key
            entity_version_order = {}
            logical_row_keys = list(logical_groups.keys())
            for entity in all_entities:
                key = f"{entity.type}:{entity.entity_id}"
                group = logical_groups[key]
                version_idx = next(i for i, e in enumerate(group) if e.id == entity.id)
                entity_version_order[str(entity.id)] = {
                    "version_order": version_idx,
                    "row_index": logical_row_keys.index(key),
                    "total_rows": len(logical_row_keys),
                }

            # Add entities and wasGeneratedBy edges
            for entity in all_entities:
                ent_id = f"ent-{entity.id}"
                entity_url = f"/dossiers/{dossier_id}/entities/{entity.type}/{entity.entity_id}/{entity.id}"
                ver_info = entity_version_order.get(str(entity.id), {})

                if ent_id not in node_ids:
                    nodes.append({
                        "id": ent_id,
                        "label": entity.content.get("uri", entity.type) if entity.type == "external" and entity.content else entity.type,
                        "type": "entity",
                        "entity_type": entity.type,
                        "logical_id": str(entity.entity_id),
                        "version_order": ver_info.get("version_order", 0),
                        "row_index": ver_info.get("row_index", 0),
                        "total_rows": ver_info.get("total_rows", 1),
                        "time": entity.created_at.isoformat() if entity.created_at else "",
                        "url": entity_url,
                        "detail": f"Entity: {entity.type}\nLogical ID: {entity.entity_id}\nVersion: {entity.id}\nAttributed to: {entity.attributed_to or 'n/a'}",
                    })
                    node_ids.add(ent_id)
                else:
                    # Update existing node with version info
                    for n in nodes:
                        if n["id"] == ent_id:
                            n["version_order"] = ver_info.get("version_order", 0)
                            n["row_index"] = ver_info.get("row_index", 0)
                            n["total_rows"] = ver_info.get("total_rows", 1)
                            break

                # wasGeneratedBy (skip if generating activity is hidden or entity is external)
                if entity.generated_by and entity.generated_by not in skipped_activity_ids:
                    edges.append({
                        "source": f"act-{entity.generated_by}",
                        "target": ent_id,
                        "label": "wasGeneratedBy",
                        "type": "generated",
                    })

                # wasAttributedTo
                if entity.attributed_to:
                    attr_agent_id = f"agent-{entity.attributed_to}"
                    if attr_agent_id not in node_ids:
                        nodes.append({
                            "id": attr_agent_id,
                            "label": entity.attributed_to,
                            "type": "agent",
                            "detail": f"Agent: {entity.attributed_to}",
                        })
                        node_ids.add(attr_agent_id)
                    edges.append({
                        "source": attr_agent_id,
                        "target": ent_id,
                        "label": "wasAttributedTo",
                        "type": "attributed",
                    })

                # wasDerivedFrom (only if parent is visible)
                if entity.derived_from and entity.derived_from in entity_by_id:
                    parent_id = f"ent-{entity.derived_from}"
                    edges.append({
                        "source": parent_id,
                        "target": ent_id,
                        "label": "wasDerivedFrom",
                        "type": "derived",
                    })

            nodes_json = json.dumps(nodes)
            edges_json = json.dumps(edges)

            html = _build_graph_html(
                dossier_id=str(dossier_id),
                workflow=dossier.workflow,
                nodes_json=nodes_json,
                edges_json=edges_json,
            )

            return HTMLResponse(content=html)

    # Import and register the columns graph
    from .prov_columns import register_columns_graph
    register_columns_graph(
        app, registry, get_user, global_access, global_audit_access
    )

    # Archive endpoint
    from fastapi.responses import Response as RawResponse

    @app.get(
        "/dossiers/{dossier_id}/archive",
        tags=["prov"],
        summary="Dossier archive (PDF, audit view)",
        description=(
            "Audit-level PDF/A archive of the dossier — the full "
            "provenance record plus embedded file attachments, "
            "suitable for long-term preservation and regulatory "
            "submission. Includes a cover page, provenance timeline "
            "(static SVG), entity content, and the raw PROV-JSON as "
            "an embedded attachment. Requires a role in "
            "global_audit_access or the dossier's audit_access list."
        ),
    )
    async def get_dossier_archive(
        dossier_id: UUID,
        user: User = Depends(get_user),
    ):
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            repo = Repository(session)

            dossier = await repo.get_dossier(dossier_id)
            if not dossier:
                raise HTTPException(404, detail="Dossier not found")

            # Audit-level access: full record in the PDF.
            await check_audit_access(
                repo, dossier_id, user, global_audit_access,
            )

            # Build the PROV-JSON document. Same builder the /prov
            # endpoint uses — so the graph shape embedded in the PDF
            # and the graph shape served from /prov can't drift apart.
            from ..prov.json_ld import build_prov_graph
            prov = await build_prov_graph(session, dossier_id)

            from ..archive import generate_archive
            file_storage_root = app.state.config.get("file_service", {}).get("storage_root")
            pdf_bytes = await generate_archive(
                session, dossier_id, dossier, registry, prov,
                file_storage_root=file_storage_root,
            )

            # ``generate_archive`` already returns ``bytes`` (see the
            # ``bytes(pdf.output())`` at the tail of archive.py), but
            # older fpdf2 versions sometimes leaked a bytearray through.
            # The defensive cast is cheap and protects against that
            # regressing silently.
            pdf_body = (
                bytes(pdf_bytes) if isinstance(pdf_bytes, bytearray) else pdf_bytes
            )

            # Audit event: PDF/A export is a full data extraction from
            # the dossier. Highest-priority event for compliance; a
            # dedicated action name so the SIEM can retain and alert
            # on exports separately from ordinary reads.
            from ..observability.audit import emit_dossier_audit
            emit_dossier_audit(
                action="dossier.exported",
                user=user,
                dossier_id=dossier_id,
                outcome="allowed",
                export_format="pdfa3",
                bytes_sent=len(pdf_body),
            )

            # Return the bytes inline — no tempfile, no background
            # cleanup task to forget. Archive PDFs are a few MB at
            # most (cover page + SVG timeline + entity content +
            # embedded PROV-JSON), which is well within what's
            # reasonable to hold in memory through a single response.
            # If archives ever grow into tens of MB, switch to a
            # streaming response with an explicit tempfile cleanup
            # via FastAPI's BackgroundTasks — but until then, the
            # in-memory path is the cleanest ownership story.
            return RawResponse(
                content=pdf_body,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="dossier-'
                        f'{str(dossier_id)[:8]}-archief.pdf"'
                    ),
                },
            )


def _build_graph_html(dossier_id: str, workflow: str, nodes_json: str, edges_json: str) -> str:
    """Render the interactive timeline PROV graph from its Jinja2 template."""
    template = _jinja_env.get_template("prov_timeline.html")
    return template.render(
        dossier_id=dossier_id,
        workflow=workflow,
        nodes_json=nodes_json,
        edges_json=edges_json,
    )

