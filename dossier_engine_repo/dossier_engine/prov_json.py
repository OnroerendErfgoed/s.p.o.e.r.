"""
PROV-JSON document builder + graph data loader.

Two exports live here, both working on the same four rowsets
(activities, entities, associations, used):

* ``load_dossier_graph_rows`` — fetches the four rowsets plus the
  agent lookup. Shared by ``/prov`` (which feeds them into the
  PROV-JSON builder), the static-SVG ``/archive`` (same), the
  interactive ``/prov/graph/columns`` (which runs its own layout
  algorithm), and ``/prov/graph/timeline`` (same, after applying
  per-user access filtering).

* ``build_prov_graph`` — assembles the PROV-JSON document. Calls
  ``load_dossier_graph_rows`` internally; endpoints that want the
  raw rowsets for their own rendering skip this.

Prior to this consolidation, the four endpoints each had their own
slight variation of the same four ``select()`` queries + their own
agent-URI lookup helper. Duplication D1 in the review. Centralising
here means index-choice changes and Bug 14's cross-dossier-used fix
both happen in one spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import (
    ActivityRow, AgentRow, AssociationRow, EntityRow,
    Repository, UsedRow,
)
from .prov_iris import (
    activity_qname, agent_qname, agent_type_value,
    entity_qname, prov_prefixes, prov_type_value,
)


@dataclass
class DossierGraphRows:
    """The four rowsets that make up a dossier's provenance graph,
    plus the agent lookup.

    Ordering:
    * ``activities`` — in insertion order (the repo's default).
    * ``entities``   — by ``created_at`` ascending.
    * ``associations`` and ``used`` — unordered; callers that care
      about order should re-sort.

    Index helpers (`by_activity`, `entity_by_id`, `agent_rows`) are
    pre-computed so callers that need them don't re-index. Small
    extra cost for callers that don't, negligible at dossier scale.
    """
    activities: list[ActivityRow]
    entities: list[EntityRow]
    associations: list[AssociationRow]
    used: list[UsedRow]
    agent_rows: dict[str, AgentRow]

    # Derived indexes — filled in by the loader after the selects.
    assoc_by_activity: dict[UUID, list[AssociationRow]] = field(
        default_factory=dict
    )
    used_by_activity: dict[UUID, list[UsedRow]] = field(
        default_factory=dict
    )
    entity_by_id: dict[UUID, EntityRow] = field(default_factory=dict)


async def load_dossier_graph_rows(
    session: AsyncSession, dossier_id: UUID,
) -> DossierGraphRows:
    """Load every row needed to render or export a dossier's PROV graph.

    Four queries + one agent-lookup query — same set every rendering
    endpoint needs. Returns a ``DossierGraphRows`` with pre-built
    indexes so callers can walk the structure without re-indexing.

    This is the "audit" load: all activities, all entities, no
    per-user filter. Endpoints that want a user-visible subset (the
    timeline graph) filter the result by entity type / activity
    visibility rather than asking the DB for a reduced rowset —
    keeps the loader simple and the filter concern colocated with
    the access-check call that defines it.
    """
    repo = Repository(session)

    activities = await repo.get_activities_for_dossier(dossier_id)

    entities_result = await session.execute(
        select(EntityRow)
        .where(EntityRow.dossier_id == dossier_id)
        .order_by(EntityRow.created_at)
    )
    entities = list(entities_result.scalars().all())

    activity_ids = [a.id for a in activities]
    if activity_ids:
        assoc_result = await session.execute(
            select(AssociationRow)
            .where(AssociationRow.activity_id.in_(activity_ids))
        )
        associations = list(assoc_result.scalars().all())
        used_result = await session.execute(
            select(UsedRow).where(UsedRow.activity_id.in_(activity_ids))
        )
        used = list(used_result.scalars().all())
    else:
        associations = []
        used = []

    # Indexes.
    assoc_by_activity: dict[UUID, list[AssociationRow]] = {}
    for a in associations:
        assoc_by_activity.setdefault(a.activity_id, []).append(a)

    used_by_activity: dict[UUID, list[UsedRow]] = {}
    for u in used:
        used_by_activity.setdefault(u.activity_id, []).append(u)

    entity_by_id = {e.id: e for e in entities}

    # Agent URI lookup. AssociationRow carries agent_id but not the
    # canonical URI — that lives on AgentRow. Entities referenced
    # via wasAttributedTo need the same lookup.
    agent_ids: set[str] = {
        a.agent_id for assocs in assoc_by_activity.values() for a in assocs
    }
    agent_ids |= {e.attributed_to for e in entities if e.attributed_to}
    agent_rows: dict[str, AgentRow] = {}
    if agent_ids:
        agent_result = await session.execute(
            select(AgentRow).where(AgentRow.id.in_(agent_ids))
        )
        agent_rows = {a.id: a for a in agent_result.scalars().all()}

    return DossierGraphRows(
        activities=activities,
        entities=entities,
        associations=associations,
        used=used,
        agent_rows=agent_rows,
        assoc_by_activity=assoc_by_activity,
        used_by_activity=used_by_activity,
        entity_by_id=entity_by_id,
    )


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

