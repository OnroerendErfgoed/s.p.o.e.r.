"""
Post-load normalization — qualifies bare activity names and references.

Runs after ``Plugin`` is constructed. Mutates the plugin's workflow
dict in place so that every activity name and every cross-reference
(requirements.activities, forbidden.activities, side_effects[*].activity,
tasks[*].cancel_if_activities, tasks[*].target_activity) carries the
full qualified name (``oe:dienAanvraagIn``, not ``dienAanvraagIn``).

This lets plugin authors write bare names in YAML without worrying
about prefix consistency — the engine fixes it up once at load.
"""
from __future__ import annotations


def _normalize_plugin_activity_names(plugin: Plugin) -> None:
    """Normalize activity names to qualified form in-place.

    Qualifies bare activity names (``dienAanvraagIn``) to the
    workflow's default prefix (``oe:dienAanvraagIn``). Also
    qualifies cross-references in ``requirements``, ``forbidden``,
    ``side_effects``, ``tasks.cancel_if_activities``, and
    ``tasks.target_activity``.

    Called from ``PluginRegistry.register``, so it runs for every
    plugin load regardless of entry point. Idempotent — running it
    twice is a no-op.

    The default prefix comes from the namespace registry if
    configured; otherwise falls back to ``oe``. In test fixtures
    that skip ``create_app``, this fallback is correct for the
    current toelatingen workflow.
    """
    from ..prov.activity_names import qualify

    # Default prefix: registry if configured, else "oe".
    try:
        from ..prov.namespaces import namespaces
        default_prefix = namespaces().default_workflow_prefix
    except (RuntimeError, ImportError):
        default_prefix = "oe"

    wf = plugin.workflow
    for act in wf.get("activities", []) or []:
        if not isinstance(act, dict):
            continue
        name = act.get("name")
        if name and ":" not in name:
            act["name"] = qualify(name, default_prefix)

        # `requirements` and `forbidden` are dicts with sub-keys
        # `activities`, `statuses`, `entities`. Only the `activities`
        # list contains cross-references to other activity names.
        for field_key in ("requirements", "forbidden"):
            block = act.get(field_key)
            if isinstance(block, dict):
                act_refs = block.get("activities") or []
                if isinstance(act_refs, list):
                    block["activities"] = [
                        qualify(r, default_prefix) if isinstance(r, str) else r
                        for r in act_refs
                    ]

        # `side_effects` is a list of entries, each a dict with an
        # ``activity:`` key pointing at another activity name (plus
        # optional ``condition:``). Legacy callers may still pass
        # bare strings, which we keep supporting. Either way, qualify
        # the activity reference so downstream code compares against
        # qualified names consistently.
        side = act.get("side_effects") or []
        if isinstance(side, list):
            normalized_side = []
            for r in side:
                if isinstance(r, str):
                    normalized_side.append(qualify(r, default_prefix))
                elif isinstance(r, dict):
                    entry = dict(r)
                    ref = entry.get("activity")
                    if isinstance(ref, str):
                        entry["activity"] = qualify(ref, default_prefix)
                    normalized_side.append(entry)
                else:
                    normalized_side.append(r)
            act["side_effects"] = normalized_side

        # Tasks can reference cancel_if_activities by name
        for task in act.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            cancel = task.get("cancel_if_activities") or []
            if isinstance(cancel, list):
                task["cancel_if_activities"] = [
                    qualify(r, default_prefix) if isinstance(r, str) else r
                    for r in cancel
                ]
            target = task.get("target_activity")
            if isinstance(target, str) and ":" not in target:
                task["target_activity"] = qualify(target, default_prefix)
