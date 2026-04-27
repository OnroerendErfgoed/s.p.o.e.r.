"""
PROV vocabulary, IRI generation, JSON-LD serialization, namespace
registry, and activity-name qualification.

Grouped here during Round 34 so the top-level ``dossier_engine/``
directory isn't crowded with four loosely-coupled PROV modules.
Contents intentionally low-level; the rest of the engine consumes
them as stable utility APIs.

Contents:
    iris.py           — prov_prefixes, entity_qname, activity_qname,
                        agent_qname, prov_type_value, activity_full_iri,
                        expand_ref, classify_ref (rewritten from prov_iris.py)
    json_ld.py        — PROV-JSON serialization / deserialization
                        (rewritten from prov_json.py)
    namespaces.py     — NamespaceRegistry singleton (namespaces())
    activity_names.py — qualify / local_name helpers for prefixed
                        activity name qualification

No re-exports at this package level — callers import the submodule
they need (``from dossier_engine.prov.iris import ...``) rather
than going through this ``__init__.py``. This keeps the import graph
legible.
"""
