"""
Activity-execution endpoints — single, batch, and per-workflow typed.

Two URL families:

**Workflow-scoped** (the workflow is in the URL, no DB lookup needed
to resolve the plugin):

* ``PUT /{workflow}/dossiers/{id}/activities/{aid}/{type}`` — typed.
* ``PUT /{workflow}/dossiers/{id}/activities/{aid}`` — generic single.
* ``PUT /{workflow}/dossiers/{id}/activities`` — generic batch.

**Workflow-agnostic** (the engine resolves the workflow from the
dossier's DB row or from ``request.workflow`` on creation):

* ``PUT /dossiers/{id}/activities/{aid}`` — generic single.
* ``PUT /dossiers/{id}/activities`` — generic batch.

All call into the same ``execute_activity`` engine entry point.

Layout (Round 34 split):
    activities/
    ├── __init__.py       — re-exports register()
    ├── register.py       — register() entry point (closures over registry/get_user)
    ├── typed.py          — _register_typed_route, _register_workflow_scoped_generic
    └── run.py            — _run_activity, _emit_activity_success, _resolve_plugin_and_def
"""
from .register import register

__all__ = ["register"]
