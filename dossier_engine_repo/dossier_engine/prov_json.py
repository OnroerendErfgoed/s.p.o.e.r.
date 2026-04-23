"""
PROV-JSON document builder.

Assembles the PROV-JSON document from a dossier's graph rows. The
row-loading concern (``load_dossier_graph_rows`` + ``DossierGraphRows``)
moved to ``db/graph_loader.py`` in Round 30.5 — other endpoints
(``routes/prov.py``, ``routes/prov_columns.py``,
``routes/dossiers.py``) need the same rowsets without the PROV-JSON
document-building, so the loader lives in a neutral home. ``build_prov_graph``
calls it internally and adds the PROV-JSON structure on top.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .db.graph_loader import DossierGraphRows, load_dossier_graph_rows
from .db.models import AgentRow, EntityRow
from .prov_iris import (
    activity_qname, agent_qname, agent_type_value,
    entity_qname, prov_prefixes, prov_type_value,
)


# ``DossierGraphRows`` and ``load_dossier_graph_rows`` moved to
# ``db/graph_loader.py`` in Round 30.5. They're imported above and
# re-exported here under their original names for any caller that
# used the historical ``from dossier_engine.prov_json import
# load_dossier_graph_rows`` path.  New callers should import from
# ``dossier_engine.db.graph_loader`` directly.
__all__ = [
    "DossierGraphRows",
    "load_dossier_graph_rows",
    "build_prov_graph",
]


def agent_key_resolver(agent_rows: dict[str, AgentRow]):
    """Build the agent-key resolver closure: canonical URI when
    available, else dossier-scoped QName.

    Returned as a closure so callers can pass it around without
    re-binding ``agent_rows`` every call site. Used by the PROV-JSON
    builder and by graph renderers that want the same
    URI-preferred-if-present policy."""
    def _key(agent_id: str) -> str:
        row = agent_rows.get(agent_id)
        if row and row.uri:
            return row.uri
        return agent_qname(agent_id)
    return _key


def entity_key_resolver():
    """Entity-key resolver: external entities use their declared URI
    as the key; local entities use the dossier-scoped version IRI.

    No closure state — pure function — but returned as a callable
    so calling sites look symmetric with ``agent_key_resolver``."""
    def _key(entity: EntityRow) -> str:
        if entity.type == "external" and entity.content:
            ext_uri = entity.content.get("uri")
            if ext_uri:
                return ext_uri
        return entity_qname(entity.type, entity.entity_id, entity.id)
    return _key


async def build_prov_graph(
    session: AsyncSession, dossier_id: UUID,
) -> dict:
    """Build a complete PROV-JSON document for the given dossier.

    This is the audit-view shape: no per-user filtering, every
    activity/entity/association included. Callers that want a
    filtered view (e.g. a user-facing timeline) should filter the
    result rather than add filtering parameters here — the concern
    of *what to include* is separate from *how to serialise it*.

    Returns a dict with the standard PROV-JSON top-level keys
    (``entity``, ``activity``, ``agent``, ``used``,
    ``wasGeneratedBy``, ``wasAssociatedWith``, ``wasAttributedTo``,
    ``wasDerivedFrom``, ``wasInformedBy``, ``actedOnBehalfOf``,
    plus ``prefix``). Empty sections are omitted so consumers can
    distinguish "no facts of this kind" from "facts intentionally
    filtered out."
    """
    rows = await load_dossier_graph_rows(session, dossier_id)
    _agent_key = agent_key_resolver(rows.agent_rows)
    _entity_key = entity_key_resolver()

    prov: dict = {
        "prefix": prov_prefixes(dossier_id),
        "entity": {},
        "activity": {},
        "agent": {},
        "wasGeneratedBy": {},
        "used": {},
        "wasAssociatedWith": {},
        "wasAttributedTo": {},
        "wasDerivedFrom": {},
        "wasInformedBy": {},
        "actedOnBehalfOf": {},
    }

    # Agents (deduplicated by canonical key).
    agents_seen: set[str] = set()
    for assocs in rows.assoc_by_activity.values():
        for assoc in assocs:
            akey = _agent_key(assoc.agent_id)
            if akey not in agents_seen:
                agents_seen.add(akey)
                agent_data = {
                    "prov:label": assoc.agent_name or assoc.agent_id,
                    "prov:type": agent_type_value(
                        assoc.agent_type or "prov:Person"
                    ),
                }
                # When the URI is the key, surface the internal id so
                # downstream tools can still correlate back to the
                # persistence layer.
                agent_row = rows.agent_rows.get(assoc.agent_id)
                if agent_row and agent_row.uri:
                    agent_data["oe:agentId"] = assoc.agent_id
                prov["agent"][akey] = agent_data

    # Activities + their outgoing edges.
    for act in rows.activities:
        act_key = activity_qname(act.id)
        act_data: dict = {"prov:type": prov_type_value(act.type)}
        if act.started_at:
            act_data["prov:startedAtTime"] = {
                "$": act.started_at.isoformat(), "type": "xsd:dateTime",
            }
        if act.ended_at:
            act_data["prov:endedAtTime"] = {
                "$": act.ended_at.isoformat(), "type": "xsd:dateTime",
            }
        prov["activity"][act_key] = act_data

        for assoc in rows.assoc_by_activity.get(act.id, []):
            prov["wasAssociatedWith"][f"_:assoc_{assoc.id}"] = {
                "prov:activity": act_key,
                "prov:agent": _agent_key(assoc.agent_id),
                "prov:hadRole": {"$": assoc.role, "type": "xsd:string"},
            }

        # ``used`` edges — if the referenced entity is missing from
        # our in-dossier rowset, the ref is cross-dossier and gets
        # dropped here. That matches the existing behaviour; Bug 14
        # proposes preserving these as external nodes instead, and
        # when that lands it's a one-line change right here.
        for used in rows.used_by_activity.get(act.id, []):
            entity = rows.entity_by_id.get(used.entity_id)
            if entity:
                prov["used"][f"_:used_{act.id}_{entity.id}"] = {
                    "prov:activity": act_key,
                    "prov:entity": _entity_key(entity),
                }

        # ``wasInformedBy`` — two flavours. Local references resolve
        # to a QName; cross-dossier ones are full IRIs and used verbatim.
        if act.informed_by_uri is not None:
            prov["wasInformedBy"][f"_:informed_{act.id}"] = {
                "prov:informedActivity": act_key,
                "prov:informantActivity": act.informed_by_uri,
            }
        elif act.informed_by_activity_id is not None:
            prov["wasInformedBy"][f"_:informed_{act.id}"] = {
                "prov:informedActivity": act_key,
                "prov:informantActivity": activity_qname(
                    act.informed_by_activity_id
                ),
            }

    # Entities + their outgoing edges.
    for entity in rows.entities:
        ent_key = _entity_key(entity)
        entity_data: dict = {
            "prov:type": prov_type_value(entity.type),
            "oe:entityId": str(entity.entity_id),
            "oe:versionId": str(entity.id),
        }
        if entity.created_at:
            entity_data["prov:generatedAtTime"] = {
                "$": entity.created_at.isoformat(), "type": "xsd:dateTime",
            }
        prov["entity"][ent_key] = entity_data

        if entity.generated_by:
            prov["wasGeneratedBy"][f"_:gen_{entity.id}"] = {
                "prov:entity": ent_key,
                "prov:activity": activity_qname(entity.generated_by),
            }
        if entity.attributed_to:
            prov["wasAttributedTo"][f"_:attr_{entity.id}"] = {
                "prov:entity": ent_key,
                "prov:agent": _agent_key(entity.attributed_to),
            }
        if entity.derived_from:
            parent = rows.entity_by_id.get(entity.derived_from)
            if parent:
                prov["wasDerivedFrom"][f"_:deriv_{entity.id}"] = {
                    "prov:generatedEntity": ent_key,
                    "prov:usedEntity": _entity_key(parent),
                }

    # Strip empty sections so consumers can distinguish "none of
    # these facts exist" from "these facts were filtered out."
    return {k: v for k, v in prov.items() if v}

