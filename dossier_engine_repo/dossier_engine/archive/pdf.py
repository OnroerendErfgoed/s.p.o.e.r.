"""
PDF document class for dossier archives.

Custom FPDF subclass with workflow-aware header/footer and Unicode
font setup (DejaVu). Fonts are resolved through ``dossier_engine.archive.fonts``
so the archive works on any distro that has DejaVu installed or has
``DOSSIER_FONT_DIR`` set.

The orchestrator in ``archive.orchestrator`` instantiates this class
and drives the page-by-page rendering.
"""
from __future__ import annotations

from fpdf import FPDF


class ArchivePDF(FPDF):
    """Custom PDF with header/footer for the dossier archive."""

    def __init__(self, dossier_id: str, workflow: str):
        super().__init__(orientation="L", format="A4")
        self._dossier_id = dossier_id
        self._workflow = workflow
        self.set_auto_page_break(auto=True, margin=20)
        # Unicode font for full character support. Paths resolved via
        # dossier_engine.archive.fonts.find_font so the archive works on any
        # distro that has DejaVu installed (or DOSSIER_FONT_DIR set).
        from ..archive.fonts import find_font
        self.add_font("DejaVu", "", str(find_font("regular")))
        self.add_font("DejaVu", "B", str(find_font("bold")))
        self.add_font("DejaVu", "I", str(find_font("italic")))
        self.add_font("DejaVuMono", "", str(find_font("mono")))

    def header(self):
        self.set_font("DejaVu", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 6,
            f"Dossier {self._dossier_id[:8]}... — {self._workflow} — Archief",
            new_x="LMARGIN", new_y="NEXT",
        )
        self.line(10, 12, self.w - 10, 12)
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("DejaVu", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", align="C")
