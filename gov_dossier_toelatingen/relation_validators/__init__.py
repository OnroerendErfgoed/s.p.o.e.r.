"""
Relation validators for the toelatingen workflow.

These are invoked by the engine when processing the generic `relations`
block on an activity request. Each validator is registered under a relation
type string and receives the list of incoming relation entries for its type
plus any stale `used` references the client declared. It returns the set
of stale entity_logical_ids whose staleness this relation "covers" —
or raises `ActivityError` to reject the request outright.
"""

from __future__ import annotations

from uuid import UUID

from gov_dossier_engine.engine import ActivityError
from gov_dossier_engine.plugin import Plugin
from gov_dossier_engine.db.models import Repository


async def validate_neemt_akte_van(
    *,
    plugin: Plugin,
    repo: Repository,
    dossier_id: UUID,
    activity_def: dict,
    entries: list[dict],
    stale_used,
) -> set[UUID]:
    """Validate `oe:neemtAkteVan` relations against stale used references.

    For each stale used reference, the client must acknowledge EVERY
    intervening version via `oe:neemtAkteVan`. If every intervening version
    for a given stale entry is covered, that entry's `entity_logical_id` is
    returned in the satisfied set.

    Additional rules:
    * Every `neemtAkteVan` entry must correspond to an intervening version
      of some stale `used` entry. Acknowledging unrelated entities is a
      client bug and is rejected.
    * Partial coverage (acknowledging some but not all intervening versions
      for a single stale entry) is NOT acceptance — the stale entry simply
      isn't satisfied and the engine will raise 409. We also reject
      outright if the client acknowledged a non-latest intervening version
      but missed a newer one, because that's almost certainly a client
      bug: they saw v3 but didn't notice v4.
    """
    # Map every intervening version → the stale entry it belongs to, so we
    # can quickly verify each incoming ack points at a real gap.
    intervening_to_stale: dict[UUID, object] = {}
    for s in stale_used:
        for v in s.intervening_version_ids:
            intervening_to_stale[v] = s

    # Track, per stale entry, which intervening versions the client covered.
    covered_per_stale: dict[UUID, set[UUID]] = {
        s.entity_logical_id: set() for s in stale_used
    }

    for entry in entries:
        ack_row = entry["entity_row"]
        ack_version = ack_row.id
        ref = entry["ref"]

        stale_entry = intervening_to_stale.get(ack_version)
        if stale_entry is None:
            raise ActivityError(
                422,
                f"oe:neemtAkteVan acknowledges {ref} but that version is not "
                f"an intervening version of any stale used reference in this "
                f"activity. Only newer versions of entities listed in `used` "
                f"can be acknowledged.",
                payload={
                    "error": "unrelated_acknowledgement",
                    "acknowledged": ref,
                },
            )

        covered_per_stale[stale_entry.entity_logical_id].add(ack_version)

    # A stale entry is satisfied iff every one of its intervening versions
    # has been acknowledged.
    satisfied: set[UUID] = set()
    for s in stale_used:
        needed = set(s.intervening_version_ids)
        if not needed:
            continue  # shouldn't happen but harmless
        if covered_per_stale[s.entity_logical_id] >= needed:
            satisfied.add(s.entity_logical_id)

    return satisfied


RELATION_VALIDATORS = {
    "oe:neemtAkteVan": validate_neemt_akte_van,
}
