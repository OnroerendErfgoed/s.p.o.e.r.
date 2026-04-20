"""
Entity reference parsing and construction.

The engine uses a single canonical string format for entity references:

    prefix:type/entity_id@version_id

For example: `oe:aanvraag/e1000000-0000-0000-0000-000000000001@f1000000-0000-0000-0000-000000000001`.

The `prefix:type` part is the entity type (e.g. `oe:aanvraag`, `system:task`,
`foaf:Person`, `dcterms:BibliographicResource`). Both prefix and local name
allow letters, digits, underscores, and hyphens. The first character of each
must be a letter — this matches standard RDF/XML QName conventions and SPARQL
prefixed names.

The `entity_id` is the logical identity — every revision of the same
conceptual entity shares it. The `version_id` is the row identity —
each revision gets its own.

Anything that doesn't match this pattern is treated as an external URI
(e.g. `https://id.erfgoed.net/erfgoedobjecten/10001`). External URIs are
persisted as `type=external` entities so the PROV graph stays complete.

This module is the single source of truth for parsing and constructing
the canonical string form. Callers that need to build a ref from
components use ``EntityRef(...)`` and let its ``__str__`` render; callers
that need to parse a string use ``EntityRef.parse(s)`` which returns
``None`` for external URIs. No f-string concatenation of
``f"{type}/{eid}@{vid}"`` should live outside this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID


# Ref pattern: prefix:LocalName/uuid@uuid
#
# Prefix and LocalName both accept:
#   - start with a letter (a-z or A-Z)
#   - followed by letters, digits, underscores, hyphens
#
# This accepts standard RDF vocabularies:
#   oe:aanvraag, system:task                    — existing
#   foaf:Person, dcterms:BibliographicResource  — external ontologies
#   schema:CreativeWork, prov:Activity          — W3C standards
#   eli:LegalResource                           — European legislation
#
# It rejects structurally invalid forms:
#   1invalid:thing (leading digit), :bare (missing prefix),
#   aaa/bbb (missing colon), oe:aanvraag:extra (multiple colons)
ENTITY_REF_PATTERN = re.compile(
    r'^(?P<prefix>[A-Za-z][A-Za-z0-9_-]*:[A-Za-z][A-Za-z0-9_-]*)'
    r'/(?P<id>[0-9a-f-]+)@(?P<version>[0-9a-f-]+)$'
)


@dataclass(frozen=True)
class EntityRef:
    """A parsed, typed entity reference.

    Frozen (hashable) so refs can be used as dict keys and in sets —
    useful for the disjoint-set invariant and deduplication elsewhere.

    Fields:
        type       — the prefix:type (e.g. "oe:aanvraag").
        entity_id  — the logical UUID (stable across revisions).
        version_id — the row UUID (specific revision).

    Constructing:
        EntityRef(type="oe:aanvraag", entity_id=..., version_id=...)

    Rendering to the canonical string form:
        str(ref)  →  "oe:aanvraag/<eid>@<vid>"

    Parsing:
        EntityRef.parse("oe:aanvraag/...@...")  →  EntityRef or None
    """

    type: str
    entity_id: UUID
    version_id: UUID

    def __str__(self) -> str:
        return f"{self.type}/{self.entity_id}@{self.version_id}"

    @classmethod
    def parse(cls, ref: str | None) -> "EntityRef | None":
        """Parse a canonical entity reference.

        Accepts two input forms:

        * **Shorthand**: ``prefix:type/entity_id@version_id``
        * **Full platform IRI**: ``{DOSSIER_BASE}{did}/entities/prefix:type/{eid}/{vid}``

        Returns an ``EntityRef`` on success, ``None`` for anything that
        doesn't match — callers should treat ``None`` as "this is an
        external URI or not a ref at all". Use ``is_external_uri`` for
        the boolean form if you only need the classification.

        Accepts ``None`` as an input and returns ``None``.
        """
        if ref is None:
            return None
        # Shorthand form.
        match = ENTITY_REF_PATTERN.match(ref)
        if match:
            return cls(
                type=match.group("prefix"),
                entity_id=UUID(match.group("id")),
                version_id=UUID(match.group("version")),
            )
        # Full IRI form — check if it's a platform entity IRI and
        # reverse-parse the path segments.
        full = _parse_full_entity_iri(ref)
        if full:
            return full
        return None


# --- Top-level helpers ------------------------------------------------------


def _parse_full_entity_iri(ref: str) -> "EntityRef | None":
    """Reverse-parse a full platform entity IRI into an EntityRef.

    Handles the canonical expanded form::

        {DOSSIER_BASE}{dossier_id}/entities/{prefix:type}/{eid}/{vid}

    Example::

        https://id.erfgoed.net/dossiers/d1000000-.../entities/oe:aanvraag/e1.../v1...

    Returns None if the ref isn't a platform entity IRI (wrong base,
    missing ``/entities/``, or malformed path segments).

    This gives clients a second accepted input form — shorthand is
    more convenient, but full IRIs are sometimes easier (e.g. when
    copy-pasting from a PROV export).
    """
    # Late import to avoid circular dependency: prov_iris imports
    # this module for reverse expansion.
    from ..prov_iris import DOSSIER_BASE

    # DOSSIER_BASE looks like "https://.../dossiers/{dossier_id}/".
    # Strip the placeholder to get the bare prefix every platform
    # IRI shares.
    platform_prefix = DOSSIER_BASE.split("{dossier_id}")[0]
    if not ref.startswith(platform_prefix):
        return None

    remainder = ref[len(platform_prefix):]
    # Expected shape: {did}/entities/{type}/{eid}/{vid}
    parts = remainder.split("/")
    if len(parts) != 5 or parts[1] != "entities":
        return None

    _did, _entities, type_name, eid_str, vid_str = parts
    try:
        return EntityRef(
            type=type_name,
            entity_id=UUID(eid_str),
            version_id=UUID(vid_str),
        )
    except (ValueError, AttributeError):
        return None


def is_external_uri(ref: str) -> bool:
    """True if ``ref`` is not a canonical entity reference (so it must
    be an external URI like a URL or other identifier)."""
    return EntityRef.parse(ref) is None
