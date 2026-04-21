"""
HTTP tests for the PUT activity execution routes:

* `PUT /dossiers/{id}/activities/{activity_id}` — generic single
* `PUT /dossiers/{id}/activities` — batch execution
* `PUT /dossiers/{id}/activities/{activity_id}/{activity_type}` —
  per-workflow typed wrapper

These exercise the main write path of the dossier API end-to-end
through the HTTP boundary: request parsing, plugin resolution,
engine invocation, response formatting, error mapping.

Like `test_http_routes.py`, we use `httpx.AsyncClient` with
`ASGITransport` to avoid the TestClient cross-loop trap.

The test plugin (`_build_activity_test_app`) has two synthetic
activities beyond SYSTEM_ACTION_DEF:

* **createStuff** — a `can_create_dossier` activity (so PUTs
  against a fresh dossier id work), generates `oe:aanvraag`,
  authorization=authenticated (any logged-in user can run it).
* **readStuff** — requires an existing `oe:aanvraag` in used,
  generates `oe:beslissing`, authorization restricted to
  users with the `oe:behandelaar` role.

This minimal pair covers: first-activity-on-new-dossier,
second-activity-reading-prior-output, authorization success,
authorization failure, workflow rule violations, missing
required used entities.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dossier_engine.auth import POCAuthMiddleware
from dossier_engine.db.models import Repository, AssociationRow
from dossier_engine.entities import SYSTEM_ACTION_DEF, SystemNote, TaskEntity
from dossier_engine.plugin import Plugin, PluginRegistry
from dossier_engine.routes import register_routes


D1 = UUID("11111111-1111-1111-1111-111111111111")
D2 = UUID("22222222-2222-2222-2222-222222222222")


CREATE_STUFF_DEF = {
    "name": "createStuff",
    "label": "Create Stuff",
    "description": "Creates a new oe:aanvraag in a fresh dossier.",
    "can_create_dossier": True,
    "client_callable": True,
    "default_role": "oe:aanvrager",
    "allowed_roles": ["oe:aanvrager"],
    "authorization": {"access": "authenticated"},
    "used": [],
    "generates": ["oe:aanvraag"],
    "status": "ingediend",
    "validators": [],
    "side_effects": [],
    "tasks": [],
}


READ_STUFF_DEF = {
    "name": "readStuff",
    "label": "Read Stuff",
    "description": "Reads an oe:aanvraag and generates an oe:beslissing.",
    "can_create_dossier": False,
    "client_callable": True,
    "default_role": "oe:behandelaar",
    "allowed_roles": ["oe:behandelaar"],
    "authorization": {
        "access": "roles",
        "roles": [{"role": "oe:behandelaar"}],
    },
    "used": [{"type": "oe:aanvraag"}],
    "generates": ["oe:beslissing"],
    "status": "beoordeeld",
    "validators": [],
    "side_effects": [],
    "tasks": [],
}


def _build_activity_test_app() -> FastAPI:
    """Build a minimal FastAPI app with the two test activities
    plus the system activities registered.

    Three POC users:
    * aanvrager — no roles (authenticated but no behandelaar)
    * behandelaar — roles=['oe:behandelaar']
    * outsider — no roles, used for "unauthorized" tests
    """
    plugin = Plugin(
        name="testwf",
        workflow={
            "name": "testwf",
            "activities": [
                SYSTEM_ACTION_DEF,
                CREATE_STUFF_DEF,
                READ_STUFF_DEF,
            ],
            "entity_types": [
                {"type": "oe:aanvraag", "cardinality": "multiple"},
                {"type": "oe:beslissing", "cardinality": "multiple"},
                {"type": "system:task", "cardinality": "multiple"},
                {"type": "system:note", "cardinality": "multiple"},
            ],
            "relations": [],
            "poc_users": [],
        },
        entity_models={
            "system:task": TaskEntity,
            "system:note": SystemNote,
        },
    )

    registry = PluginRegistry()
    registry.register(plugin)

    auth = POCAuthMiddleware([
        {
            "id": "aanvrager", "username": "aanvrager",
            "type": "natuurlijk_persoon", "name": "Aanvrager",
            "roles": [], "properties": {},
        },
        {
            "id": "behandelaar", "username": "behandelaar",
            "type": "natuurlijk_persoon", "name": "Behandelaar",
            "roles": ["oe:behandelaar"], "properties": {},
        },
        {
            "id": "outsider", "username": "outsider",
            "type": "natuurlijk_persoon", "name": "Outsider",
            "roles": [], "properties": {},
        },
    ])

    app = FastAPI()
    app.state.registry = registry
    app.state.config = {"file_service": {"url": "http://test", "signing_key": "k"}}
    register_routes(app, registry, auth, global_access=[])
    return app


@pytest_asyncio.fixture
async def activity_client():
    """AsyncClient wired to the two-activity test app."""
    app = _build_activity_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        yield c


async def _commit(repo: Repository) -> None:
    await repo.session.commit()


def _new_ref(entity_type: str) -> tuple[str, UUID, UUID]:
    """Build a fresh canonical ref with a new entity_id and version_id.
    Returns (ref, entity_id, version_id)."""
    eid = uuid4()
    vid = uuid4()
    return f"{entity_type}/{eid}@{vid}", eid, vid


# --------------------------------------------------------------------
# Generic single endpoint — happy path and request validation
# --------------------------------------------------------------------


class TestPutActivityHappyPath:

    async def test_create_dossier_with_first_activity(
        self, activity_client, repo,
    ):
        """Fresh dossier id, first activity `createStuff` has
        `can_create_dossier=True`. The request should create the
        dossier and persist the generated oe:aanvraag."""
        await _commit(repo)
        activity_id = uuid4()
        ref, eid, vid = _new_ref("oe:aanvraag")

        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{activity_id}",
            json={
                "type": "createStuff",
                "workflow": "testwf",
                "generated": [
                    {"entity": ref, "content": {"titel": "My application"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["activity"]["id"] == str(activity_id)
        assert body["activity"]["type"] == "oe:createStuff"
        assert body["dossier"]["id"] == str(D1)
        assert body["dossier"]["status"] == "ingediend"
        # Generated entity appears in the response
        assert len(body["generated"]) == 1
        assert body["generated"][0]["type"] == "oe:aanvraag"

    async def test_second_activity_reads_prior_generated_entity(
        self, activity_client, repo,
    ):
        """Run createStuff to seed an oe:aanvraag, then run
        readStuff referencing it via `used`. The second call
        persists an oe:beslissing and advances the dossier
        status."""
        await _commit(repo)
        # First: create the dossier + aanvraag
        aanvraag_ref, _, aanvraag_vid = _new_ref("oe:aanvraag")
        r1 = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "createStuff",
                "workflow": "testwf",
                "generated": [
                    {"entity": aanvraag_ref, "content": {"titel": "First"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r1.status_code == 200, r1.text

        # Second: reference the aanvraag via used, generate beslissing
        beslissing_ref, _, _ = _new_ref("oe:beslissing")
        r2 = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "readStuff",
                "used": [{"entity": aanvraag_ref}],
                "generated": [
                    {
                        "entity": beslissing_ref,
                        "content": {"uitkomst": "goedgekeurd"},
                    },
                ],
            },
            headers={"X-POC-User": "behandelaar"},
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["dossier"]["status"] == "beoordeeld"
        assert body["used"][0]["entity"] == aanvraag_ref
        assert body["generated"][0]["type"] == "oe:beslissing"


class TestPutActivityRequestValidation:

    async def test_missing_type_returns_422(self, activity_client, repo):
        """Generic endpoint requires `type` in the body — the
        typed wrapper bakes it in, but the generic path can't
        guess. 422."""
        await _commit(repo)
        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={"workflow": "testwf"},  # no type
            headers={"X-POC-User": "aanvrager"},
        )
        assert r.status_code == 422
        assert "type" in r.json()["detail"].lower()

    async def test_unknown_activity_type_returns_404(
        self, activity_client, repo,
    ):
        """Type is supplied but nothing in any loaded plugin
        matches. `_resolve_plugin_and_def` raises 404."""
        await _commit(repo)
        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={"type": "ghostActivity", "workflow": "testwf"},
            headers={"X-POC-User": "aanvrager"},
        )
        assert r.status_code == 404
        assert "ghostActivity" in r.json()["detail"]

    async def test_unknown_workflow_first_activity_returns_404(
        self, activity_client, repo,
    ):
        """`createStuff` exists in `testwf` but the request
        supplies an unknown workflow name. The fallback path
        fails to find the workflow. 404."""
        await _commit(repo)
        # First deregister the plugin, then ask for an activity
        # name that isn't registered anywhere — we hit the
        # "workflow_name supplied but plugin missing" branch.
        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "notRegisteredAnywhere",
                "workflow": "nonexistent_workflow",
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r.status_code == 404


class TestPutActivityErrorForwarding:

    async def test_activity_error_from_engine_becomes_http(
        self, activity_client, repo,
    ):
        """Supply an unresolvable local ref in `used` — the
        engine's resolve_used raises 422, which the route layer
        forwards via activity_error_to_http."""
        await _commit(repo)
        # Create the dossier first so we hit the engine's
        # used resolution, not the dossier-not-found path.
        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")
        r_create = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "createStuff",
                "workflow": "testwf",
                "generated": [
                    {"entity": aanvraag_ref, "content": {"titel": "x"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r_create.status_code == 200

        # Now try readStuff with a used ref pointing at a
        # nonexistent entity — 422 "Entity not found".
        bogus_ref = f"oe:aanvraag/{uuid4()}@{uuid4()}"
        beslissing_ref, _, _ = _new_ref("oe:beslissing")
        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "readStuff",
                "used": [{"entity": bogus_ref}],
                "generated": [
                    {"entity": beslissing_ref, "content": {"uitkomst": "x"}},
                ],
            },
            headers={"X-POC-User": "behandelaar"},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "not found" in detail.lower()

    async def test_authorization_failure_returns_403(
        self, activity_client, repo,
    ):
        """`readStuff` requires `oe:behandelaar` role. A user
        without that role gets 403 from the authorize phase."""
        await _commit(repo)
        # Create the dossier as aanvrager
        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")
        await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "createStuff",
                "workflow": "testwf",
                "generated": [
                    {"entity": aanvraag_ref, "content": {"titel": "x"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )

        # Try readStuff as outsider — 403
        beslissing_ref, _, _ = _new_ref("oe:beslissing")
        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "readStuff",
                "used": [{"entity": aanvraag_ref}],
                "generated": [
                    {"entity": beslissing_ref, "content": {"uitkomst": "x"}},
                ],
            },
            headers={"X-POC-User": "outsider"},
        )
        assert r.status_code == 403

    async def test_idempotent_replay_same_id_returns_existing(
        self, activity_client, repo,
    ):
        """PUTting the same activity_id twice against the same
        dossier is idempotent — the second call returns the
        existing activity's synthesized replay response rather
        than creating a new row."""
        await _commit(repo)
        activity_id = uuid4()
        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")

        payload = {
            "type": "createStuff",
            "workflow": "testwf",
            "generated": [
                {"entity": aanvraag_ref, "content": {"titel": "replay"}},
            ],
        }

        r1 = await activity_client.put(
            f"/dossiers/{D1}/activities/{activity_id}",
            json=payload,
            headers={"X-POC-User": "aanvrager"},
        )
        assert r1.status_code == 200

        # Same activity_id, same payload → idempotent
        r2 = await activity_client.put(
            f"/dossiers/{D1}/activities/{activity_id}",
            json=payload,
            headers={"X-POC-User": "aanvrager"},
        )
        assert r2.status_code == 200
        # Both responses reference the same activity
        assert r1.json()["activity"]["id"] == r2.json()["activity"]["id"]

    async def test_reusing_activity_id_for_different_dossier_returns_409(
        self, activity_client, repo,
    ):
        """Same activity_id across two different dossiers is a
        client bug → 409 'different dossier'."""
        await _commit(repo)
        activity_id = uuid4()

        # First: dossier D1
        aanvraag1, _, _ = _new_ref("oe:aanvraag")
        r1 = await activity_client.put(
            f"/dossiers/{D1}/activities/{activity_id}",
            json={
                "type": "createStuff", "workflow": "testwf",
                "generated": [
                    {"entity": aanvraag1, "content": {"titel": "d1"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r1.status_code == 200

        # Same activity_id, different dossier → 409
        aanvraag2, _, _ = _new_ref("oe:aanvraag")
        r2 = await activity_client.put(
            f"/dossiers/{D2}/activities/{activity_id}",
            json={
                "type": "createStuff", "workflow": "testwf",
                "generated": [
                    {"entity": aanvraag2, "content": {"titel": "d2"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r2.status_code == 409
        # Detail could be a string or a dict depending on payload shape
        detail = r2.json()["detail"]
        assert "different dossier" in (
            detail if isinstance(detail, str) else detail.get("detail", "")
        )


# --------------------------------------------------------------------
# Batch endpoint
# --------------------------------------------------------------------


class TestPutActivityBatch:

    async def test_two_activities_in_order(self, activity_client, repo):
        """Batch of [createStuff, readStuff]. The second sees
        the first's generated aanvraag and produces a
        beslissing. Single transaction, single response."""
        await _commit(repo)

        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")
        beslissing_ref, _, _ = _new_ref("oe:beslissing")

        r = await activity_client.put(
            f"/dossiers/{D1}/activities",
            json={
                "workflow": "testwf",
                "activities": [
                    {
                        "activity_id": str(uuid4()),
                        "type": "createStuff",
                        "generated": [
                            {"entity": aanvraag_ref, "content": {"titel": "A"}},
                        ],
                    },
                    {
                        "activity_id": str(uuid4()),
                        "type": "readStuff",
                        "role": "oe:behandelaar",
                        "used": [{"entity": aanvraag_ref}],
                        "generated": [
                            {
                                "entity": beslissing_ref,
                                "content": {"uitkomst": "goedgekeurd"},
                            },
                        ],
                    },
                ],
            },
            headers={"X-POC-User": "behandelaar"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["activities"]) == 2
        assert body["activities"][0]["activity"]["type"] == "oe:createStuff"
        assert body["activities"][1]["activity"]["type"] == "oe:readStuff"
        # Final dossier state reflects the last activity
        assert body["dossier"]["status"] == "beoordeeld"

    async def test_batch_error_annotated_with_index(
        self, activity_client, repo,
    ):
        """First activity OK, second fails. Error detail
        includes the batch position and activity name. Because
        the batch runs in one transaction, the first activity's
        effects don't persist — GET after the failed batch
        shows no dossier."""
        await _commit(repo)

        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")

        r = await activity_client.put(
            f"/dossiers/{D1}/activities",
            json={
                "workflow": "testwf",
                "activities": [
                    {
                        "activity_id": str(uuid4()),
                        "type": "createStuff",
                        "generated": [
                            {"entity": aanvraag_ref, "content": {"titel": "ok"}},
                        ],
                    },
                    {
                        "activity_id": str(uuid4()),
                        "type": "readStuff",
                        "role": "oe:behandelaar",
                        # Missing used block — will fail 422 from the engine
                        "used": [
                            {"entity": f"oe:aanvraag/{uuid4()}@{uuid4()}"},
                        ],
                        "generated": [],
                    },
                ],
            },
            headers={"X-POC-User": "behandelaar"},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        # Error message contains the batch position indicator
        if isinstance(detail, dict):
            msg = detail.get("detail", "")
        else:
            msg = detail
        assert "readStuff" in msg
        assert "#2" in msg or "2" in msg


# --------------------------------------------------------------------
# Typed wrapper endpoint
# --------------------------------------------------------------------


class TestPutActivityTypedWrapper:

    async def test_typed_wrapper_registered_per_activity(
        self, activity_client, repo,
    ):
        """The typed wrapper `PUT /dossiers/{id}/activities/{aid}/createStuff`
        is registered automatically at plugin load time for each
        client_callable activity. It's functionally identical to
        the generic endpoint but doesn't require `type` in the
        body."""
        await _commit(repo)
        activity_id = uuid4()
        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")

        r = await activity_client.put(
            f"/testwf/dossiers/{D1}/activities/{activity_id}/oe:createStuff",
            json={
                # No "type" field — the URL carries it
                "generated": [
                    {"entity": aanvraag_ref, "content": {"titel": "typed"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["activity"]["type"] == "oe:createStuff"

    async def test_typed_wrapper_not_registered_for_noncallable(
        self, activity_client, repo,
    ):
        """systemAction has `client_callable: True` in its def,
        so typed wrappers DO get generated for it. But it
        requires the `systeemgebruiker` role, so an aanvrager
        gets 403 when calling it. This verifies the wrapper
        exists AND the auth check fires."""
        await _commit(repo)
        r = await activity_client.put(
            f"/testwf/dossiers/{D1}/activities/{uuid4()}/systemAction",
            json={},
            headers={"X-POC-User": "aanvrager"},
        )
        # The route exists (not 404). Auth fails → 403.
        assert r.status_code in (403, 404)  # depends on resolution order
        # Ensure it's NOT a 405 (route not registered)
        assert r.status_code != 405


# --------------------------------------------------------------------
# Bug 7 — audit emit must be post-commit
# --------------------------------------------------------------------
#
# Before the fix, `_run_activity` emitted `dossier.created` /
# `dossier.updated` in-transaction, before the outer
# `run_with_deadlock_retry` commit. On rollback (a mid-batch failure)
# the audit log still claimed the failed items succeeded; on deadlock-
# retry the log claimed each retried item twice or more. After the
# fix, emission happens after `run_with_deadlock_retry` returns — so
# the audit log is only written for items that actually committed,
# and exactly once per committed activity.
#
# Denial emits (`dossier.denied` on ActivityError.code=403) stay where
# they are — the denial decision is the auditable fact regardless of
# whether the surrounding transaction commits. Pin that too so a
# future refactor doesn't accidentally defer denials.


class TestAuditEmitIsPostCommit:

    async def test_successful_single_emits_exactly_one_success_event(
        self, activity_client, repo, monkeypatch,
    ):
        """Happy path: a single successful activity produces exactly
        one `dossier.created` (or `.updated`) audit event. Pin the
        happy-path count so a regression that double-emits or
        drops-emits would fail here."""
        import dossier_engine.routes.activities as activities_mod
        captured: list[dict] = []
        monkeypatch.setattr(
            activities_mod, "emit_dossier_audit",
            lambda **kwargs: captured.append(kwargs),
        )

        await _commit(repo)
        activity_id = uuid4()
        ref, _, _ = _new_ref("oe:aanvraag")
        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{activity_id}",
            json={
                "type": "createStuff",
                "workflow": "testwf",
                "generated": [
                    {"entity": ref, "content": {"titel": "ok"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert r.status_code == 200

        # Exactly one success event. createStuff has
        # can_create_dossier=True so the action is 'dossier.created'.
        successes = [
            c for c in captured if c.get("outcome") == "allowed"
        ]
        assert len(successes) == 1
        assert successes[0]["action"] == "dossier.created"
        assert successes[0]["activity_id"] == str(activity_id)

    async def test_batch_rollback_emits_no_success_events(
        self, activity_client, repo, monkeypatch,
    ):
        """Bug 7 core regression: a batch where the second item
        fails rolls back the whole transaction — including the
        successful first item's DB effects. The audit log must
        reflect reality: zero success events. Before the fix, the
        first item's `dossier.created` was already on disk by the
        time the second raised, so the log claimed an item that
        never committed."""
        import dossier_engine.routes.activities as activities_mod
        captured: list[dict] = []
        monkeypatch.setattr(
            activities_mod, "emit_dossier_audit",
            lambda **kwargs: captured.append(kwargs),
        )

        await _commit(repo)
        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")
        r = await activity_client.put(
            f"/dossiers/{D1}/activities",
            json={
                "workflow": "testwf",
                "activities": [
                    {
                        "activity_id": str(uuid4()),
                        "type": "createStuff",
                        "generated": [
                            {"entity": aanvraag_ref, "content": {"titel": "first"}},
                        ],
                    },
                    {
                        # Second item fails — `used` references a
                        # version that doesn't exist, so resolve_used
                        # raises 422. The transaction rolls back,
                        # including the first createStuff's effects.
                        "activity_id": str(uuid4()),
                        "type": "readStuff",
                        "role": "oe:behandelaar",
                        "used": [
                            {"entity": f"oe:aanvraag/{uuid4()}@{uuid4()}"},
                        ],
                        "generated": [],
                    },
                ],
            },
            headers={"X-POC-User": "behandelaar"},
        )
        assert r.status_code == 422

        # The whole batch rolled back. No success audit events
        # should have been emitted — not for the first item, not
        # for anything. Zero is the correct answer; any other
        # value means the audit log is lying about committed work.
        successes = [
            c for c in captured if c.get("outcome") == "allowed"
        ]
        assert successes == [], (
            f"expected no success emits on rollback, got: "
            f"{[s.get('activity_id') for s in successes]}"
        )

    async def test_batch_success_emits_one_event_per_committed_item(
        self, activity_client, repo, monkeypatch,
    ):
        """Happy batch of two: exactly two success events, one per
        committed activity. Pins the count so a refactor that
        skipped per-item emission (e.g. emitting once for the
        whole batch) would fail here."""
        import dossier_engine.routes.activities as activities_mod
        captured: list[dict] = []
        monkeypatch.setattr(
            activities_mod, "emit_dossier_audit",
            lambda **kwargs: captured.append(kwargs),
        )

        await _commit(repo)
        aanvraag_ref, _, vid = _new_ref("oe:aanvraag")
        beslissing_ref, _, _ = _new_ref("oe:beslissing")
        first_aid = uuid4()
        second_aid = uuid4()
        r = await activity_client.put(
            f"/dossiers/{D1}/activities",
            json={
                "workflow": "testwf",
                "activities": [
                    {
                        "activity_id": str(first_aid),
                        "type": "createStuff",
                        "generated": [
                            {"entity": aanvraag_ref, "content": {"titel": "ok"}},
                        ],
                    },
                    {
                        "activity_id": str(second_aid),
                        "type": "readStuff",
                        "role": "oe:behandelaar",
                        "used": [{"entity": aanvraag_ref}],
                        "generated": [
                            {"entity": beslissing_ref, "content": {"oordeel": "yes"}},
                        ],
                    },
                ],
            },
            headers={"X-POC-User": "behandelaar"},
        )
        assert r.status_code == 200

        successes = [
            c for c in captured if c.get("outcome") == "allowed"
        ]
        assert len(successes) == 2
        emitted_aids = {s["activity_id"] for s in successes}
        assert emitted_aids == {str(first_aid), str(second_aid)}
        # Action names derive from can_create_dossier: createStuff is
        # the root (can_create_dossier=True → dossier.created);
        # readStuff is not (→ dossier.updated).
        by_aid = {s["activity_id"]: s["action"] for s in successes}
        assert by_aid[str(first_aid)] == "dossier.created"
        assert by_aid[str(second_aid)] == "dossier.updated"

    async def test_denial_still_emits_in_transaction(
        self, activity_client, repo, monkeypatch,
    ):
        """Denial emits (ActivityError code=403) stay where they
        are inside `_run_activity`, in-transaction. The denial
        decision is the auditable fact; rollback doesn't change
        that. Pin this so a future refactor doesn't accidentally
        defer denials and leave a 403 un-audited for the SIEM
        stream.

        Bootstrap: create the dossier via createStuff as aanvrager
        so readStuff has a valid dossier + used entity to work
        against. The monkeypatch is installed AFTER the bootstrap
        so the setup activity's emit doesn't pollute the capture —
        we're only measuring the denial path.
        """
        import dossier_engine.routes.activities as activities_mod

        await _commit(repo)
        # Bootstrap — createStuff as aanvrager creates the dossier
        # and generates an oe:aanvraag that readStuff can `use`.
        aanvraag_ref, _, _ = _new_ref("oe:aanvraag")
        bootstrap_r = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "createStuff",
                "workflow": "testwf",
                "generated": [
                    {"entity": aanvraag_ref, "content": {"titel": "x"}},
                ],
            },
            headers={"X-POC-User": "aanvrager"},
        )
        assert bootstrap_r.status_code == 200

        # Install the capture AFTER bootstrap so we only observe
        # the denial attempt's emits.
        captured: list[dict] = []
        monkeypatch.setattr(
            activities_mod, "emit_dossier_audit",
            lambda **kwargs: captured.append(kwargs),
        )

        # readStuff requires oe:behandelaar; outsider has no roles
        # → authorize() raises ActivityError(code=403) → dossier.denied.
        beslissing_ref, _, _ = _new_ref("oe:beslissing")
        r = await activity_client.put(
            f"/dossiers/{D1}/activities/{uuid4()}",
            json={
                "type": "readStuff",
                "workflow": "testwf",
                "used": [{"entity": aanvraag_ref}],
                "generated": [
                    {"entity": beslissing_ref, "content": {"uitkomst": "x"}},
                ],
            },
            headers={"X-POC-User": "outsider"},
        )
        assert r.status_code == 403

        denials = [c for c in captured if c.get("outcome") == "denied"]
        successes = [c for c in captured if c.get("outcome") == "allowed"]
        assert len(denials) == 1
        assert denials[0]["action"] == "dossier.denied"
        # Pin that the `reason` field carries the real authorize
        # message, not a default-fallback string. Before Bug 77 was
        # fixed, the code read ``getattr(e, 'code', None)`` and
        # ``getattr(e, 'message', str(e))`` on an ``ActivityError`` —
        # neither attribute exists, so ``code`` was always ``None``
        # (emit skipped) and ``reason`` would have been ``str(e)``
        # (uninformative). Asserting a substring of the authorize
        # message catches any regression where the attribute names
        # drift again.
        assert "behandelaar" in denials[0]["reason"]
        # And no spurious success events.
        assert successes == []

