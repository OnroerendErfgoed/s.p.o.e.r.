"""
Integration tests for workflow-scoped reference data and validation
endpoints.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from dossier_engine.routes import register_routes
from dossier_engine.auth import POCAuthMiddleware


def _build_toelatingen_app() -> FastAPI:
    """Build a FastAPI app with the real toelatingen plugin loaded."""
    from dossier_engine.plugin import PluginRegistry
    import dossier_toelatingen

    plugin = dossier_toelatingen.create_plugin()
    registry = PluginRegistry()
    registry.register(plugin)

    auth = POCAuthMiddleware(plugin.workflow.get("poc_users", []))

    app = FastAPI()
    app.state.registry = registry
    app.state.config = {"file_service": {"url": "http://test", "signing_key": "k"}}
    register_routes(app, registry, auth, global_access=[])
    return app


@pytest_asyncio.fixture
async def activity_client():
    """AsyncClient wired to the toelatingen app."""
    app = _build_toelatingen_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        yield c


class TestReferenceData:

    async def test_get_all_reference_data(self, activity_client):
        """GET /{workflow}/reference returns all lists."""
        r = await activity_client.get("/toelatingen/reference")
        assert r.status_code == 200
        body = r.json()
        assert "bijlagetypes" in body
        assert "handelingen" in body
        assert "beslissingstypes" in body
        assert "gemeenten" in body

    async def test_get_single_list(self, activity_client):
        """GET /{workflow}/reference/{name} returns one list."""
        r = await activity_client.get("/toelatingen/reference/bijlagetypes")
        assert r.status_code == 200
        body = r.json()
        items = body["items"]
        assert len(items) > 0
        keys = [i["key"] for i in items]
        assert "foto" in keys
        assert "detailplan" in keys

    async def test_get_gemeenten(self, activity_client):
        """Gemeenten reference data includes nis_code."""
        r = await activity_client.get("/toelatingen/reference/gemeenten")
        assert r.status_code == 200
        items = r.json()["items"]
        brugge = next(i for i in items if i["key"] == "brugge")
        assert brugge["nis_code"] == "31005"

    async def test_unknown_list_returns_404(self, activity_client):
        """GET /{workflow}/reference/{bad} returns 404 with available names."""
        r = await activity_client.get("/toelatingen/reference/nonexistent")
        assert r.status_code == 404
        assert "Available" in r.json()["detail"]

    async def test_unknown_workflow_returns_404(self, activity_client):
        """GET /{bad_workflow}/reference returns 404."""
        r = await activity_client.get("/nonexistent/reference")
        assert r.status_code == 404


class TestValidation:

    # Any authenticated user of any role can call these endpoints
    # (Bug 58 decision — auth is for attack-surface reduction, not
    # RBAC). Pick one of the toelatingen plugin's poc_users; the
    # exact role doesn't matter.
    _AUTH = {"X-POC-User": "claeyswo"}

    async def test_list_validators(self, activity_client):
        """GET /{workflow}/validate lists registered validators."""
        r = await activity_client.get(
            "/toelatingen/validate", headers=self._AUTH,
        )
        assert r.status_code == 200
        names = r.json()["validators"]
        assert "erfgoedobject" in names
        assert "handeling" in names

    async def test_validate_erfgoedobject_valid(self, activity_client):
        """POST /{workflow}/validate/erfgoedobject — known URI."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={"uri": "https://id.erfgoed.net/erfgoedobjecten/10001"},
            headers=self._AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert body["label"] == "Stadhuis Brugge"
        assert body["type"] == "monument"
        assert body["gemeente"] == "Brugge"

    async def test_validate_erfgoedobject_invalid(self, activity_client):
        """POST /{workflow}/validate/erfgoedobject — unknown URI."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={"uri": "https://id.erfgoed.net/erfgoedobjecten/99999"},
            headers=self._AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert "niet gevonden" in body["error"]

    async def test_validate_erfgoedobject_bad_format(self, activity_client):
        """POST /{workflow}/validate/erfgoedobject — wrong URI scheme."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={"uri": "http://example.com/something"},
            headers=self._AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert "Ongeldig formaat" in body["error"]

    async def test_validate_handeling_valid(self, activity_client):
        """POST /{workflow}/validate/handeling — allowed combo."""
        r = await activity_client.post(
            "/toelatingen/validate/handeling",
            json={
                "erfgoedobject_uri": "https://id.erfgoed.net/erfgoedobjecten/10001",
                "handeling": "restauratie",
            },
            headers=self._AUTH,
        )
        assert r.status_code == 200
        assert r.json()["valid"] is True

    async def test_validate_handeling_invalid(self, activity_client):
        """POST /{workflow}/validate/handeling — disallowed combo."""
        r = await activity_client.post(
            "/toelatingen/validate/handeling",
            json={
                "erfgoedobject_uri": "https://id.erfgoed.net/erfgoedobjecten/20001",
                "handeling": "sloop_deel",
            },
            headers=self._AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert "niet toegelaten" in body["error"]
        assert "landschap" in body["error"]

    async def test_unknown_validator_returns_404(self, activity_client):
        """POST /{workflow}/validate/{bad} returns 404."""
        r = await activity_client.post(
            "/toelatingen/validate/nonexistent",
            json={},
            headers=self._AUTH,
        )
        assert r.status_code == 404

    async def test_validate_missing_fields(self, activity_client):
        """POST with empty body returns 422 — the Pydantic model
        catches the missing required field before our validator runs."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={},
            headers=self._AUTH,
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        # FastAPI's validation error includes the field name.
        assert any("uri" in str(e.get("loc", "")) for e in detail)


class TestValidateRequiresAuth:
    """Bug 58 regression — the validator endpoints were unauthenticated
    before this fix. An unauthenticated caller could hit them to
    enumerate the inventaris URI space (``erfgoedobject``: URI →
    label/type/gemeente) or the allowed-handelingen mapping
    (``handeling``: invalid input surfaces the full allowed-set in
    the error message). Requiring any authenticated session closes
    that surface without adding role-based access control — the
    decision recorded in the round was "authenticated = fine"
    because these are field-level sanity checks any logged-in user
    might legitimately call.

    This class pins the 401 behaviour so a future regression that
    drops the dependency goes red. Reference-data endpoints stay
    public by product decision (see reference.py module docstring)
    — tests for those *not* requiring auth live in
    ``TestReferenceData`` above."""

    async def test_list_validators_without_auth_returns_401(
        self, activity_client,
    ):
        """GET /{workflow}/validate (validator list) requires auth."""
        r = await activity_client.get("/toelatingen/validate")
        assert r.status_code == 401

    async def test_post_validate_without_auth_returns_401(
        self, activity_client,
    ):
        """POST /{workflow}/validate/{name} requires auth. The 401
        fires from the middleware before the Pydantic body model
        validates, so even a malformed body reaches the auth gate
        first — good for the "can't be used as oracle" story."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={"uri": "https://id.erfgoed.net/erfgoedobjecten/10001"},
        )
        assert r.status_code == 401

    async def test_post_validate_without_auth_even_for_bogus_inputs(
        self, activity_client,
    ):
        """Auth must fire regardless of body shape — not after the
        Pydantic validation runs, which would let an attacker
        distinguish "route exists but unauthenticated" (401) from
        "body failed validation" (422) and thereby enumerate
        validator input schemas. Real validator name + empty body
        must 401, not 422.

        Note on what this test does NOT claim: it does not pin 401
        for unknown validator names. FastAPI's route resolution
        happens before middleware, so ``POST /.../nonexistent`` 404s
        before the auth middleware runs. Knowing which validator
        names exist is a weaker disclosure than what Bug 58 targets
        (the oracle behaviour of actually running the validator);
        the ``GET /validate`` endpoint also returns the list for
        authenticated users. Enforcing name-level enumeration
        resistance would require a catch-all handler or a route-
        resolution hack, which is out of scope for Bug 58's
        'authenticated = fine' framing."""
        r = await activity_client.post(
            "/toelatingen/validate/erfgoedobject",
            json={},
        )
        # Real validator + missing body — auth beats Pydantic.
        assert r.status_code == 401

    async def test_reference_stays_public(self, activity_client):
        """Sanity check: the reference endpoints are still callable
        without auth. Guards against a future 'add auth to the
        whole file' refactor that would drag the public reference
        endpoints behind an unnecessary auth gate."""
        r_all = await activity_client.get("/toelatingen/reference")
        assert r_all.status_code == 200
        r_one = await activity_client.get("/toelatingen/reference/bijlagetypes")
        assert r_one.status_code == 200
