"""
Private route-layer helpers used by the route registrars.

Grouped here during Round 34 to reduce crowding of the flat routes/
directory. Underscore prefix on file names dropped inside — the
package name already signals privacy.

Contents:
    activity_visibility.py — parse_activity_view, is_activity_visible
                             (filters timeline + entity views by user role)
    errors.py              — activity_error_to_http
    models.py              — Pydantic request/response models for routes
    serializers.py         — entity_version_dict (row → dict for API)
    typed_doc.py           — build_activity_description (OpenAPI doc text)
"""
