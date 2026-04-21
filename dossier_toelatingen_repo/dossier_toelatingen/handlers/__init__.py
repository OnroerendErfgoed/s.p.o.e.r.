"""
Handler functions for toelatingen system activities.

Each handler receives an ActivityContext and optional client content,
and returns a HandlerResult with the computed entity content and optional status.

Handlers use context.get_typed("oe:type") to get Pydantic model instances
instead of accessing raw dicts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from dossier_engine.engine import ActivityContext, HandlerResult
from dossier_toelatingen.entities import (
    Aanvraag, Beslissing, Handtekening, VerantwoordelijkeOrganisatie,
)


async def set_dossier_access(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Determines who can see this dossier based on the current state.
    Creates/updates the dossier_access entity.
    """
    access_entries = []

    # Aanvrager can always see their own dossier
    aanvraag: Aanvraag | None = context.get_typed("oe:aanvraag")
    if aanvraag:
        if aanvraag.aanvrager.kbo:
            access_entries.append({
                "role": f"kbo-toevoeger:{aanvraag.aanvrager.kbo}",
                "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external", "external"],
                "activity_view": "own",
            })
        if aanvraag.aanvrager.rrn:
            access_entries.append({
                "role": aanvraag.aanvrager.rrn,
                "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external", "external"],
                "activity_view": "own",
            })

    # Verantwoordelijke organisatie gets full access
    verantw: VerantwoordelijkeOrganisatie | None = await context.get_singleton_typed("oe:verantwoordelijke_organisatie")
    if verantw:
        access_entries.append({
            "role": f"gemeente-toevoeger:{verantw.uri}",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields", "system:task"],
            "activity_view": "all",
        })

    # Behandelaar gets access — oe:behandelaar is cardinality=multiple, so
    # we iterate all behandelaar entities currently on the dossier and grant
    # each one its own access entry. Previously this singleton-looked-up
    # the "latest" one which was incorrect for multi-cardinality types.
    # Dedupe by URI so repeated handler invocations that each create a new
    # behandelaar entity (phase 3 will formalize revisions) don't cause
    # duplicate access entries.
    behandelaars = await context.get_entities_latest("oe:behandelaar")
    seen_uris: set[str] = set()
    for behandelaar_row in behandelaars:
        uri = (behandelaar_row.content or {}).get("uri")
        if not uri or uri in seen_uris:
            continue
        seen_uris.add(uri)
        access_entries.append({
            "role": f"behandelaar:{uri}",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields", "system:task"],
            "activity_view": "all",
        })

    # Back-compat: also emit a generic "behandelaar" role so access rules
    # that match by bare role-name (not by behandelaar URI) keep working.
    # Remove once all downstream consumers match by URI.
    if behandelaars:
        access_entries.append({
            "role": "behandelaar",
            "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                      "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                      "oe:system_fields", "system:task"],
            "activity_view": "all",
        })

    # Beheerder gets everything
    access_entries.append({
        "role": "beheerder",
        "view": ["oe:aanvraag", "oe:beslissing", "oe:handtekening", "external",
                  "oe:verantwoordelijke_organisatie", "oe:behandelaar",
                  "oe:system_fields", "oe:dossier_access", "system:task"],
        "activity_view": "all",
    })

    return HandlerResult(
        content={"access": access_entries},
        status=None,
    )


async def set_verantwoordelijke_organisatie(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Determines the responsible organization based on the aanvraag.
    """
    aanvraag: Aanvraag | None = context.get_typed("oe:aanvraag")
    if not aanvraag:
        return HandlerResult(content={"uri": "https://organisatie.onbekend"}, status=None)

    # POC: simple mapping. In production: lookup in organisation registry.
    if aanvraag.gemeente == "Brugge":
        org_uri = "https://id.erfgoed.net/organisaties/brugge"
    else:
        org_uri = "https://id.erfgoed.net/organisaties/oe"

    return HandlerResult(
        content={"uri": org_uri},
        status=None,
    )


async def set_system_fields(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Sets system-computed fields: creation date, creator.
    """
    entity = context.get_used_entity("oe:aanvraag")
    aanmaker = entity.attributed_to if entity else "unknown"

    return HandlerResult(
        content={
            "datum": datetime.now(timezone.utc).isoformat(),
            "aanmaker": f"https://id.erfgoed.net/agenten/{aanmaker}",
        },
        status=None,
    )


async def handle_beslissing(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    System activity triggered after tekenBeslissing.
    Determines the final status based on the handtekening and beslissing.
    If onvolledig, schedules a trekAanvraagIn task with a 30-day deadline.

    NOTE: This handler is kept for backward compatibility only. The
    tekenBeslissing and neemBeslissing activities now use the split-
    style YAML declarations (``status_resolver:`` + ``task_builders:``)
    which route to ``resolve_beslissing_status`` and
    ``schedule_trekAanvraag_if_onvolledig`` below. This legacy handler
    reproduces the combined behaviour for any caller still using it.
    """
    handtekening: Handtekening | None = context.get_typed("oe:handtekening")
    beslissing: Beslissing | None = context.get_typed("oe:beslissing")

    if not handtekening:
        return HandlerResult(status="beslissing_te_tekenen")

    if not handtekening.getekend:
        return HandlerResult(status="klaar_voor_behandeling")

    if beslissing:
        if beslissing.beslissing == "goedgekeurd":
            return HandlerResult(status="toelating_verleend")
        elif beslissing.beslissing == "onvolledig":
            task = await _build_trekAanvraag_task(context)
            return HandlerResult(
                status="aanvraag_onvolledig",
                tasks=[task] if task else [],
            )
        else:
            return HandlerResult(status="toelating_geweigerd")

    return HandlerResult(status="beslissing_ondertekend")


# ---------- Split-style hooks for tekenBeslissing / neemBeslissing ----------
#
# These three functions together replace the monolithic handle_beslissing.
# The YAML now declares:
#
#   handler: null (no content to generate for these activities)
#   status_resolver: "resolve_beslissing_status"
#   task_builders: ["schedule_trekAanvraag_if_onvolledig"]
#
# Each function has a single, documented responsibility. The status
# resolver reads used entities and returns a status string. The task
# builder decides whether to schedule a trekAanvraagIn task and, if
# so, resolves the anchor entity and computes the deadline from
# plugin constants. Both are independently testable.


async def resolve_beslissing_status(context: ActivityContext) -> str | None:
    """Decide the dossier status after a tekenBeslissing or
    neemBeslissing activity.

    Reads the latest handtekening and beslissing and maps them to a
    status string. Returns None only in theoretical cases where
    neither entity exists — the engine leaves the status unchanged
    when None is returned.
    """
    handtekening: Handtekening | None = context.get_typed("oe:handtekening")
    beslissing: Beslissing | None = context.get_typed("oe:beslissing")

    if not handtekening:
        return "beslissing_te_tekenen"
    if not handtekening.getekend:
        return "klaar_voor_behandeling"

    if beslissing:
        if beslissing.beslissing == "goedgekeurd":
            return "toelating_verleend"
        if beslissing.beslissing == "onvolledig":
            return "aanvraag_onvolledig"
        return "toelating_geweigerd"

    return "beslissing_ondertekend"


async def schedule_trekAanvraag_if_onvolledig(
    context: ActivityContext,
) -> list[dict]:
    """When a beslissing is ``onvolledig``, schedule a
    trekAanvraagIn task anchored to the aanvraag, cancellable by
    vervolledigAanvraag.

    Returns an empty list in every other case. Separating "do I
    schedule?" into its own function makes the scheduling condition
    grep-able and the task shape testable in isolation.
    """
    beslissing: Beslissing | None = context.get_typed("oe:beslissing")
    if not beslissing or beslissing.beslissing != "onvolledig":
        return []

    task = await _build_trekAanvraag_task(context)
    return [task] if task else []


async def _build_trekAanvraag_task(context: ActivityContext) -> dict | None:
    """Build the trekAanvraagIn task dict.

    Shared between the legacy handler path and the split-style task
    builder so behaviour stays identical. Resolves the anchor entity
    (via used, then via lineage walk from the beslissing) and reads
    the deadline days from plugin constants.
    """
    from datetime import datetime, timezone, timedelta
    from dossier_engine.lineage import find_related_entity

    aanvraag_row = context.get_used_row("oe:aanvraag")
    if aanvraag_row is None:
        beslissing_row = context.get_used_row("oe:beslissing")
        if beslissing_row is not None:
            aanvraag_row = await find_related_entity(
                context.repo,
                context.dossier_id,
                beslissing_row,
                "oe:aanvraag",
            )

    anchor_entity_id = str(aanvraag_row.entity_id) if aanvraag_row else None

    deadline_days = context.constants.aanvraag_deadline_days
    deadline = (
        datetime.now(timezone.utc) + timedelta(days=deadline_days)
    ).isoformat()

    task = {
        "kind": "scheduled_activity",
        "target_activity": "trekAanvraagIn",
        "scheduled_for": deadline,
        "cancel_if_activities": ["vervolledigAanvraag"],
        "allow_multiple": False,
        "anchor_type": "oe:aanvraag",
    }
    if anchor_entity_id is not None:
        task["anchor_entity_id"] = anchor_entity_id
    return task


async def duid_behandelaar_aan(context: ActivityContext, content: dict | None) -> HandlerResult:
    """
    Assigns a behandelaar based on the verantwoordelijke organisatie.
    """
    verantw: VerantwoordelijkeOrganisatie | None = context.get_typed("oe:verantwoordelijke_organisatie")

    if verantw and verantw.uri == "https://id.erfgoed.net/organisaties/oe":
        behandelaar_uri = f"{verantw.uri}/behandelaar/benjamma"
    elif verantw:
        behandelaar_uri = verantw.uri
    else:
        behandelaar_uri = "https://id.erfgoed.net/organisaties/onbekend"

    return HandlerResult(
        content={"uri": behandelaar_uri},
        status="klaar_voor_behandeling",
    )


# Registry of all handlers
HANDLERS = {
    "set_dossier_access": set_dossier_access,
    "set_verantwoordelijke_organisatie": set_verantwoordelijke_organisatie,
    "set_system_fields": set_system_fields,
    "handle_beslissing": handle_beslissing,  # legacy path; still registered
    "duid_behandelaar_aan": duid_behandelaar_aan,
}

# Split-style status resolvers. Each function returns a status
# string (or None) given the activity context. Referenced from YAML
# via ``status_resolver: "name"`` on an activity.
STATUS_RESOLVERS = {
    "resolve_beslissing_status": resolve_beslissing_status,
}

# Split-style task builders. Each returns a list of task dicts
# (possibly empty). Referenced from YAML via ``task_builders: [...]``
# on an activity. Multiple builders can apply to one activity; the
# engine concatenates their results.
TASK_BUILDERS = {
    "schedule_trekAanvraag_if_onvolledig": schedule_trekAanvraag_if_onvolledig,
}


# Named predicates for gating side-effect execution. YAML references
# these via ``condition_fn: "name"`` on a side-effect entry. Each is
# an async function taking an ActivityContext and returning bool:
# True means "run the side effect," False means skip.
#
# Choose this form when the gate is more than simple field equality
# (for which ``condition: {entity_type, field, value}`` is clearer
# inline in YAML). Empty by default — no workflow currently needs a
# non-equality gate; the registry exists so plugins can add one
# without engine changes.
SIDE_EFFECT_CONDITIONS = {}
