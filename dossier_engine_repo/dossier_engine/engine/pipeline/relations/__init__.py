"""
Relation processing — process-control and domain relations.

An activity request can carry a ``relations`` block alongside ``used``
and ``generated``. Each entry is either:

* **Process-control** (has ``entity``) — a directed edge from the
  activity to an entity, like ``oe:neemtAkteVan``. Persisted in the
  ``activity_relations`` table.

* **Domain** (has ``from`` + ``to``) — a semantic edge between two
  things (entity→entity, entity→URI, dossier→dossier). Persisted in
  the ``domain_relations`` table. Neither endpoint is the activity;
  the activity is the *provenance* of the relation.

The activity may also carry a ``remove_relations`` block (domain only)
to supersede existing domain relations.

Two policy layers control which relation types are allowed:

1. **Permission gate** — the union of the workflow's top-level
   ``relations:`` block and the activity's own ``relations:`` block
   declares which types may be sent. Anything outside is a 422.

2. **Operations gate** (domain only) — each activity's relation
   declaration can specify ``operations: [add, remove]``. If only
   ``[add]`` (the default), remove_relations for that type is rejected.

3. **Validator firing** (activity-level opt-in) — validators run
   only for types listed in the activity's OWN ``relations:`` block.

Layout (Round 34 split):
    relations/
    ├── __init__.py        — re-exports process_relations, _validate_ref_types
    ├── declarations.py    — YAML introspection helpers + _validate_ref_types
    ├── process.py         — process_relations, _parse_relations,
    │                        _parse_remove_relations
    └── dispatch.py        — _handle_domain_add, _handle_process_control,
                             _resolve_validator, _dispatch_validators
"""
from .declarations import _validate_ref_types
from .process import process_relations

__all__ = ["process_relations", "_validate_ref_types"]
