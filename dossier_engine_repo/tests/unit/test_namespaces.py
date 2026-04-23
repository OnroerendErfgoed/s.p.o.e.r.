"""
Tests for the namespace registry and mixed-ontology support.
"""

from __future__ import annotations

import pytest
from uuid import UUID

from dossier_engine.engine.refs import EntityRef
from dossier_engine.prov.namespaces import (
    NamespaceRegistry,
    namespaces,
    set_namespaces,
    reset_namespaces,
)


class TestNamespaceRegistry:
    """The registry stores prefix → IRI mappings and validates types."""

    def test_builtin_prefixes_available(self):
        reg = NamespaceRegistry()
        assert "prov" in reg
        assert "xsd" in reg
        assert "rdf" in reg
        assert "rdfs" in reg
        assert reg.iri_for("prov") == "http://www.w3.org/ns/prov#"

    def test_register_new_prefix(self):
        reg = NamespaceRegistry()
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")
        assert "foaf" in reg
        assert reg.iri_for("foaf") == "http://xmlns.com/foaf/0.1/"

    def test_cannot_override_builtin(self):
        reg = NamespaceRegistry()
        with pytest.raises(ValueError, match="Cannot override built-in"):
            reg.register("prov", "http://evil.com/")

    def test_cannot_rebind_to_different_iri(self):
        reg = NamespaceRegistry()
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")
        with pytest.raises(ValueError, match="refusing to rebind"):
            reg.register("foaf", "http://other.com/foaf/")

    def test_same_iri_rebind_is_idempotent(self):
        reg = NamespaceRegistry()
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")
        # Same IRI again — no error
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")

    def test_iri_must_end_with_separator(self):
        reg = NamespaceRegistry()
        with pytest.raises(ValueError, match="should end with"):
            reg.register("bad", "http://example.com/vocab")  # no trailing / or #

    def test_expand_qualified_name(self):
        reg = NamespaceRegistry()
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")
        assert reg.expand("foaf:Person") == "http://xmlns.com/foaf/0.1/Person"

    def test_expand_unqualified_uses_default(self):
        reg = NamespaceRegistry()
        reg.register("oe", "https://id.erfgoed.net/vocab/ontology#")
        reg.default_workflow_prefix = "oe"
        assert reg.expand("aanvraag") == "https://id.erfgoed.net/vocab/ontology#aanvraag"

    def test_validate_known_prefix_passes(self):
        reg = NamespaceRegistry()
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")
        reg.validate_type("foaf:Person")  # no raise

    def test_validate_unknown_prefix_raises(self):
        reg = NamespaceRegistry()
        with pytest.raises(ValueError, match="Unknown namespace prefix"):
            reg.validate_type("schema:CreativeWork")

    def test_validate_unqualified_always_passes(self):
        reg = NamespaceRegistry()
        reg.validate_type("aanvraag")  # no colon, no validation


class TestNamespaceSingleton:
    """The module-level namespaces() accessor raises if not configured."""

    def test_raises_when_unconfigured(self):
        reset_namespaces()
        with pytest.raises(RuntimeError, match="not configured"):
            namespaces()

    def test_set_and_get(self):
        reset_namespaces()
        reg = NamespaceRegistry()
        reg.register("foaf", "http://xmlns.com/foaf/0.1/")
        set_namespaces(reg)
        assert namespaces() is reg


class TestEntityRefMixedOntologies:
    """EntityRef.parse accepts RDF-compliant qualified names."""

    _UUID = "e1000000-0000-0000-0000-000000000001"
    _VUUID = "f1000000-0000-0000-0000-000000000001"

    def test_foaf_person(self):
        parsed = EntityRef.parse(f"foaf:Person/{self._UUID}@{self._VUUID}")
        assert parsed is not None
        assert parsed.type == "foaf:Person"

    def test_dcterms_bibliographic_resource(self):
        parsed = EntityRef.parse(f"dcterms:BibliographicResource/{self._UUID}@{self._VUUID}")
        assert parsed is not None
        assert parsed.type == "dcterms:BibliographicResource"

    def test_schema_creative_work(self):
        parsed = EntityRef.parse(f"schema:CreativeWork/{self._UUID}@{self._VUUID}")
        assert parsed is not None
        assert parsed.type == "schema:CreativeWork"

    def test_hyphenated_local_name(self):
        parsed = EntityRef.parse(f"org:Organisation-Unit/{self._UUID}@{self._VUUID}")
        assert parsed is not None
        assert parsed.type == "org:Organisation-Unit"

    def test_local_name_with_digits(self):
        parsed = EntityRef.parse(f"schema:Foo2Bar/{self._UUID}@{self._VUUID}")
        assert parsed is not None
        assert parsed.type == "schema:Foo2Bar"

    def test_leading_digit_prefix_rejected(self):
        """Prefixes must start with a letter per RDF QName rules."""
        assert EntityRef.parse(f"1foaf:Person/{self._UUID}@{self._VUUID}") is None

    def test_leading_digit_local_rejected(self):
        """Local names must start with a letter per RDF QName rules."""
        assert EntityRef.parse(f"foaf:1Person/{self._UUID}@{self._VUUID}") is None

    def test_double_colon_rejected(self):
        """Only one colon allowed between prefix and local name."""
        assert EntityRef.parse(f"a:b:c/{self._UUID}@{self._VUUID}") is None


class TestEntityRefFullIRI:
    """EntityRef.parse accepts full platform IRIs as input."""

    _UUID = "e1000000-0000-0000-0000-000000000001"
    _VUUID = "f1000000-0000-0000-0000-000000000001"
    _DID = "d1000000-0000-0000-0000-000000000001"

    def test_full_iri_parses_to_same_as_shorthand(self):
        """Shorthand and full IRI forms produce identical EntityRefs."""
        short = f"oe:aanvraag/{self._UUID}@{self._VUUID}"
        full = (
            f"https://id.erfgoed.net/dossiers/{self._DID}/"
            f"entities/oe:aanvraag/{self._UUID}/{self._VUUID}"
        )
        r_short = EntityRef.parse(short)
        r_full = EntityRef.parse(full)
        assert r_short == r_full

    def test_full_iri_with_foaf_type(self):
        full = (
            f"https://id.erfgoed.net/dossiers/{self._DID}/"
            f"entities/foaf:Person/{self._UUID}/{self._VUUID}"
        )
        r = EntityRef.parse(full)
        assert r is not None
        assert r.type == "foaf:Person"
        assert str(r.entity_id) == self._UUID

    def test_non_platform_https_returns_none(self):
        """External URIs that aren't our platform remain external."""
        assert EntityRef.parse("https://id.erfgoed.net/erfgoedobjecten/10001") is None
        assert EntityRef.parse("https://example.com/thing/123") is None

    def test_platform_iri_without_entities_segment_returns_none(self):
        """The dossier IRI itself isn't an entity."""
        assert EntityRef.parse(f"https://id.erfgoed.net/dossiers/{self._DID}/") is None

    def test_malformed_platform_iri_returns_none(self):
        """Short path (missing version) returns None."""
        full = (
            f"https://id.erfgoed.net/dossiers/{self._DID}/"
            f"entities/oe:aanvraag/{self._UUID}"  # missing /vid
        )
        assert EntityRef.parse(full) is None

    def test_bad_uuid_in_iri_returns_none(self):
        """Non-UUID path segments fail gracefully."""
        full = (
            f"https://id.erfgoed.net/dossiers/{self._DID}/"
            f"entities/oe:aanvraag/not-a-uuid/also-not-a-uuid"
        )
        assert EntityRef.parse(full) is None
