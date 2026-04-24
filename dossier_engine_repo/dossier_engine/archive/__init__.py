"""
Dossier archive generator — produces a self-contained PDF/A-3 package
suitable for long-term (30+ year) archival.

The archive contains:
- Cover page with dossier metadata (workflow, status, dates, actors)
- Provenance timeline rendered as a static SVG (no JavaScript)
- Entity content pages (one section per entity type, version history)
- Embedded attachments: the raw PROV-JSON for machine readability

The SVG is pure server-side Python — no browser, no D3, no external
dependencies. It uses the same layout logic as the interactive columns
graph but rendered as static vector graphics that survive PDF embedding.

Usage:
    from dossier_engine.archive import generate_archive

    pdf_bytes = await generate_archive(session, dossier_id, plugin, registry)

Layout (Round 34 split):
    archive/
    ├── __init__.py       — re-exports generate_archive, render_timeline_svg
    ├── orchestrator.py   — the async generate_archive entry point
    ├── pdf.py            — ArchivePDF (FPDF subclass with header/footer)
    └── svg_timeline.py   — render_timeline_svg + colors + helpers
"""
from .orchestrator import generate_archive
from .svg_timeline import render_timeline_svg

__all__ = ["generate_archive", "render_timeline_svg"]
