"""
Unit tests for the search module — ACL flattening, filter building,
and the no-op path when ES isn't configured.

We don't stand up a real ES for these; the goal is to verify the
pure-function pieces (ACL shape, filter shape, doc shape) and the
fact that every index/search operation is a safe no-op without ES.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest

from dossier_engine.search import (
    build_acl, build_acl_filter, get_client, SearchSettings,
)
from dossier_engine.search.common_index import (
    build_common_doc, recreate_index as recreate_common,
    reindex_all as reindex_common_all,
    search_common,
)


D1 = UUID("11111111-1111-1111-1111-111111111111")


class _FakeUser:
    def __init__(self, id: str, roles: list[str]):
        self.id = id
        self.roles = roles


class TestBuildAcl:
    """ACL flattening: access entries + audit_access list."""

    def test_empty_content_returns_empty_list(self):
        assert build_acl(None) == []
        assert build_acl({}) == []

    def test_roles_flattened(self):
        content = {
            "access": [
                {"role": "behandelaar"},
                {"role": "beheerder"},
            ],
        }
        assert build_acl(content) == ["behandelaar", "beheerder"]

    def test_agents_flattened(self):
        content = {
            "access": [
                {"agents": ["agent-1", "agent-2"]},
                {"agents": ["agent-3"]},
            ],
        }
        assert build_acl(content) == ["agent-1", "agent-2", "agent-3"]

    def test_roles_and_agents_mixed(self):
        content = {
            "access": [
                {"role": "behandelaar", "agents": ["agent-1"]},
                {"role": "beheerder"},
            ],
        }
        # Role comes first (iteration order), agents next.
        assert set(build_acl(content)) == {
            "behandelaar", "agent-1", "beheerder",
        }

    def test_audit_access_included(self):
        """Audit-access roles also appear in __acl__ — they're meant
        to be able to see the dossier."""
        content = {
            "access": [{"role": "behandelaar"}],
            "audit_access": ["beheerder", "auditor"],
        }
        assert set(build_acl(content)) == {
            "behandelaar", "beheerder", "auditor",
        }

    def test_duplicates_removed(self):
        content = {
            "access": [
                {"role": "behandelaar"},
                {"role": "behandelaar"},  # dup
                {"agents": ["agent-1", "agent-1"]},  # dup
            ],
            "audit_access": ["behandelaar"],  # dup with access
        }
        tokens = build_acl(content)
        assert tokens == ["behandelaar", "agent-1"]  # stable order, dedup


    def test_global_access_roles_included(self):
        """Users matching global_access (e.g. beheerder) need to
        appear in every dossier's __acl__ so they find dossiers in
        search. Without this, a user who can open any dossier via
        GET /dossiers/{id} would search and see nothing."""
        content = {"access": [{"role": "behandelaar"}]}
        global_access = [
            {"role": "beheerder", "view": "all"},
            {"role": "systeemgebruiker", "view": "all"},
        ]
        tokens = build_acl(content, global_access)
        assert set(tokens) == {"behandelaar", "beheerder", "systeemgebruiker"}

    def test_global_access_none_safe(self):
        """Passing None for global_access works the same as omitting
        it — no per-call guard needed in callers."""
        content = {"access": [{"role": "behandelaar"}]}
        assert build_acl(content, None) == ["behandelaar"]

    def test_global_access_deduplicates_with_per_dossier(self):
        """A role appearing in both per-dossier access and global_access
        shows up once."""
        content = {"access": [{"role": "beheerder"}]}
        global_access = [{"role": "beheerder", "view": "all"}]
        assert build_acl(content, global_access) == ["beheerder"]


class TestAclFilter:
    """build_acl_filter produces the `terms` clause every search uses."""

    def test_combines_roles_and_user_id(self):
        user = _FakeUser(id="alice-uuid", roles=["behandelaar", "beheerder"])
        filter_clause = build_acl_filter(user)
        assert filter_clause == {
            "terms": {
                "__acl__": ["behandelaar", "beheerder", "alice-uuid"],
            }
        }

    def test_no_roles_just_user_id(self):
        """A user with no roles can still match agent-level ACL
        entries via their user id."""
        user = _FakeUser(id="alice-uuid", roles=[])
        filter_clause = build_acl_filter(user)
        assert filter_clause == {
            "terms": {"__acl__": ["alice-uuid"]}
        }


class TestNoOpWithoutES:
    """Every operation short-circuits when DOSSIER_ES_URL is empty."""

    def test_get_client_returns_none(self, monkeypatch):
        monkeypatch.delenv("DOSSIER_ES_URL", raising=False)
        # Reset the cached client.
        import dossier_engine.search as s
        s._client = None
        assert get_client() is None

    async def test_recreate_common_is_no_op(self, monkeypatch):
        monkeypatch.delenv("DOSSIER_ES_URL", raising=False)
        import dossier_engine.search as s
        s._client = None
        result = await recreate_common()
        assert result["recreated"] is False
        assert "not configured" in result["reason"]

    async def test_search_common_returns_empty(self, monkeypatch):
        monkeypatch.delenv("DOSSIER_ES_URL", raising=False)
        import dossier_engine.search as s
        s._client = None
        user = _FakeUser(id="u1", roles=["behandelaar"])
        result = await search_common(user=user, onderwerp="monument")
        assert result == {
            "hits": [],
            "total": 0,
            "reason": "ES not configured (DOSSIER_ES_URL is empty)",
        }


class TestCommonDoc:
    """build_common_doc produces the shape the mapping expects."""

    def test_doc_shape(self):
        access = {
            "access": [{"role": "behandelaar"}],
            "audit_access": ["beheerder"],
        }
        doc = build_common_doc(D1, "toelatingen", "Renovatie kasteel", access)
        assert doc == {
            "dossier_id": str(D1),
            "workflow": "toelatingen",
            "onderwerp": "Renovatie kasteel",
            "__acl__": ["behandelaar", "beheerder"],
        }

    def test_none_onderwerp_becomes_empty_string(self):
        """ES text fields accept empty strings; None would be a
        nullable value we'd rather not expose in the index."""
        doc = build_common_doc(D1, "toelatingen", None, None)
        assert doc["onderwerp"] == ""
        assert doc["__acl__"] == []


class TestReindexAllPluginDispatch:
    """``reindex_all`` must prefer a plugin-provided
    ``build_common_doc_for_dossier`` over the bare-minimum fallback
    — otherwise every doc in the index ends up with empty onderwerp
    and only global-access roles in ``__acl__``, which makes every
    non-global user invisible from search. That was a real bug in
    production."""

    async def test_uses_plugin_builder_when_present(self):
        """Engine-level reindex calls the plugin's
        build_common_doc_for_dossier so the resulting doc carries
        real onderwerp + full ACL, not the fallback shape."""
        called_with = []

        async def fake_builder(repo, dossier_id):
            called_with.append((repo, dossier_id))
            return {
                "dossier_id": str(dossier_id),
                "workflow": "toelatingen",
                "onderwerp": "Real onderwerp",
                "__acl__": ["behandelaar", "beheerder", "aanvrager_rrn"],
            }

        class _Plugin:
            build_common_doc_for_dossier = staticmethod(fake_builder)

        class _Dossier:
            def __init__(self, dossier_id, workflow):
                self.id = dossier_id
                self.workflow = workflow

        class _FakeESClient:
            def __init__(self):
                self.indexed = []
                self.indices = self  # recreate_index pattern not used here

            async def index(self, index, id, document):
                self.indexed.append({"index": index, "id": id, "doc": document})

            async def refresh(self, index):
                pass

        class _ExecResult:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        class _Session:
            def __init__(self, rows):
                self._rows = rows

            async def execute(self, _query):
                return _ExecResult(self._rows)

        class _Repo:
            def __init__(self, rows):
                self.session = _Session(rows)

        dossiers = [_Dossier(D1, "toelatingen")]
        repo = _Repo(dossiers)
        registry = {"toelatingen": _Plugin()}

        import dossier_engine.search.common_index as common_mod
        original_get_client = common_mod.get_client
        fake_client = _FakeESClient()
        common_mod.get_client = lambda: fake_client
        try:
            result = await common_mod.reindex_all(repo, registry)
        finally:
            common_mod.get_client = original_get_client

        assert result["reindexed"] == 1
        assert result["skipped"] == 0
        assert len(called_with) == 1
        assert called_with[0][1] == D1
        # The indexed doc is the plugin's output, NOT the fallback.
        assert fake_client.indexed[0]["doc"]["onderwerp"] == "Real onderwerp"
        assert "aanvrager_rrn" in fake_client.indexed[0]["doc"]["__acl__"]

    async def test_falls_back_when_plugin_has_no_builder(self):
        """If a plugin hasn't opted into the new builder, reindex_all
        still works — but the doc is sparse. The fallback exists so
        plugins that haven't migrated yet aren't broken outright."""

        class _Plugin:
            pass  # No build_common_doc_for_dossier

        class _Dossier:
            def __init__(self, dossier_id, workflow):
                self.id = dossier_id
                self.workflow = workflow

        class _FakeESClient:
            def __init__(self):
                self.indexed = []
                self.indices = self

            async def index(self, index, id, document):
                self.indexed.append({"index": index, "id": id, "doc": document})

            async def refresh(self, index):
                pass

        class _ExecResult:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        class _Session:
            def __init__(self, rows):
                self._rows = rows

            async def execute(self, _query):
                return _ExecResult(self._rows)

        class _Repo:
            def __init__(self, rows):
                self.session = _Session(rows)

        dossiers = [_Dossier(D1, "legacy_wf")]
        repo = _Repo(dossiers)
        registry = {"legacy_wf": _Plugin()}

        import dossier_engine.search.common_index as common_mod
        original_get_client = common_mod.get_client
        fake_client = _FakeESClient()
        common_mod.get_client = lambda: fake_client
        try:
            result = await common_mod.reindex_all(repo, registry)
        finally:
            common_mod.get_client = original_get_client

        assert result["reindexed"] == 1
        # Fallback doc shape — empty onderwerp, empty ACL.
        doc = fake_client.indexed[0]["doc"]
        assert doc["onderwerp"] == ""
        assert doc["__acl__"] == []
