"""
Activity name resolution — CURIE ↔ local-name ↔ qualified-name.

Activity names can appear in YAML and on the wire in two forms:

* **Qualified**: ``oe:dienAanvraagIn``, ``prov:Activity`` — uses a
  namespace prefix. This is the canonical form, preferred when
  workflows mix multiple vocabularies or adopt standard ones.
* **Bare**: ``dienAanvraagIn`` — no prefix. Resolves to the
  workflow's default prefix (``oe`` by default). This is the
  shorthand form, convenient and backwards compatible with older
  workflow YAMLs.

Both forms are accepted anywhere an activity name is needed —
workflow YAML declarations, request body ``type`` fields, cross-
references in ``requirements``/``forbidden``/``cancel_if_activities``/
``side_effects``.

**URLs use local names only.** Typed endpoint URLs look like
``/{workflow}/dossiers/{did}/activities/{aid}/dienAanvraagIn``
regardless of whether the YAML declares ``oe:dienAanvraagIn`` or
bare ``dienAanvraagIn``. Colons in URL path segments are technically
valid but cause trouble with some HTTP middleware and URL libraries,
so the local part is the canonical URL form.

**PROV output uses qualified names.** The ``activities.type`` column
stores the qualified form (the engine qualifies at YAML load if
needed); PROV-JSON renders it as ``prov:type = oe:dienAanvraagIn``
with proper prefix expansion.

Two equivalents of the same activity declared in different forms
MUST resolve to the same identity. ``qualify("dienAanvraagIn")`` and
``qualify("oe:dienAanvraagIn")`` both return ``oe:dienAanvraagIn``
(assuming ``oe`` is the default prefix).
"""

from __future__ import annotations


def qualify(name: str, default_prefix: str | None = None) -> str:
    """Return the qualified form of an activity name.

    If ``name`` already contains a colon, returns it unchanged.
    Otherwise prepends the default prefix: ``dienAanvraagIn`` with
    default ``oe`` → ``oe:dienAanvraagIn``.

    Passing ``default_prefix=None`` (the default) reads the prefix
    from the namespace registry. Use an explicit argument only for
    tests that run without a configured registry.
    """
    if ":" in name:
        return name
    if default_prefix is None:
        from .namespaces import namespaces
        try:
            default_prefix = namespaces().default_workflow_prefix
        except RuntimeError:
            default_prefix = "oe"
    return f"{default_prefix}:{name}"


def local_name(name: str) -> str:
    """Return the local part of a qualified activity name.

    ``oe:dienAanvraagIn`` → ``dienAanvraagIn``
    ``dienAanvraagIn`` → ``dienAanvraagIn``
    ``prov:Activity`` → ``Activity``

    Used to build URL path segments. Callers who receive URLs and
    need to look up the activity should use the inverse resolver
    (e.g. ``match_activity(local_name_from_url, plugin)``).
    """
    if ":" in name:
        return name.split(":", 1)[1]
    return name


def match_activity_def(url_name: str, activity_defs: list[dict]) -> dict | None:
    """Find the activity definition whose local name matches ``url_name``.

    The URL path segment is always the local part. The YAML may
    declare the activity as qualified (``oe:dienAanvraagIn``) or
    bare (``dienAanvraagIn``); both match the URL ``dienAanvraagIn``.

    If multiple activities have the same local name (which indicates
    a workflow-level bug — you shouldn't declare ``oe:foo`` and
    ``custom:foo`` in the same workflow), returns the first match
    by YAML order.
    """
    for act_def in activity_defs:
        if local_name(act_def.get("name", "")) == url_name:
            return act_def
    return None
