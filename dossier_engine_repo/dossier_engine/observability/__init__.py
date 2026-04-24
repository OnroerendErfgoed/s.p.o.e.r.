"""
Cross-cutting observability infrastructure.

Not about the dossier domain; instead, the tools that let
operators see what the engine and worker are doing at runtime.

Contents:
    audit.py  — emit_dossier_audit and related audit-log helpers
    sentry.py — Sentry setup + capture_* helpers for the worker and
                web app (error reporting / tracing)

Grouped here during Round 34 to reduce crowding at the top level.
"""
