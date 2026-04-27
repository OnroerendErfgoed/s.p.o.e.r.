"""Unit tests for ``move_bijlagen_to_permanent`` — Bug 30.

Before Bug 30's fix this function ran a bare ``except Exception`` per
file, logged at ERROR without ``exc_info``, and continued the loop
regardless of outcome. The task was marked completed by the worker
even when half the files had failed to move, so the aanvraag entity
persisted with file_ids pointing at unmoved files in the file service's
``temp/`` area. Downloads returned 404 forever, invisibly.

The fix introduces three behaviours these tests pin:

1. **Happy path (all 200)** — task completes normally, no raise.
2. **Per-file 403 (dossier-binding mismatch)** — audit event emitted
   attributed to the triggering user (the aanvrager whose activity
   referenced the cross-dossier file_id), the failure is recorded,
   and the task raises at the end so the worker's recorded-task
   retry machinery fires. A persistent 403 surfaces the corrupted
   aanvraag to ops instead of leaving it permanently broken.
3. **Infrastructure failures (5xx, network, exceptions)** — logged
   with ``exc_info=True`` (Round 13's M2 Stage 1 pattern, for
   Sentry breadcrumbs), recorded as failures, raise at end of loop.

The raise-after-loop shape is important: successes in the same batch
aren't reverted (``/internal/move`` is idempotent, so a retry no-ops
for them), and a transient network blip recovers on the worker's
next attempt without operator intervention.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from dossier_engine.auth import User
from dossier_toelatingen.tasks import move_bijlagen_to_permanent


D1 = UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Fake ActivityContext
# ---------------------------------------------------------------------------
#
# The task only touches a small subset of ActivityContext:
#   * ``dossier_id`` (UUID)
#   * ``triggering_activity_id`` (UUID) — used by the function to look up
#     the aanvraag version that scheduled this task
#   * ``triggering_user`` (User) — used by the Bug 30 fix when emitting
#     the 403 dossier.denied audit event
#   * ``repo.get_entities_generated_by_activity(...)``
#   * ``repo.get_entity(...)``  (only used when the aanvraag has a parent)
#
# A SimpleNamespace + AsyncMock is enough — no DB, no engine pipeline.


def _make_user(user_id: str = "aanvrager-1") -> User:
    return User(
        id=user_id, type="natuurlijk_persoon", name=f"User {user_id}",
        roles=[], properties={},
    )


def _make_aanvraag_row(file_ids: list[str], *, derived_from=None):
    """A minimal aanvraag EntityRow stand-in."""
    return SimpleNamespace(
        type="oe:aanvraag",
        content={
            "titel": "test",
            "bijlagen": [{"file_id": fid} for fid in file_ids],
        },
        derived_from=derived_from,
    )


def _make_ctx(file_ids: list[str], user: User | None = None):
    user = user or _make_user()
    aanvraag = _make_aanvraag_row(file_ids)

    repo = MagicMock()
    repo.get_entities_generated_by_activity = AsyncMock(return_value=[aanvraag])
    repo.get_entity = AsyncMock(return_value=None)  # no parent → all bijlagen are new

    return SimpleNamespace(
        dossier_id=D1,
        triggering_activity_id=uuid4(),
        triggering_user=user,
        repo=repo,
    )


class _FakeResponse:
    """Stand-in for aiohttp's response context manager."""
    def __init__(self, status: int, text: str = ""):
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession. Returns canned responses in
    order — one per .post() call."""
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, params=None, **kwargs):
        self.calls.append({"url": url, "params": params})
        return self._responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _patch_session(responses: list[_FakeResponse]):
    """Patch aiohttp.ClientSession() to return a _FakeSession."""
    fake = _FakeSession(responses)
    return patch("aiohttp.ClientSession", return_value=fake), fake


class TestMoveBijlagenHappyPath:

    async def test_all_200_no_raise(self):
        """Every file returns 200 — task completes normally without
        raising and without emitting any audit events. Pin the
        baseline: the fix must not change behaviour for the happy
        path, which is ~99% of production runs."""
        ctx = _make_ctx(["f1", "f2", "f3"])
        responses = [_FakeResponse(200) for _ in range(3)]
        patcher, fake = _patch_session(responses)

        with patcher, patch(
            "dossier_toelatingen.tasks.emit_dossier_audit",
        ) as mock_emit:
            # Does not raise.
            await move_bijlagen_to_permanent(ctx)

        assert len(fake.calls) == 3
        assert mock_emit.call_count == 0


class TestMoveBijlagenFailureTracking:

    async def test_403_emits_audit_and_raises(self):
        """Single 403 response: an audit event is emitted attributed
        to the triggering_user (the aanvrager whose activity created
        this task), and the function raises so the worker retries.
        Before the fix, 403 was logged and swallowed — the task was
        marked completed even though the aanvraag had a
        never-to-be-movable file reference."""
        user = _make_user("alice-aanvrager")
        ctx = _make_ctx(["bad-file"], user=user)
        responses = [_FakeResponse(403, "dossier binding mismatch")]
        patcher, fake = _patch_session(responses)

        with patcher, patch(
            "dossier_toelatingen.tasks.emit_dossier_audit",
        ) as mock_emit:
            with pytest.raises(RuntimeError) as exc:
                await move_bijlagen_to_permanent(ctx)

        # Task raised so the worker retries; summary names the file.
        assert "bad-file" in str(exc.value)
        assert "HTTP 403" in str(exc.value)

        # Audit event emitted exactly once, attributed to the
        # triggering user (not to SYSTEM_USER).
        assert mock_emit.call_count == 1
        call_kwargs = mock_emit.call_args.kwargs
        assert call_kwargs["action"] == "dossier.denied"
        assert call_kwargs["user"] is user  # attribution to triggering user
        assert call_kwargs["dossier_id"] == D1
        assert call_kwargs["outcome"] == "denied"
        assert "different dossier" in call_kwargs["reason"]
        assert call_kwargs["file_id"] == "bad-file"

    async def test_500_raises_without_audit_emit(self):
        """Infrastructure failure (5xx): logged and counted as failure,
        raises, but NO audit event — audit is for security events only.
        A file service outage is a monitoring/alerting concern, not a
        SIEM concern."""
        ctx = _make_ctx(["f1"])
        responses = [_FakeResponse(500, "internal error")]
        patcher, _ = _patch_session(responses)

        with patcher, patch(
            "dossier_toelatingen.tasks.emit_dossier_audit",
        ) as mock_emit:
            with pytest.raises(RuntimeError) as exc:
                await move_bijlagen_to_permanent(ctx)

        assert "HTTP 500" in str(exc.value)
        assert mock_emit.call_count == 0

    async def test_exception_raises_with_exc_info_logged(self, caplog):
        """A raised exception during the POST (network error, timeout,
        etc.) is caught, logged with exc_info=True, counted as a
        failure, and the task raises at loop end. The exc_info=True
        is the bridge to Sentry via Round 13's LoggingIntegration —
        before this fix, only the str(e) reached any log."""
        import logging

        class BoomSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, *a, **kw):
                raise ConnectionResetError("simulated network failure")

        ctx = _make_ctx(["f1"])
        with patch("aiohttp.ClientSession", return_value=BoomSession()):
            with caplog.at_level(logging.ERROR, logger="toelatingen.tasks"):
                with pytest.raises(RuntimeError) as exc:
                    await move_bijlagen_to_permanent(ctx)

        assert "ConnectionResetError" in str(exc.value)
        # At least one ERROR log record from this logger with an
        # exc_info attached (the Round 13 pattern).
        error_records = [
            r for r in caplog.records
            if r.name == "toelatingen.tasks" and r.levelno >= logging.ERROR
        ]
        assert error_records, "no ERROR records captured"
        # At least one record must carry exc_info — that's the
        # Sentry-breadcrumb bridge. Before Bug 30 this was missing.
        assert any(r.exc_info is not None for r in error_records), (
            "no record carried exc_info; Sentry won't get a traceback"
        )

    async def test_mixed_200_and_500_raises_listing_only_failures(self):
        """Batch of three: first 200, second 500, third 200. The task
        still raises (because one failed), but the raised error names
        only the failing file. Pin that successes in the same batch
        aren't counted as failures or reverted — /internal/move is
        idempotent so the successful moves stay done."""
        ctx = _make_ctx(["f1", "f2", "f3"])
        responses = [
            _FakeResponse(200),
            _FakeResponse(500, "boom"),
            _FakeResponse(200),
        ]
        patcher, fake = _patch_session(responses)

        with patcher, patch(
            "dossier_toelatingen.tasks.emit_dossier_audit",
        ):
            with pytest.raises(RuntimeError) as exc:
                await move_bijlagen_to_permanent(ctx)

        msg = str(exc.value)
        # Only the failing file_id is named; the two 200 file_ids aren't.
        assert "f2" in msg
        assert "f1" not in msg
        assert "f3" not in msg
        # "1 of 3" count reflects only-f2-failed.
        assert "1 of 3" in msg
        # All three HTTP calls did happen — the loop didn't short-circuit.
        assert len(fake.calls) == 3

    async def test_triggering_user_is_attributed_not_system(self):
        """The audit emit's ``user`` field must carry the
        triggering_user (the aanvrager or behandelaar whose activity
        scheduled this task), not SYSTEM_USER. This is the whole
        point of the two-field plumbing that landed alongside Bug 30:
        SIEM rules that key on actor identity need the human, not
        'system', in the denial stream."""
        from dossier_engine.auth import SYSTEM_USER
        user = _make_user("bob-behandelaar")
        assert user is not SYSTEM_USER  # sanity

        ctx = _make_ctx(["stolen-file"], user=user)
        responses = [_FakeResponse(403, "nope")]
        patcher, _ = _patch_session(responses)

        with patcher, patch(
            "dossier_toelatingen.tasks.emit_dossier_audit",
        ) as mock_emit:
            with pytest.raises(RuntimeError):
                await move_bijlagen_to_permanent(ctx)

        assert mock_emit.call_count == 1
        emitted_user = mock_emit.call_args.kwargs["user"]
        assert emitted_user is user
        assert emitted_user is not SYSTEM_USER
        assert emitted_user.id == "bob-behandelaar"
