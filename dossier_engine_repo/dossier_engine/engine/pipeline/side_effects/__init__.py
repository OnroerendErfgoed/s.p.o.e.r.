"""
Side-effect activity execution.

When an activity declares `side_effects: [{activity: ...}, ...]` in
its YAML, each named activity is executed automatically after the
triggering activity has persisted its outputs. Side effects run as
the system caller (`agent="system"`, `role="systeem"`) and are
recursively allowed to declare their own side effects, up to a depth
limit.

Side effects are a deliberately pared-down form of the main pipeline:

* No client `used`/`generated`/`relations` blocks — the side effect
  computes everything from its handler.
* No custom validators.
* No tombstone shape check.
* No status-determining-from-content rules (system handlers return
  `HandlerResult.status` directly).
* No tasks scheduling.
* No finalization (the triggering activity's finalization runs once
  at the end and reflects the cumulative side-effect chain).

What side effects DO have:

* **Conditions.** A side effect can carry a `condition: {entity_type,
  field, value}` block — only run if the condition entity exists and
  its field equals the expected value. Used for "only run this if
  the user/entity is of a certain type."
* **Auto-resolved used entities.** Each side effect's used block
  declares types with `auto_resolve: latest`; the engine looks at the
  triggering activity's generated + used entities first (the trigger
  scope), then falls back to dossier-wide singleton lookup if the
  type wasn't touched by the trigger.
* **Schema versioning** for generated entities, via the same
  `_resolve_schema_version` helper the main pipeline uses.
* **Recursive side-effect chains.** A side effect can declare its own
  `side_effects:` block, which runs after it persists. Depth is
  capped to prevent runaway chains.

Layout (Round 34 split):
    side_effects/
    ├── __init__.py      — re-exports execute_side_effects, _condition_met,
    │                      _auto_resolve_used, _persist_se_generated
    ├── execute.py       — execute_side_effects, _execute_one_side_effect
    └── helpers.py       — _condition_met, _auto_resolve_used,
                           _persist_se_generated
"""
from .execute import execute_side_effects
from .helpers import _condition_met, _auto_resolve_used, _persist_se_generated

__all__ = [
    "execute_side_effects",
    "_condition_met",
    "_auto_resolve_used",
    "_persist_se_generated",
]
