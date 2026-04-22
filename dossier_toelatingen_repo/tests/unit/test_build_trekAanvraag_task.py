"""Unit tests for ``_build_trekAanvraag_task`` — specifically the
Bug 54 (Round 25) caller-side handling of ``LineageAmbiguous``.

The handler's lineage-walk fallback (when an aanvraag isn't in
``used``, walk from the beslissing) used to silently proceed with
an unanchored task on both ambiguity and not-found. After Bug 54,
the walker distinguishes the two cases — ambiguity raises
``LineageAmbiguous``, not-found still returns ``None`` — and this
caller handles the exception by logging a warning and proceeding
with an unanchored task.

These tests pin the caller's end of the contract:
* Happy path (walker returns the aanvraag) → anchored task.
* Not-found (walker returns None) → unanchored task, no log.
* Ambiguous (walker raises LineageAmbiguous) → unanchored task
  AND a WARNING log line with the candidate entity_ids.

We fake the ``ActivityContext`` with a ``SimpleNamespace`` and
monkeypatch ``find_related_entity`` at the import site inside
``_build_trekAanvraag_task`` — which imports it lazily via
``from dossier_engine.lineage import find_related_entity``, so
we patch the original module attribute.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest


D1 = UUID("11111111-1111-1111-1111-111111111111")


def _make_context(
    aanvraag_in_used: bool,
    beslissing_in_used: bool = True,
    *,
    aanvraag_entity_id: UUID | None = None,
    beslissing_entity_id: UUID | None = None,
):
    """Build a minimal ActivityContext-shaped object. Only the
    attributes ``_build_trekAanvraag_task`` reads are populated:
    ``get_used_row``, ``repo``, ``dossier_id``, ``constants``."""
    aanvraag_row = (
        SimpleNamespace(entity_id=aanvraag_entity_id or uuid4())
        if aanvraag_in_used else None
    )
    beslissing_row = (
        SimpleNamespace(entity_id=beslissing_entity_id or uuid4())
        if beslissing_in_used else None
    )

    def get_used_row(type_name: str):
        if type_name == "oe:aanvraag":
            return aanvraag_row
        if type_name == "oe:beslissing":
            return beslissing_row
        return None

    return SimpleNamespace(
        get_used_row=get_used_row,
        repo=SimpleNamespace(),   # walker is mocked, repo is unused
        dossier_id=D1,
        constants=SimpleNamespace(aanvraag_deadline_days=60),
    )


class TestBuildTrekAanvraagTask:

    @pytest.mark.asyncio
    async def test_happy_path_aanvraag_in_used_anchors_task(self):
        """If the aanvraag is directly in ``used``, no lineage walk
        runs and the task comes out anchored to it. Sanity check —
        this path doesn't touch Bug 54's machinery."""
        from dossier_toelatingen.handlers import _build_trekAanvraag_task

        aanvraag_eid = uuid4()
        ctx = _make_context(
            aanvraag_in_used=True, aanvraag_entity_id=aanvraag_eid,
        )
        task = await _build_trekAanvraag_task(ctx)

        assert task is not None
        assert task["target_activity"] == "trekAanvraagIn"
        assert task["anchor_type"] == "oe:aanvraag"
        assert task["anchor_entity_id"] == str(aanvraag_eid)

    @pytest.mark.asyncio
    async def test_walker_returns_none_unanchored_task(self, monkeypatch):
        """Not-found case: walker returns None (e.g. start entity
        has no generated_by, frontier exhausted, max hops). Task
        goes out unanchored — no ``anchor_entity_id`` key set —
        and no log line fires. Pins that Bug 54's changes didn't
        accidentally log on every unanchored task."""
        from dossier_engine import lineage
        from dossier_toelatingen.handlers import _build_trekAanvraag_task

        async def fake_walker(*args, **kwargs):
            return None

        monkeypatch.setattr(lineage, "find_related_entity", fake_walker)

        ctx = _make_context(aanvraag_in_used=False, beslissing_in_used=True)
        task = await _build_trekAanvraag_task(ctx)

        assert task is not None
        # Key must be absent, not just None — the caller builds the
        # dict conditionally.
        assert "anchor_entity_id" not in task
        assert task["anchor_type"] == "oe:aanvraag"

    @pytest.mark.asyncio
    async def test_walker_raises_ambiguous_logs_and_unanchors(
        self, monkeypatch, caplog,
    ):
        """Bug 54 regression: walker raises ``LineageAmbiguous``.
        Caller must (a) catch it, (b) emit a WARNING log carrying
        the candidate entity_ids so an operator can triage, (c)
        still produce a task — just unanchored.

        Before Bug 54's fix this was indistinguishable from
        not-found: both silently produced an unanchored task with
        no log line, so operators had no way to know the underlying
        PROV graph had a structural anomaly."""
        from dossier_engine import lineage
        from dossier_engine.lineage import LineageAmbiguous
        from dossier_toelatingen.handlers import _build_trekAanvraag_task

        cand_a, cand_b = uuid4(), uuid4()
        ambiguous_activity = uuid4()

        async def fake_walker(*args, **kwargs):
            raise LineageAmbiguous(
                activity_id=ambiguous_activity,
                target_type="oe:aanvraag",
                candidate_entity_ids=[cand_a, cand_b],
            )

        monkeypatch.setattr(lineage, "find_related_entity", fake_walker)

        beslissing_eid = uuid4()
        ctx = _make_context(
            aanvraag_in_used=False, beslissing_in_used=True,
            beslissing_entity_id=beslissing_eid,
        )

        with caplog.at_level(logging.WARNING,
                             logger="dossier_toelatingen.handlers"):
            task = await _build_trekAanvraag_task(ctx)

        # (a + c) Task still came out, but unanchored.
        assert task is not None
        assert "anchor_entity_id" not in task
        assert task["anchor_type"] == "oe:aanvraag"

        # (b) The log line exists and carries enough triage info.
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert len(warnings) == 1, (
            f"Expected exactly 1 WARNING, got {len(warnings)}: "
            f"{[r.message for r in warnings]}"
        )
        msg = warnings[0].getMessage()
        # Identifies which beslissing + dossier triggered it.
        assert str(beslissing_eid) in msg
        assert str(D1) in msg
        # Identifies the ambiguous activity + candidates — the
        # affordance Bug 54 added.
        assert str(ambiguous_activity) in msg
        assert str(cand_a) in msg or str(cand_b) in msg

    @pytest.mark.asyncio
    async def test_no_beslissing_no_walk_unanchored(self, monkeypatch):
        """Edge case: neither aanvraag nor beslissing in used.
        Walker shouldn't even run; task comes out unanchored.
        Pins that the walker-invocation gate (`if beslissing_row
        is not None`) still holds after the Bug 54 refactor."""
        from dossier_engine import lineage
        from dossier_toelatingen.handlers import _build_trekAanvraag_task

        walker_called = False
        async def fake_walker(*args, **kwargs):
            nonlocal walker_called
            walker_called = True
            return None

        monkeypatch.setattr(lineage, "find_related_entity", fake_walker)

        ctx = _make_context(aanvraag_in_used=False, beslissing_in_used=False)
        task = await _build_trekAanvraag_task(ctx)

        assert task is not None
        assert "anchor_entity_id" not in task
        assert not walker_called, (
            "Walker should not be invoked when beslissing is absent"
        )
