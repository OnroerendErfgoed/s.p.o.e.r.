"""
Integration tests for `lineage.find_related_entity` — the
activity-graph walker that finds related entities by walking
backwards through the PROV graph.

Branches:
* Start entity IS the target type → return itself (trivial)
* Start entity has no generated_by → return None (root/external)
* Target found at first hop (in the generating activity's scope)
* Target found after two hops (through used entity's generator)
* Ambiguous result (two distinct entity_ids of target type at
  one activity) → return None
* Max hops exhausted → return None
* Target not found anywhere → return None
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.lineage import find_related_entity


D1 = UUID("11111111-1111-1111-1111-111111111111")


async def _bootstrap(repo: Repository) -> UUID:
    await repo.create_dossier(D1, "test")
    await repo.ensure_agent("system", "systeem", "Systeem", {})
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type="systemAction",
        started_at=now, ended_at=now,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _make_activity(repo, act_type="act", informed_by=None):
    act_id = uuid4()
    now = datetime.now(timezone.utc)
    await repo.create_activity(
        activity_id=act_id, dossier_id=D1, type=act_type,
        started_at=now, ended_at=now,
        informed_by=informed_by,
    )
    repo.session.add(AssociationRow(
        id=uuid4(), activity_id=act_id, agent_id="system",
        agent_name="Systeem", agent_type="systeem", role="systeem",
    ))
    await repo.session.flush()
    return act_id


async def _make_entity(repo, gen_by, etype, eid=None):
    eid = eid or uuid4()
    vid = uuid4()
    await repo.create_entity(
        version_id=vid, entity_id=eid, dossier_id=D1,
        type=etype, generated_by=gen_by,
        content={}, attributed_to="system",
    )
    await repo.session.flush()
    return eid, vid


class TestFindRelatedEntity:

    async def test_start_entity_is_target_type(self, repo):
        """Trivial case: the start entity is already the target
        type. Returns itself without walking."""
        boot = await _bootstrap(repo)
        eid, vid = await _make_entity(repo, boot, "oe:aanvraag")
        row = await repo.get_entity(vid)

        result = await find_related_entity(
            repo, D1, row, "oe:aanvraag",
        )
        assert result is not None
        assert result.id == vid

    async def test_start_entity_no_generated_by_returns_none(self, repo):
        """Start entity has no generating activity (external or
        root). Can't walk — returns None."""
        boot = await _bootstrap(repo)
        eid = uuid4()
        vid = uuid4()
        await repo.create_entity(
            version_id=vid, entity_id=eid, dossier_id=D1,
            type="external", generated_by=None,  # no generator
            content={"uri": "https://example.org"},
            attributed_to="system",
        )
        await repo.session.flush()
        row = await repo.get_entity(vid)

        result = await find_related_entity(
            repo, D1, row, "oe:aanvraag",
        )
        assert result is None

    async def test_target_found_at_first_hop(self, repo):
        """Activity A generates both an aanvraag and a
        beslissing. Start from beslissing, find aanvraag at
        the same activity (one hop)."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "makeStuff")
        aanvraag_eid, _ = await _make_entity(repo, act_a, "oe:aanvraag")
        _, beslissing_vid = await _make_entity(repo, act_a, "oe:beslissing")

        start_row = await repo.get_entity(beslissing_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == aanvraag_eid

    async def test_target_found_via_used_entity_generator(self, repo):
        """Two activities:
        * A generates aanvraag
        * B uses aanvraag, generates beslissing

        Start from beslissing, target is aanvraag. The walker:
        1. Visits B (beslissing's generator) — checks generated+used.
           Finds aanvraag in used. Returns it."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "createAanvraag")
        aanvraag_eid, aanvraag_vid = await _make_entity(
            repo, act_a, "oe:aanvraag",
        )

        act_b = await _make_activity(repo, "makeBeslissing")
        await repo.create_used(act_b, aanvraag_vid)
        _, beslissing_vid = await _make_entity(
            repo, act_b, "oe:beslissing",
        )
        await repo.session.flush()

        start_row = await repo.get_entity(beslissing_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == aanvraag_eid

    async def test_target_found_two_hops_away(self, repo):
        """Three activities:
        * A generates aanvraag
        * B uses aanvraag, generates dossier_access
        * C uses dossier_access, generates nota

        Start from nota, target is aanvraag. The walker needs
        to go through C → dossier_access → B → aanvraag (two
        hops). The walker checks C's scope first (finds
        dossier_access, not aanvraag), then walks to
        dossier_access's generator (B), then finds aanvraag in
        B's used."""
        boot = await _bootstrap(repo)

        act_a = await _make_activity(repo, "a")
        aanvraag_eid, aanvraag_vid = await _make_entity(
            repo, act_a, "oe:aanvraag",
        )

        act_b = await _make_activity(repo, "b")
        await repo.create_used(act_b, aanvraag_vid)
        _, access_vid = await _make_entity(repo, act_b, "oe:access")
        await repo.session.flush()

        act_c = await _make_activity(repo, "c")
        await repo.create_used(act_c, access_vid)
        _, nota_vid = await _make_entity(repo, act_c, "system:note")
        await repo.session.flush()

        start_row = await repo.get_entity(nota_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is not None
        assert result.entity_id == aanvraag_eid

    async def test_ambiguous_returns_none(self, repo):
        """Activity A generates TWO distinct aanvraag entities
        and one beslissing. Start from beslissing, target is
        aanvraag. Two distinct entity_ids → ambiguous → None."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "a")
        await _make_entity(repo, act_a, "oe:aanvraag")
        await _make_entity(repo, act_a, "oe:aanvraag")
        _, beslissing_vid = await _make_entity(repo, act_a, "oe:beslissing")

        start_row = await repo.get_entity(beslissing_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )
        assert result is None

    async def test_max_hops_exhausted_returns_none(self, repo):
        """Build a chain deeper than max_hops and verify the
        walker gives up."""
        boot = await _bootstrap(repo)
        # Chain: boot → entity → act1 → entity → act2 → ...
        prev_act = boot
        for i in range(5):
            _, vid = await _make_entity(repo, prev_act, f"type_{i}")
            prev_act = await _make_activity(repo, f"act_{i}")
            await repo.create_used(prev_act, vid)
            await repo.session.flush()

        # Final entity in the chain
        _, start_vid = await _make_entity(repo, prev_act, "oe:end")
        # The aanvraag is at the root (boot), 5+ hops away
        await _make_entity(repo, boot, "oe:aanvraag")

        start_row = await repo.get_entity(start_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag", max_hops=2,
        )
        # With max_hops=2, the walker can't reach the root
        assert result is None

    async def test_no_match_anywhere_returns_none(self, repo):
        """Target type doesn't exist in the graph at all.
        Walker exhausts the frontier and returns None."""
        boot = await _bootstrap(repo)
        act_a = await _make_activity(repo, "a")
        _, vid = await _make_entity(repo, act_a, "oe:beslissing")

        start_row = await repo.get_entity(vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:nonexistent",
        )
        assert result is None


class TestCrossDossierDefense:
    """Bug 55 — defense in depth. The walker must not traverse activities
    from a different dossier, even if a PROV edge (``generated_by``,
    ``used``, ``informed_by_activity_id``) somehow points across the
    boundary. In normal operation that never happens, but these tests
    pin the guard so a future regression doesn't silently re-open the
    traversal hole.

    Attack/drift scenario: a data integrity violation or PROV
    manipulation produces an edge from dossier D1 into an activity in
    dossier D2. Before Bug 55, the walker would follow the edge and
    inspect D2's activity data (wasted queries at best; a confirmation
    side channel about D2's activity graph at worst). After Bug 55,
    the walker refuses to traverse the foreign activity — same as if
    the edge didn't exist.

    Important test-design note: the **return value** of
    ``find_related_entity`` was *already* None in these cases pre-
    Bug-55, because line 87's ``get_latest_entity_by_id(dossier_id, ...)``
    enforces dossier scope on the final return. That's why Bug 55 is
    "defense in depth" — the leak was in the traversal, not in the
    return. These tests therefore pin the *traversal behaviour* (did
    we query D2's entities?) via repo-call spying, not just the
    return value. Without this spy, a regression that removed the
    guard would silently re-open the walk without any test going red."""

    async def test_informed_by_across_dossier_does_not_traverse(
        self, repo, monkeypatch,
    ):
        """The `informed_by_activity_id` path is the most likely
        route for a cross-dossier traversal (it's an activity-to-
        activity pointer, not mediated by an entity). If it ever
        points at an activity in D2, the walker on D1 must refuse
        to expand into it — specifically, the walker must NOT call
        ``get_entities_generated_by_activity`` or
        ``get_used_entities_for_activity`` against the D2 activity
        id. Spy on those calls to pin the behaviour."""
        D2 = UUID("22222222-2222-2222-2222-222222222222")

        # D1 setup
        boot_d1 = await _bootstrap(repo)

        # D2 setup — separate dossier with an aanvraag in it
        await repo.create_dossier(D2, "test")
        now = datetime.now(timezone.utc)
        d2_act = uuid4()
        await repo.create_activity(
            activity_id=d2_act, dossier_id=D2, type="indienen",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=d2_act, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        d2_aanvraag_vid = uuid4()
        await repo.create_entity(
            version_id=d2_aanvraag_vid, entity_id=uuid4(),
            dossier_id=D2, type="oe:aanvraag",
            generated_by=d2_act, content={}, attributed_to="system",
        )
        await repo.session.flush()

        # Now craft the anomaly: a D1 activity whose
        # informed_by_activity_id points at d2_act. In production this
        # shouldn't happen — informed_by is same-dossier by convention
        # — but if it did, the walker pre-Bug-55 would traverse into
        # D2's aanvraag.
        d1_tainted = uuid4()
        await repo.create_activity(
            activity_id=d1_tainted, dossier_id=D1, type="beslissen",
            started_at=now, ended_at=now,
            informed_by=str(d2_act),
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=d1_tainted, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        _, d1_start_vid = await _make_entity(repo, d1_tainted, "oe:beslissing")

        # Spy on the traversal helpers. Capture every activity_id the
        # walker queries so we can assert it never touched d2_act.
        touched_activity_ids: list[UUID] = []
        real_generated = repo.get_entities_generated_by_activity
        real_used = repo.get_used_entities_for_activity

        async def spy_generated(activity_id):
            touched_activity_ids.append(activity_id)
            return await real_generated(activity_id)

        async def spy_used(activity_id):
            touched_activity_ids.append(activity_id)
            return await real_used(activity_id)

        monkeypatch.setattr(repo, "get_entities_generated_by_activity", spy_generated)
        monkeypatch.setattr(repo, "get_used_entities_for_activity", spy_used)

        start_row = await repo.get_entity(d1_start_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )

        assert result is None
        # The walker must have queried d1_tainted (it's in D1, and
        # it's where the walk starts), but it must NOT have queried
        # d2_act — the guard rejects it before the generated/used
        # queries fire.
        assert d1_tainted in touched_activity_ids, (
            "Sanity check: the walker should have queried d1_tainted"
        )
        assert d2_act not in touched_activity_ids, (
            f"Bug 55 regression: walker queried cross-dossier activity "
            f"{d2_act}. Touched ids: {touched_activity_ids}"
        )

    async def test_generated_by_across_dossier_does_not_traverse(
        self, repo, monkeypatch,
    ):
        """A corrupted ``used`` edge pointing at a D2 entity would
        produce a ``used_entity.generated_by`` that's a D2 activity.
        The walker follows used entities' generators to expand the
        frontier. Verify the guard rejects the D2 activity when it
        surfaces in the frontier — pin via repo-call spy, same
        rationale as above."""
        D2 = UUID("33333333-3333-3333-3333-333333333333")

        # D1 setup
        boot_d1 = await _bootstrap(repo)

        # D2 setup — entity generated by a D2 activity
        await repo.create_dossier(D2, "test")
        now = datetime.now(timezone.utc)
        d2_act = uuid4()
        await repo.create_activity(
            activity_id=d2_act, dossier_id=D2, type="indienen",
            started_at=now, ended_at=now,
        )
        repo.session.add(AssociationRow(
            id=uuid4(), activity_id=d2_act, agent_id="system",
            agent_name="Systeem", agent_type="systeem", role="systeem",
        ))
        d2_entity_vid = uuid4()
        d2_entity_eid = uuid4()
        await repo.create_entity(
            version_id=d2_entity_vid, entity_id=d2_entity_eid,
            dossier_id=D2, type="oe:middle",
            generated_by=d2_act, content={}, attributed_to="system",
        )
        # And an aanvraag in D2 — the target we don't want to leak.
        await repo.create_entity(
            version_id=uuid4(), entity_id=uuid4(),
            dossier_id=D2, type="oe:aanvraag",
            generated_by=d2_act, content={}, attributed_to="system",
        )
        await repo.session.flush()

        # D1 activity that "uses" the D2 entity (the anomaly).
        d1_act = await _make_activity(repo, "derive")
        await repo.create_used(d1_act, d2_entity_vid)
        _, d1_start_vid = await _make_entity(repo, d1_act, "oe:beslissing")
        await repo.session.flush()

        # Spy on traversal helpers (same pattern as the informed_by test).
        touched_activity_ids: list[UUID] = []
        real_generated = repo.get_entities_generated_by_activity
        real_used = repo.get_used_entities_for_activity

        async def spy_generated(activity_id):
            touched_activity_ids.append(activity_id)
            return await real_generated(activity_id)

        async def spy_used(activity_id):
            touched_activity_ids.append(activity_id)
            return await real_used(activity_id)

        monkeypatch.setattr(repo, "get_entities_generated_by_activity", spy_generated)
        monkeypatch.setattr(repo, "get_used_entities_for_activity", spy_used)

        start_row = await repo.get_entity(d1_start_vid)
        result = await find_related_entity(
            repo, D1, start_row, "oe:aanvraag",
        )

        assert result is None
        assert d1_act in touched_activity_ids, (
            "Sanity check: walker should have queried d1_act"
        )
        assert d2_act not in touched_activity_ids, (
            f"Bug 55 regression: walker queried cross-dossier activity "
            f"{d2_act}. Touched ids: {touched_activity_ids}"
        )
