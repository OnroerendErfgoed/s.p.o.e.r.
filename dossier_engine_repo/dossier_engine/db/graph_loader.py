"""
Dossier graph row loader.

One call that fetches every row needed to reason about a dossier's
provenance graph — activities, entities, associations, used — plus the
agent lookup, with pre-built per-activity indexes so callers don't
have to re-bucket. Extracted into ``db/`` in Round 30.5 from
``prov_json.py`` where it was originally colocated with the PROV-JSON
document builder.

The move: the loader is a generic DB concern (just rows + indexes) and
has five callers across three route modules plus one JSON builder. The
original home (``prov_json.py``) made unrelated modules reach into the
PROV-JSON file to get their rowsets — awkward by name, and fragile if
``prov_json.py`` ever gets reshaped.  The new home makes the loader
discoverable next to ``db/models.py`` and ``db/session.py``, alongside
the rest of the DB-layer abstractions.

Callers (as of Round 30.5):

* ``routes/prov.py`` — ``/prov`` endpoint; feeds rows into
  ``prov_json.build_prov_graph``.
* ``routes/prov_columns.py`` — ``/prov/graph/columns`` and
  ``/prov/graph/timeline``; runs its own layout over the rows.
* ``routes/dossiers.py::get_dossier`` — uses the per-activity indexes
  to avoid an N+1 in the visibility loop (Bug 9 fix, Round 29).
* ``prov_json.build_prov_graph`` — assembles the PROV-JSON document
  from the loaded rows.

This is the "audit" load: all activities, all entities, no per-user
filter. Endpoints that want a user-visible subset filter the result by
entity type / activity visibility rather than asking the DB for a
reduced rowset — keeps the loader simple and the filter concern
colocated with the access-check call that defines it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ActivityRow, AgentRow, AssociationRow, EntityRow,
    Repository, UsedRow,
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

    Index helpers (``assoc_by_activity``, ``used_by_activity``,
    ``entity_by_id``) are pre-computed so callers that need them
    don't re-index. Small extra cost for callers that don't,
    negligible at dossier scale.
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
    timeline graph, the dossier-detail activity timeline) filter the
    result by entity type / activity visibility rather than asking
    the DB for a reduced rowset — keeps the loader simple and the
    filter concern colocated with the access-check call that defines
    it.
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
