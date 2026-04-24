"""Unit tests for the ``set_dossier_access`` handler.

These tests exercise the handler in isolation — no database, no engine
pipeline. We fake the ``ActivityContext`` just enough to return the
entities the handler asks for (``get_typed``, ``get_singleton_typed``,
``get_entities_latest``), then inspect the ``HandlerResult.content``.

The handler builds the ``oe:dossier_access`` entity's content — a list
of access entries under the ``access`` key. Each entry grants a role
a view into a set of entity types with a specific activity-visibility
mode. The refactor that landed in round 11 extracted the hard-coded
view lists and role-string formats into module-level constants and
helpers; these tests lock down the behaviour so a future edit to a
constant doesn't silently change who can see what.

What's deliberately *not* tested here:

* The engine-side interpretation of the emitted content
  (``check_dossier_access`` in the engine's routes) — covered by
  ``dossier_engine_repo/tests/``.
* Whether ``set_dossier_access`` fires at the right moment in the
  workflow — that's a workflow.yaml + engine-side concern, covered
  by the integration and shell-spec tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dossier_toelatingen.entities import (
    Aanvraag, Aanvrager, VerantwoordelijkeOrganisatie,
)
from dossier_toelatingen.handlers import (
    _AANVRAGER_VIEW, _BEHANDELAAR_VIEW, _BEHEERDER_VIEW,
    set_dossier_access,
)


# ---------------------------------------------------------------------------
# Fake ActivityContext
# ---------------------------------------------------------------------------
#
# The handler only uses three methods on the context:
#   * ``get_typed(entity_type)``             — sync
#   * ``get_singleton_typed(entity_type)``   — async
#   * ``get_entities_latest(entity_type)``   — async, returns list of rows
#
# A plain SimpleNamespace carrying closures is enough; no subclass of
# the real ActivityContext. This keeps the test decoupled from the
# engine's internals — if the real ActivityContext grows new methods,
# we don't have to update these fixtures.


class _FakeContext:
    def __init__(
        self,
        *,
        aanvraag: Aanvraag | None = None,
        verantw: VerantwoordelijkeOrganisatie | None = None,
        behandelaar_rows: list[SimpleNamespace] | None = None,
    ):
        self._aanvraag = aanvraag
        self._verantw = verantw
        self._behandelaars = behandelaar_rows or []

    def get_typed(self, entity_type: str):
        if entity_type == "oe:aanvraag":
            return self._aanvraag
        return None

    async def get_singleton_typed(self, entity_type: str):
        if entity_type == "oe:verantwoordelijke_organisatie":
            return self._verantw
        return None

    async def get_entities_latest(self, entity_type: str):
        if entity_type == "oe:behandelaar":
            return list(self._behandelaars)
        return []


def _behandelaar_row(uri: str | None) -> SimpleNamespace:
    """The handler reads ``row.content`` as a dict. The real EntityRow
    is a SQLAlchemy model but the handler doesn't care about any other
    attribute, so a SimpleNamespace with a ``.content`` dict is a
    faithful fake."""
    return SimpleNamespace(content={"uri": uri} if uri is not None else {})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBeheerderAlwaysPresent:
    """The beheerder entry is unconditional — it's the break-glass
    access grant. Even an empty dossier (no aanvraag, no organisatie,
    no behandelaars) still grants beheerder."""

    async def test_empty_dossier_still_has_beheerder(self):
        ctx = _FakeContext()
        result = await set_dossier_access(ctx, content=None)

        entries = result.generated[0]["content"]["access"]
        roles = [e["role"] for e in entries]
        assert roles == ["beheerder"]

        beheerder = entries[0]
        assert beheerder["view"] == _BEHEERDER_VIEW
        assert beheerder["activity_view"] == "all"

    async def test_beheerder_sees_oe_dossier_access(self):
        """Beheerder's view is the only one that includes
        ``oe:dossier_access`` — that's the "who can see who can see
        what" bit and it's specific to this role."""
        ctx = _FakeContext()
        result = await set_dossier_access(ctx, content=None)

        beheerder = result.generated[0]["content"]["access"][0]
        assert "oe:dossier_access" in beheerder["view"]
        # And no other role gets it — _BEHANDELAAR_VIEW lacks it.
        assert "oe:dossier_access" not in _BEHANDELAAR_VIEW
        assert "oe:dossier_access" not in _AANVRAGER_VIEW


class TestAanvragerAccess:
    """Aanvrager access is granted via ``kbo`` or ``rrn`` — exactly
    one of the two per aanvrager (the Aanvrager model enforces XOR),
    so each dossier produces exactly one aanvrager entry."""

    async def test_kbo_aanvrager_emits_kbo_role(self):
        aanvraag = Aanvraag(
            onderwerp="x", handeling="aanvraag",
            aanvrager=Aanvrager(kbo="0123456789"),
            gemeente="Brugge", object="https://id.erfgoed.net/erfgoedobjecten/1",
        )
        ctx = _FakeContext(aanvraag=aanvraag)
        result = await set_dossier_access(ctx, content=None)

        aanvrager_entry = next(
            e for e in result.generated[0]["content"]["access"]
            if e["role"].startswith("kbo-toevoeger:")
        )
        assert aanvrager_entry["role"] == "kbo-toevoeger:0123456789"
        assert aanvrager_entry["view"] == _AANVRAGER_VIEW
        assert aanvrager_entry["activity_view"] == "own"

    async def test_rrn_aanvrager_emits_bare_rrn_as_role(self):
        """The rrn itself is the role string — no prefix. Kept behind
        the ``_rrn_role`` helper but the current contract is bare."""
        aanvraag = Aanvraag(
            onderwerp="x", handeling="aanvraag",
            aanvrager=Aanvrager(rrn="12345678901"),
            gemeente="Brugge", object="https://id.erfgoed.net/erfgoedobjecten/1",
        )
        ctx = _FakeContext(aanvraag=aanvraag)
        result = await set_dossier_access(ctx, content=None)

        rrn_entry = next(
            e for e in result.generated[0]["content"]["access"] if e["role"] == "12345678901"
        )
        assert rrn_entry["view"] == _AANVRAGER_VIEW
        assert rrn_entry["activity_view"] == "own"

    async def test_aanvrager_view_has_no_duplicate_external(self):
        """Round-11 bug fix: the inline view lists used to contain
        ``"external"`` twice in the aanvrager slots (copy-paste error,
        inert but confusing). After extracting the shared constant,
        each element appears exactly once."""
        aanvraag = Aanvraag(
            onderwerp="x", handeling="aanvraag",
            aanvrager=Aanvrager(kbo="0123456789"),
            gemeente="Brugge", object="https://id.erfgoed.net/erfgoedobjecten/1",
        )
        ctx = _FakeContext(aanvraag=aanvraag)
        result = await set_dossier_access(ctx, content=None)

        aanvrager_entry = next(
            e for e in result.generated[0]["content"]["access"]
            if e["role"].startswith("kbo-toevoeger:")
        )
        view = aanvrager_entry["view"]
        assert len(view) == len(set(view)), (
            f"Aanvrager view contains duplicates: {view}"
        )


class TestVerantwoordelijkeOrganisatieAccess:
    async def test_organisatie_gets_gemeente_toevoeger_role(self):
        verantw = VerantwoordelijkeOrganisatie(
            uri="https://id.erfgoed.net/organisaties/brugge",
        )
        ctx = _FakeContext(verantw=verantw)
        result = await set_dossier_access(ctx, content=None)

        org_entry = next(
            e for e in result.generated[0]["content"]["access"]
            if e["role"].startswith("gemeente-toevoeger:")
        )
        assert org_entry["role"] == (
            "gemeente-toevoeger:https://id.erfgoed.net/organisaties/brugge"
        )
        assert org_entry["view"] == _BEHANDELAAR_VIEW
        assert org_entry["activity_view"] == "all"


class TestBehandelaarAccess:
    """Behandelaar access runs on two axes:

    1. Per-URI — each behandelaar's URI is itself a role string.
    2. Bare ``"behandelaar"`` — the global staff role, present iff
       at least one behandelaar exists.

    The two populations are independent — a user can have only the
    URI in their roles (identity-scoped) or only the bare role
    (global staff) or both."""

    async def test_no_behandelaars_no_entries(self):
        """When nothing is attached, neither the per-URI entries nor
        the bare ``"behandelaar"`` entry should appear."""
        ctx = _FakeContext(behandelaar_rows=[])
        result = await set_dossier_access(ctx, content=None)

        roles = [e["role"] for e in result.generated[0]["content"]["access"]]
        assert "behandelaar" not in roles
        assert roles == ["beheerder"]

    async def test_single_behandelaar_yields_uri_and_bare_entries(self):
        """One behandelaar → two entries: one with the URI as role,
        one with the bare ``"behandelaar"`` role. Both granting the
        same view."""
        ctx = _FakeContext(
            behandelaar_rows=[_behandelaar_row("https://example.test/u/alice")],
        )
        result = await set_dossier_access(ctx, content=None)

        roles = [e["role"] for e in result.generated[0]["content"]["access"]]
        assert "https://example.test/u/alice" in roles
        assert "behandelaar" in roles

        for role_name in ("https://example.test/u/alice", "behandelaar"):
            entry = next(e for e in result.generated[0]["content"]["access"] if e["role"] == role_name)
            assert entry["view"] == _BEHANDELAAR_VIEW
            assert entry["activity_view"] == "all"

    async def test_multiple_behandelaars_one_entry_per_uri_plus_one_bare(self):
        ctx = _FakeContext(behandelaar_rows=[
            _behandelaar_row("https://example.test/u/alice"),
            _behandelaar_row("https://example.test/u/bob"),
            _behandelaar_row("https://example.test/u/carol"),
        ])
        result = await set_dossier_access(ctx, content=None)

        roles = [e["role"] for e in result.generated[0]["content"]["access"]]
        uri_roles = [r for r in roles if r.startswith("https://")]
        assert set(uri_roles) == {
            "https://example.test/u/alice",
            "https://example.test/u/bob",
            "https://example.test/u/carol",
        }
        assert roles.count("behandelaar") == 1

    async def test_duplicate_uri_is_deduplicated(self):
        """Repeated handler invocations can attach the same behandelaar
        twice (different entity rows, same URI). We emit one entry per
        URI — matching rows are merged."""
        ctx = _FakeContext(behandelaar_rows=[
            _behandelaar_row("https://example.test/u/alice"),
            _behandelaar_row("https://example.test/u/alice"),
            _behandelaar_row("https://example.test/u/alice"),
        ])
        result = await set_dossier_access(ctx, content=None)

        roles = [e["role"] for e in result.generated[0]["content"]["access"]]
        assert roles.count("https://example.test/u/alice") == 1
        # Bare role still appears exactly once.
        assert roles.count("behandelaar") == 1

    async def test_row_without_uri_is_skipped(self):
        """A behandelaar row with no ``uri`` field in its content is a
        malformed state (shouldn't happen in production) — we skip it
        rather than emitting an empty role, because an empty string in
        ``access.role`` would silently match any user whose
        ``user.roles`` list also contains an empty string."""
        ctx = _FakeContext(behandelaar_rows=[
            SimpleNamespace(content={}),
            SimpleNamespace(content=None),
            _behandelaar_row("https://example.test/u/alice"),
        ])
        result = await set_dossier_access(ctx, content=None)

        roles = [e["role"] for e in result.generated[0]["content"]["access"]]
        assert "" not in roles
        assert None not in roles
        assert "https://example.test/u/alice" in roles
        # The bare role still fires because at least one behandelaar row
        # existed — independent of whether any URI was valid.
        assert "behandelaar" in roles


class TestFullDossier:
    """End-to-end shape check on a realistic full dossier: aanvraag
    with kbo, verantwoordelijke organisatie, two behandelaars."""

    async def test_full_dossier_all_expected_roles(self):
        aanvraag = Aanvraag(
            onderwerp="test", handeling="aanvraag",
            aanvrager=Aanvrager(kbo="0123456789"),
            gemeente="Brugge", object="https://id.erfgoed.net/erfgoedobjecten/1",
        )
        verantw = VerantwoordelijkeOrganisatie(
            uri="https://id.erfgoed.net/organisaties/brugge",
        )
        ctx = _FakeContext(
            aanvraag=aanvraag,
            verantw=verantw,
            behandelaar_rows=[
                _behandelaar_row("https://example.test/u/alice"),
                _behandelaar_row("https://example.test/u/bob"),
            ],
        )
        result = await set_dossier_access(ctx, content=None)

        roles = [e["role"] for e in result.generated[0]["content"]["access"]]
        assert set(roles) == {
            "kbo-toevoeger:0123456789",
            "gemeente-toevoeger:https://id.erfgoed.net/organisaties/brugge",
            "https://example.test/u/alice",
            "https://example.test/u/bob",
            "behandelaar",
            "beheerder",
        }


class TestViewConstants:
    """The extracted view-list constants are the single source of truth
    — any future new entity type that should be visible to a role has
    to be added to exactly one of these lists. Lock the shape so a
    missed update is a failing test, not a silent visibility gap."""

    def test_aanvrager_view_includes_own_documents(self):
        """Aanvragers must see their own application + decision +
        signature, and any external references their dossier touches."""
        assert set(_AANVRAGER_VIEW) == {
            "oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
        }

    def test_behandelaar_view_supersets_aanvrager_view(self):
        """Staff-role views must include everything aanvragers can see
        — if we add a new aanvrager-visible type, staff must still see
        it too."""
        assert set(_AANVRAGER_VIEW) <= set(_BEHANDELAAR_VIEW)

    def test_behandelaar_view_adds_staff_types(self):
        staff_extras = set(_BEHANDELAAR_VIEW) - set(_AANVRAGER_VIEW)
        assert staff_extras == {
            "oe:verantwoordelijke_organisatie",
            "oe:behandelaar",
            "oe:system_fields",
            "system:task",
        }

    def test_beheerder_view_is_behandelaar_plus_dossier_access(self):
        assert set(_BEHEERDER_VIEW) == set(_BEHANDELAAR_VIEW) | {"oe:dossier_access"}
