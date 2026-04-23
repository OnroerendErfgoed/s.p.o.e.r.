"""
Repository — the single object the rest of the codebase uses to
interact with the database.

All writes are INSERTs (append-only). Session-scoped caches reduce
redundant reads within a single request: ``_ensured_agents``,
``_activities_cache``, ``_dossier_cache``. Caches live on the
Repository instance and die with the session.

The class is one file on purpose despite its length — methods
cross-reference each other heavily and splitting by table would spread
one logical unit across multiple files without a legibility win. See
the Round 34 refactor plan (``docs/refactor-plan-round34.md``) for
the discussion.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Text,
    distinct, func, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from .rows import (
    Base, UUID_DB, JSON_DB,
    DossierRow, ActivityRow, AssociationRow, EntityRow,
    UsedRow, RelationRow, AgentRow, DomainRelationRow,
)


# Repository
# =====================================================================

class Repository:
    """Database operations. All writes are INSERTs (append-only)."""

    def __init__(self, session: AsyncSession):
        self.session = session
        # Session-scoped cache: agents that have already been ensured in this
        # session. Lets `ensure_agent` short-circuit after the first call per
        # agent_id, avoiding redundant SELECT+UPDATE pairs. Cleared when the
        # session ends (next request gets a fresh Repository).
        self._ensured_agents: set[str] = set()
        # Session-scoped cache of activities per dossier. `derive_status`,
        # `validate_workflow_rules`, `compute_eligible_activities`, and the
        # post-activity hook all call `get_activities_for_dossier` within a
        # single `execute_activity` — without this cache they each issue a
        # separate SELECT. The cache is invalidated in-place when
        # `create_activity` adds a new row for the same dossier, so it stays
        # consistent with the session's view. Keyed by dossier_id.
        self._activities_cache: dict[UUID, list] = {}
        # Session-scoped cache of dossier rows. Avoids redundant `SELECT
        # FROM dossiers WHERE id = ?` when the same dossier is fetched by
        # multiple code paths in a single request.
        self._dossier_cache: dict[UUID, Optional["DossierRow"]] = {}

    # --- Dossier ---

    async def get_dossier(self, dossier_id: UUID) -> Optional[DossierRow]:
        if dossier_id in self._dossier_cache:
            return self._dossier_cache[dossier_id]
        result = await self.session.get(DossierRow, dossier_id)
        self._dossier_cache[dossier_id] = result
        return result

    async def get_dossier_for_update(self, dossier_id: UUID) -> Optional[DossierRow]:
        """Fetch the dossier row with a row-level exclusive lock.

        Issued as `SELECT ... FOR UPDATE` so concurrent activities
        against the same dossier serialize. The lock is held until
        the enclosing transaction commits or rolls back. Other
        dossiers are unaffected — the lock is scoped to one row.

        The session cache is bypassed: if the row was loaded earlier
        in this transaction without the lock, SQLAlchemy's identity
        map would return it unchanged. Forcing a fresh query with
        FOR UPDATE guarantees the lock is actually acquired.

        Used by the activity pipeline as the optimistic-concurrency
        replacement (see pipeline/preconditions.py::ensure_dossier).
        """
        from sqlalchemy import select
        # Invalidate any previously loaded version so FOR UPDATE
        # actually hits the database.
        self._dossier_cache.pop(dossier_id, None)
        stmt = select(DossierRow).where(DossierRow.id == dossier_id).with_for_update()
        result = (await self.session.execute(stmt)).scalar_one_or_none()
        self._dossier_cache[dossier_id] = result
        return result

    async def create_dossier(self, dossier_id: UUID, workflow: str) -> DossierRow:
        row = DossierRow(id=dossier_id, workflow=workflow)
        self.session.add(row)
        # Cache the newly-created row so a subsequent get_dossier in the
        # same session hits the cache instead of issuing a SELECT.
        self._dossier_cache[dossier_id] = row
        return row

    # --- Activity ---

    async def get_activity(self, activity_id: UUID) -> Optional[ActivityRow]:
        """Return an activity row by id, or None if not found.

        Scoping contract: activity-id-only lookup; does NOT filter by
        dossier. Callers that reach this helper via a PROV traversal
        (``informed_by_activity_id``, ``generated_by``, etc.) must
        check the returned row's ``dossier_id`` against their own
        scope if the traversal could cross dossier boundaries.
        """
        return await self.session.get(ActivityRow, activity_id)

    async def get_activities_for_dossier(self, dossier_id: UUID) -> list[ActivityRow]:
        cached = self._activities_cache.get(dossier_id)
        if cached is not None:
            return cached
        result = await self.session.execute(
            select(ActivityRow)
            .where(ActivityRow.dossier_id == dossier_id)
            .order_by(ActivityRow.started_at)
        )
        rows = list(result.scalars().all())
        self._activities_cache[dossier_id] = rows
        return rows

    async def create_activity(
        self,
        activity_id: UUID,
        dossier_id: UUID,
        type: str,
        started_at: datetime,
        ended_at: datetime | None = None,
        informed_by: str | None = None,
        computed_status: str | None = None,
    ) -> ActivityRow:
        # Classify `informed_by` once, here, rather than at every read
        # site. Callers pass a single string; we map it to the right
        # column. Rules:
        #   - None           → both columns NULL (no informant)
        #   - UUID-shaped    → informed_by_activity_id (same-dossier ref)
        #   - anything else  → informed_by_uri (cross-dossier IRI)
        # Strings that look like UUIDs but are actually URIs won't
        # happen in practice — IRIs always contain slashes.
        informed_by_activity_id = None
        informed_by_uri = None
        if informed_by is not None:
            try:
                informed_by_activity_id = UUID(informed_by)
            except (ValueError, AttributeError):
                informed_by_uri = informed_by

        row = ActivityRow(
            id=activity_id,
            dossier_id=dossier_id,
            type=type,
            started_at=started_at,
            ended_at=ended_at,
            informed_by_activity_id=informed_by_activity_id,
            informed_by_uri=informed_by_uri,
            computed_status=computed_status,
        )
        self.session.add(row)
        # Keep the activities cache consistent with the session's view.
        # Append-only: insertion order matches started_at order for
        # activities created within a single request.
        cached = self._activities_cache.get(dossier_id)
        if cached is not None:
            cached.append(row)
        return row

    # --- Association ---

    async def create_association(
        self,
        association_id: UUID,
        activity_id: UUID,
        agent_id: str,
        agent_name: str | None,
        agent_type: str | None,
        role: str,
    ) -> AssociationRow:
        row = AssociationRow(
            id=association_id,
            activity_id=activity_id,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_type=agent_type,
            role=role,
        )
        self.session.add(row)
        return row

    # --- Entity ---

    async def get_entity(self, version_id: UUID) -> Optional[EntityRow]:
        return await self.session.get(EntityRow, version_id)

    async def get_singleton_entity(
        self, dossier_id: UUID, entity_type: str
    ) -> Optional[EntityRow]:
        """Return the latest (most recently created) entity of `entity_type`
        in the dossier. Intended for singleton-cardinality types — callers
        expecting a unique entity per type per dossier.

        NOTE: this method does NOT enforce the singleton invariant itself;
        cardinality enforcement happens at the engine layer via
        `plugin.cardinality_of(entity_type)`. See phase 1b."""
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .order_by(EntityRow.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_entity_by_id(
        self, dossier_id: UUID, entity_id: UUID
    ) -> Optional[EntityRow]:
        """Return the newest version row for a specific logical entity_id,
        or None if no versions of this entity exist in the dossier."""
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.entity_id == entity_id)
            .order_by(EntityRow.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_all_latest_entities(self, dossier_id: UUID) -> list[EntityRow]:
        # Get the latest version of each logical entity
        subq = (
            select(
                EntityRow.entity_id,
                func.max(EntityRow.created_at).label("max_created")
            )
            .where(EntityRow.dossier_id == dossier_id)
            .group_by(EntityRow.entity_id)
            .subquery()
        )
        result = await self.session.execute(
            select(EntityRow)
            .join(subq, (EntityRow.entity_id == subq.c.entity_id) & (EntityRow.created_at == subq.c.max_created))
            .where(EntityRow.dossier_id == dossier_id)
        )
        return list(result.scalars().all())

    async def get_entities_by_type(self, dossier_id: UUID, entity_type: str) -> list[EntityRow]:
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .order_by(EntityRow.created_at)
        )
        return list(result.scalars().all())

    async def get_entities_by_type_latest(
        self, dossier_id: UUID, entity_type: str
    ) -> list[EntityRow]:
        """Return the latest version of each distinct logical entity of this
        type in the dossier. For singleton types the list has at most one
        element. For multi-cardinality types, one element per entity_id."""
        # Subquery: max(created_at) per entity_id for this type
        subq = (
            select(
                EntityRow.entity_id,
                func.max(EntityRow.created_at).label("max_created"),
            )
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .group_by(EntityRow.entity_id)
            .subquery()
        )
        result = await self.session.execute(
            select(EntityRow)
            .join(
                subq,
                (EntityRow.entity_id == subq.c.entity_id)
                & (EntityRow.created_at == subq.c.max_created),
            )
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
            .order_by(EntityRow.created_at)
        )
        return list(result.scalars().all())

    async def get_entity_versions(self, dossier_id: UUID, entity_id: UUID) -> list[EntityRow]:
        """Get all versions of a specific logical entity, ordered by creation time."""
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.entity_id == entity_id)
            .order_by(EntityRow.created_at)
        )
        return list(result.scalars().all())

    async def entity_type_exists(self, dossier_id: UUID, entity_type: str) -> bool:
        result = await self.session.execute(
            select(func.count())
            .select_from(EntityRow)
            .where(EntityRow.dossier_id == dossier_id)
            .where(EntityRow.type == entity_type)
        )
        return result.scalar() > 0

    async def create_entity(
        self,
        version_id: UUID,
        entity_id: UUID,
        dossier_id: UUID,
        type: str,
        generated_by: UUID | None = None,
        content: dict | None = None,
        derived_from: UUID | None = None,
        attributed_to: str | None = None,
        schema_version: str | None = None,
    ) -> EntityRow:
        row = EntityRow(
            id=version_id,
            entity_id=entity_id,
            dossier_id=dossier_id,
            type=type,
            generated_by=generated_by,
            content=content,
            derived_from=derived_from,
            attributed_to=attributed_to,
            schema_version=schema_version,
        )
        self.session.add(row)
        return row

    async def ensure_external_entity(self, dossier_id: UUID, uri: str) -> EntityRow:
        """Ensure an external entity exists for this URI in this dossier. Idempotent."""
        # Deterministic UUID from URI + dossier_id so the same URI doesn't create duplicates
        entity_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{dossier_id}:{uri}")
        version_id = entity_id  # external entities have one "version"
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.id == version_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing
        return await self.create_entity(
            version_id=version_id,
            entity_id=entity_id,
            dossier_id=dossier_id,
            type="external",
            generated_by=None,
            content={"uri": uri},
        )

    async def tombstone_entity_versions(
        self, version_ids: list[UUID], tombstone_activity_id: UUID
    ) -> None:
        """Mark the given entity versions as tombstoned: set content=NULL
        and stamp tombstoned_by with the activity that performed the
        deletion. Per the deletion-scope decision (option a), only the
        content blob is nulled — the row itself, derivation edges,
        used/relations references, schema_version, and all PROV linkage
        survive intact. The audit skeleton stays whole; the data is gone."""
        if not version_ids:
            return
        await self.session.execute(
            update(EntityRow)
            .where(EntityRow.id.in_(version_ids))
            .values(content=None, tombstoned_by=tombstone_activity_id)
        )

    # --- Used ---

    async def create_used(self, activity_id: UUID, entity_version_id: UUID):
        row = UsedRow(activity_id=activity_id, entity_id=entity_version_id)
        self.session.add(row)

    async def get_used_entity_ids_for_activity(self, activity_id: UUID) -> set[UUID]:
        """Get all entity version IDs used by an activity."""
        result = await self.session.execute(
            select(UsedRow.entity_id).where(UsedRow.activity_id == activity_id)
        )
        return {row[0] for row in result.all()}

    async def get_entities_generated_by_activity(
        self, activity_id: UUID
    ) -> list[EntityRow]:
        """Return all entities whose `generated_by` points at this activity.

        Scoping contract: this helper queries by ``activity_id`` alone
        and does NOT filter by dossier. Callers are responsible for
        ensuring the activity belongs to the dossier they intend to
        operate on. Every PROV edge is created within a single dossier
        scope, so in normal operation this is free — but code that
        *traverses* activity IDs from untrusted inputs (e.g. the
        lineage walker resolving ``informed_by_activity_id``, or any
        future caller walking PROV graphs from client-supplied refs)
        must verify dossier scope separately before calling this.
        """
        result = await self.session.execute(
            select(EntityRow)
            .where(EntityRow.generated_by == activity_id)
            .order_by(EntityRow.created_at)
        )
        return list(result.scalars().all())

    async def get_used_entities_for_activity(
        self, activity_id: UUID
    ) -> list[EntityRow]:
        """Return the full EntityRow objects used by an activity.

        Scoping contract: same as
        ``get_entities_generated_by_activity`` above — this query is
        activity-id-only, callers must ensure dossier scope themselves.
        """
        result = await self.session.execute(
            select(EntityRow)
            .join(UsedRow, UsedRow.entity_id == EntityRow.id)
            .where(UsedRow.activity_id == activity_id)
        )
        return list(result.scalars().all())

    # --- Relations (generic activity→entity edges beyond used/generated) ---

    async def create_relation(
        self,
        activity_id: UUID,
        entity_version_id: UUID,
        relation_type: str,
    ):
        """Record an activity→entity relation under a named type. Idempotent
        at the (activity, entity, type) level: inserting the same triple
        twice is a no-op (caller should avoid it but we don't enforce it
        here beyond the PK constraint)."""
        row = RelationRow(
            activity_id=activity_id,
            entity_id=entity_version_id,
            relation_type=relation_type,
        )
        self.session.add(row)

    async def get_relations_for_activity(
        self, activity_id: UUID
    ) -> list[RelationRow]:
        """Return every relation row attached to this activity."""
        result = await self.session.execute(
            select(RelationRow).where(RelationRow.activity_id == activity_id)
        )
        return list(result.scalars().all())

    # --- Domain relations (entity↔entity/URI semantic links) ---

    async def create_domain_relation(
        self,
        dossier_id: UUID,
        relation_type: str,
        from_ref: str,
        to_ref: str,
        created_by_activity_id: UUID,
    ) -> DomainRelationRow:
        """Create a new domain relation. Idempotent: if an active
        (non-superseded) relation with the same (type, from, to) already
        exists in the dossier, the insert is skipped and the existing
        row is returned."""
        # Check for existing active duplicate.
        existing = await self.session.execute(
            select(DomainRelationRow).where(
                DomainRelationRow.dossier_id == dossier_id,
                DomainRelationRow.relation_type == relation_type,
                DomainRelationRow.from_ref == from_ref,
                DomainRelationRow.to_ref == to_ref,
                DomainRelationRow.superseded_at.is_(None),
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            return row
        row = DomainRelationRow(
            dossier_id=dossier_id,
            relation_type=relation_type,
            from_ref=from_ref,
            to_ref=to_ref,
            created_by_activity_id=created_by_activity_id,
        )
        self.session.add(row)
        return row

    async def supersede_domain_relation(
        self,
        dossier_id: UUID,
        relation_type: str,
        from_ref: str,
        to_ref: str,
        superseded_by_activity_id: UUID,
    ) -> bool:
        """Mark an active domain relation as superseded. Returns True
        if a matching active relation was found and superseded, False
        if no match (idempotent — removing something already gone is
        a no-op)."""
        result = await self.session.execute(
            select(DomainRelationRow).where(
                DomainRelationRow.dossier_id == dossier_id,
                DomainRelationRow.relation_type == relation_type,
                DomainRelationRow.from_ref == from_ref,
                DomainRelationRow.to_ref == to_ref,
                DomainRelationRow.superseded_at.is_(None),
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            return False
        row.superseded_by_activity_id = superseded_by_activity_id
        row.superseded_at = datetime.now(timezone.utc)
        return True

    async def get_active_domain_relations(
        self, dossier_id: UUID,
    ) -> list[DomainRelationRow]:
        """Return all non-superseded domain relations for a dossier."""
        result = await self.session.execute(
            select(DomainRelationRow)
            .where(DomainRelationRow.dossier_id == dossier_id)
            .where(DomainRelationRow.superseded_at.is_(None))
            .order_by(DomainRelationRow.created_at)
        )
        return list(result.scalars().all())

    # --- Agent ---

    async def ensure_agent(self, agent_id: str, agent_type: str, name: str | None, properties: dict | None, uri: str | None = None):
        # Fast path: already ensured this session, nothing to do. This is
        # safe because agents are effectively immutable for the purposes of
        # a single activity execution — name/properties changes are rare
        # and not functional.
        if agent_id in self._ensured_agents:
            return
        existing = await self.session.get(AgentRow, agent_id)
        if existing:
            # Only write if something actually changed. Bumping `updated_at`
            # on every call was pure overhead — the field has no semantic
            # meaning for the engine.
            if existing.name != name or existing.properties != properties or existing.uri != uri:
                existing.name = name
                existing.properties = properties
                if uri is not None:
                    existing.uri = uri
                existing.updated_at = datetime.now(timezone.utc)
        else:
            row = AgentRow(id=agent_id, type=agent_type, name=name, uri=uri, properties=properties)
            self.session.add(row)
        self._ensured_agents.add(agent_id)
