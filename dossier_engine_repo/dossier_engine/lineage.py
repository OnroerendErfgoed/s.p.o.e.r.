"""
Activity-graph traversal for sideways entity lookup.

Given an entity, find a related entity of a different type by walking
the PROV activity graph backwards. Unlike a pure derivation walk
(which follows `derived_from` and `used` edges from one version to
its parents), this walker inspects every activity it visits in full:
both the entities the activity used AND the entities it co-generated,
plus the activity it was informed by.

The canonical use case is anchoring a scheduled task to an entity
that the triggering activity didn't touch directly. For example,
`tekenBeslissing` uses a `beslissing` but not the `aanvraag` the
beslissing was made about. The handler that runs afterwards still
needs the aanvraag's `entity_id` to anchor a `trekAanvraagIn`
scheduled task — walking from the beslissing through its generating
activity (`doeVoorstelBeslissing`) finds the aanvraag in that
activity's used block.

Semantics:

* Starts at `start_entity.generated_by` and walks backwards through
  `used` entities' generating activities AND through `informed_by`.
* At each visited activity, checks both `generated` and `used` for
  an entity of `target_type`.
* Returns the match if exactly one distinct `entity_id` of that type
  appears at a visited activity.
* **Raises `LineageAmbiguous`** (Bug 54) if multiple distinct
  ``entity_id`` values of the target type appear at one activity.
  Ambiguity is a structural/data signal, not a normal "no result"
  outcome — the caller must decide whether to log-and-skip or
  propagate. Before Bug 54's fix, the walker returned ``None`` for
  both ambiguity and not-found, making the two cases indistinguishable
  at the callsite (and invisible to operators).
* Returns ``None`` in all "no result" cases: start entity is a root
  (no generating activity to walk from), frontier exhausted without
  finding the target type, ``max_hops`` budget exhausted, or
  ``start_entity.generated_by`` is ``None``. These three outcomes all
  mean the same thing to a caller — "no anchor available for this
  task" — so they share a return value. If a caller needs to
  distinguish them, extend the API with a reason code rather than
  asking callers to peek at internals.
* Returns the start entity itself if ``start_entity.type == target_type``
  (trivial case, no walk needed).

Frontier management (Bug 53):

* The frontier is a **set** — activities appended through multiple
  paths in one hop are deduplicated, bounding memory even for
  high-fan-in PROV graphs (many activities using entities generated
  by a common ancestor). Already-visited activities are skipped at
  append time, so frontier growth is bounded by "activities not yet
  visited" rather than by "paths taken through the graph."

**Intra-dossier by construction.** The walker refuses to traverse
any activity whose `dossier_id` differs from the `dossier_id`
argument. In normal operation this never triggers — PROV edges
are created within a single dossier scope — but the check is
defense-in-depth against data integrity violations or PROV
manipulation that would otherwise let the walker leak a
confirmation signal about another dossier's activity graph.
Cross-dossier references (`informed_by_uri`) are separately
not walkable from within one repository scope.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from .db.models import EntityRow, Repository


class LineageAmbiguous(Exception):
    """Raised by :func:`find_related_entity` when a visited activity
    touches more than one distinct ``entity_id`` of the target type.

    Carries the list of candidate ``entity_id`` values and the
    ``activity_id`` at which the ambiguity was detected, so callers
    (or operators triaging a log line) can identify which data
    structure caused the ambiguity.

    Before Bug 54 (Round 25), ambiguity and "no match found" both
    returned ``None``, making the two indistinguishable at callsites.
    This exception exists so callers can decide: a ``trekAanvraagIn``
    task builder might log-and-skip (producing an unanchored task),
    while a stricter caller might propagate to fail the activity.
    """

    def __init__(
        self,
        *,
        activity_id: UUID,
        target_type: str,
        candidate_entity_ids: list[UUID],
    ) -> None:
        self.activity_id = activity_id
        self.target_type = target_type
        self.candidate_entity_ids = candidate_entity_ids
        super().__init__(
            f"Lineage walk found {len(candidate_entity_ids)} distinct "
            f"entities of type {target_type!r} at activity "
            f"{activity_id}: {candidate_entity_ids}"
        )


async def find_related_entity(
    repo: Repository,
    dossier_id: UUID,
    start_entity: EntityRow,
    target_type: str,
    *,
    max_hops: int = 10,
) -> Optional[EntityRow]:
    """Find an entity of `target_type` related to `start_entity` by
    walking the activity graph backwards. See module docstring for
    semantics.

    :raises LineageAmbiguous: if a visited activity touches more than
        one distinct ``entity_id`` of the target type. See the
        exception's docstring for context.
    """
    if start_entity.type == target_type:
        return start_entity

    if start_entity.generated_by is None:
        # External or root entity — no activity to walk from.
        return None

    visited_activities: set[UUID] = set()
    frontier: set[UUID] = {start_entity.generated_by}

    for _ in range(max_hops):
        if not frontier:
            return None

        # Bug 53: next_frontier is a set, and we only add activities
        # we haven't already visited. This bounds frontier growth to
        # "activities in this dossier we haven't processed yet" —
        # in the worst case the count of activities in the dossier,
        # not the count of paths through the graph. Without this,
        # high-fan-in graphs (e.g. 50 activities using an entity
        # generated by activity A) put A in next_frontier 50 times
        # on one hop; further iterations compound.
        next_frontier: set[UUID] = set()
        for activity_id in frontier:
            if activity_id in visited_activities:
                continue
            visited_activities.add(activity_id)

            # Defense-in-depth: refuse to traverse activities from a
            # different dossier. In normal operation the walker never
            # encounters one — entity and activity rows are always
            # created within a single dossier scope — but if a data
            # integrity violation, PROV manipulation, or future refactor
            # bug ever produced a cross-dossier edge, the walker would
            # traverse into it and (at best) waste queries checking
            # entities we'd never return, (at worst) leak a confirmation
            # signal about another dossier's activity graph. Failing
            # closed at the activity level — not just at the final
            # ``get_latest_entity_by_id(dossier_id, ...)`` scope check
            # (line below) — keeps the walker intra-dossier by
            # construction, not by post-hoc filter.
            #
            # Concretely: ``get_entities_generated_by_activity`` and
            # ``get_used_entities_for_activity`` are activity-id-only
            # queries (see their docstrings — "caller is responsible
            # for scoping to a known dossier"). This is the caller
            # that the docstring contract is about.
            activity_row = await repo.get_activity(activity_id)
            if activity_row is None:
                continue
            if activity_row.dossier_id != dossier_id:
                continue

            # What did this activity touch? generated + used.
            generated = await repo.get_entities_generated_by_activity(activity_id)
            used = await repo.get_used_entities_for_activity(activity_id)
            all_touched = generated + used

            # Target type present at this activity?
            candidates = [e for e in all_touched if e.type == target_type]
            if candidates:
                entity_ids = {e.entity_id for e in candidates}
                if len(entity_ids) == 1:
                    # Return the current latest version of this entity_id.
                    # The scope check above means this query is guaranteed
                    # to be for an entity in ``dossier_id`` — the
                    # ``dossier_id`` argument here is belt-and-braces,
                    # not the primary defense.
                    return await repo.get_latest_entity_by_id(
                        dossier_id, candidates[0].entity_id,
                    )
                # Bug 54: raise, don't silently return None. Ambiguity
                # at a single activity is a structural signal (the
                # PROV graph says "this activity touched two different
                # aanvragen" or similar) — squashing it to None hid
                # the distinction between "no anchor available" and
                # "multiple candidate anchors, refusing to guess."
                raise LineageAmbiguous(
                    activity_id=activity_id,
                    target_type=target_type,
                    candidate_entity_ids=sorted(entity_ids),
                )

            # No match here — expand the frontier backwards.
            # (a) Through each used entity's generating activity.
            #     Skip activities already visited to keep the frontier
            #     bounded (Bug 53).
            for used_entity in used:
                gen_by = used_entity.generated_by
                if gen_by is not None and gen_by not in visited_activities:
                    next_frontier.add(gen_by)
            # (b) Through the informed_by chain — only same-dossier.
            # Cross-dossier references (informed_by_uri) can't be walked
            # from within one repository scope; the lineage walker is
            # intra-dossier by design. The dossier-scope check at the
            # top of the loop will reject any informed_by_activity_id
            # that somehow points cross-dossier.
            informed_by = activity_row.informed_by_activity_id
            if informed_by is not None and informed_by not in visited_activities:
                next_frontier.add(informed_by)

        frontier = next_frontier

    return None
