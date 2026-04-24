"""
Static SVG timeline renderer for dossier archives.

Pure server-side Python — no browser, no D3, no external dependencies.
Renders activities as columns, entity versions as rows, and used-ref
arrows between them. Used by the archive generator; the output is
embedded directly in the PDF as an SVG image so it survives long-term
archival without relying on a JavaScript runtime.

Color palette and hex-to-rgb helper live here because only SVG code
uses them. `_esc` is the XML-escape helper for attribute and text
content.
"""
from __future__ import annotations


# ── Colours ──────────────────────────────────────────────────────

COL_BG = "#0f172a"
COL_ACTIVITY = "#3b82f6"
COL_ENTITY = "#10b981"
COL_AGENT = "#f59e0b"
COL_SYSTEM = "#8b5cf6"
COL_TASK = "#a78bfa"
COL_EXTERNAL = "#6b7280"
COL_DERIVED = "#34d399"
COL_TEXT = "#e2e8f0"
COL_MUTED = "#64748b"
COL_LINE = "#334155"


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _esc(s: str) -> str:
    """Escape XML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_timeline_svg(
    activities: list[dict],
    entities_by_type: dict[str, list[dict]],
    agents: dict[str, str],
    used_map: dict[str, list[str]],
    generated_map: dict[str, list[str]],
    derivations: list[tuple[str, str]],
    *,
    width: int = 1200,
) -> str:
    """Render a static SVG of the provenance timeline.

    Layout:
    - Top band: activities as columns, left to right chronologically
    - Middle: entity rows grouped by type, versions placed under their
      generating activity's column
    - Lines: wasGeneratedBy (down), used (up), wasDerivedFrom (horizontal)

    Returns SVG markup as a string.
    """
    if not activities:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100"><text x="20" y="50" fill="#64748b" font-size="14">Geen activiteiten in dit dossier</text></svg>'

    # Layout constants
    col_w = max(140, min(200, (width - 160) // max(len(activities), 1)))
    margin_left = 180
    margin_top = 80
    act_y = margin_top + 20
    row_h = 50
    entity_start_y = act_y + 70

    # Collect entity type rows
    type_order = list(entities_by_type.keys())
    total_height = entity_start_y + len(type_order) * row_h + 60

    # Build activity position map
    act_x = {}
    for i, act in enumerate(activities):
        act_x[act["id"]] = margin_left + i * col_w

    svg_parts = []
    svg_w = margin_left + len(activities) * col_w + 80
    svg_parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_w}" height="{total_height}" '
        f'viewBox="0 0 {svg_w} {total_height}" '
        f'style="background:{COL_BG}; font-family: sans-serif;">'
    )

    # ── Activity columns ──
    for i, act in enumerate(activities):
        x = act_x[act["id"]]
        # Vertical guide line
        svg_parts.append(
            f'<line x1="{x}" y1="{act_y + 25}" x2="{x}" y2="{total_height - 30}" '
            f'stroke="{COL_LINE}" stroke-width="1" stroke-dasharray="3,6" opacity="0.3"/>'
        )
        # Activity box
        bw = col_w - 20
        svg_parts.append(
            f'<rect x="{x - bw//2}" y="{act_y - 15}" width="{bw}" height="30" '
            f'rx="6" fill="{COL_ACTIVITY}" opacity="0.9"/>'
        )
        # Label
        label = act["type"]
        if label.startswith("oe:"):
            label = label[3:]
        if len(label) > 18:
            label = label[:16] + "..."
        svg_parts.append(
            f'<text x="{x}" y="{act_y + 4}" text-anchor="middle" '
            f'fill="white" font-size="9" font-weight="500">{_esc(label)}</text>'
        )
        # Time
        if act.get("time"):
            t = act["time"]
            if "T" in t:
                t = t.split("T")[1][:8]
            svg_parts.append(
                f'<text x="{x}" y="{act_y + 22}" text-anchor="middle" '
                f'fill="{COL_MUTED}" font-size="8">{t}</text>'
            )
        # Agent
        if act.get("agent"):
            svg_parts.append(
                f'<text x="{x}" y="{act_y - 22}" text-anchor="middle" '
                f'fill="{COL_AGENT}" font-size="8">{_esc(act["agent"][:20])}</text>'
            )

    # ── Entity type rows ──
    entity_positions = {}  # version_id → (x, y)

    for row_idx, etype in enumerate(type_order):
        y = entity_start_y + row_idx * row_h
        # Row label
        label = etype
        if label.startswith("oe:"):
            label = label[3:]
        svg_parts.append(
            f'<text x="{margin_left - 15}" y="{y + 5}" text-anchor="end" '
            f'fill="{COL_MUTED}" font-size="10" font-style="italic">{_esc(label)}</text>'
        )
        # Row line
        svg_parts.append(
            f'<line x1="{margin_left - 10}" y1="{y}" '
            f'x2="{margin_left + (len(activities) - 1) * col_w + 10}" y2="{y}" '
            f'stroke="{COL_LINE}" stroke-width="1" stroke-dasharray="2,6" opacity="0.3"/>'
        )

        # Place entity versions
        versions = entities_by_type[etype]
        for ver in versions:
            gen_act = ver.get("generated_by")
            if gen_act and gen_act in act_x:
                ex = act_x[gen_act]
            else:
                ex = margin_left

            is_task = etype in ("system:task",)
            is_ext = etype == "external"
            col = COL_TASK if is_task else (COL_EXTERNAL if is_ext else COL_ENTITY)

            # Entity marker
            svg_parts.append(
                f'<rect x="{ex - 30}" y="{y - 10}" width="60" height="20" '
                f'rx="10" fill="{col}" opacity="0.85"/>'
            )
            vlabel = f'v{ver.get("version_idx", "?")}' if not is_ext else "ext"
            svg_parts.append(
                f'<text x="{ex}" y="{y + 4}" text-anchor="middle" '
                f'fill="white" font-size="8">{vlabel}</text>'
            )

            entity_positions[ver["version_id"]] = (ex, y)

            # wasGeneratedBy line (activity → entity)
            if gen_act and gen_act in act_x:
                ax = act_x[gen_act]
                svg_parts.append(
                    f'<line x1="{ax}" y1="{act_y + 15}" x2="{ex}" y2="{y - 10}" '
                    f'stroke="{COL_ACTIVITY}" stroke-width="0.8" opacity="0.3"/>'
                )

    # ── Derivation arrows ──
    for from_vid, to_vid in derivations:
        if from_vid in entity_positions and to_vid in entity_positions:
            x1, y1 = entity_positions[from_vid]
            x2, y2 = entity_positions[to_vid]
            svg_parts.append(
                f'<line x1="{x1 + 30}" y1="{y1}" x2="{x2 - 30}" y2="{y2}" '
                f'stroke="{COL_DERIVED}" stroke-width="2" opacity="0.6" '
                f'/>'
            )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)
