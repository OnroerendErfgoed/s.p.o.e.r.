"""
Font discovery for PDF archive generation.

The archive module renders PDF/A output via fpdf2, which needs TTF
font files loaded at PDF construction time. Historically the paths
were hardcoded to Debian/Ubuntu's ``/usr/share/fonts/truetype/dejavu/``
layout — which breaks silently on Alpine (``/usr/share/fonts/dejavu/``),
RHEL/Rocky (``/usr/share/fonts/dejavu-sans-fonts/``), macOS, and slim
containers that ship no fonts at all.

The failure mode was unhelpful: fpdf2 raises a generic
``FileNotFoundError`` pointing at the Debian path, with no hint
about which package provides the font for other distros. First
production archive request on a non-Debian host was always the
moment this bug fired.

This module centralises font discovery:

* A prioritised list of candidate paths covering the common
  distros and macOS.
* An env-var escape hatch (``DOSSIER_FONT_DIR``) for deployments
  that ship fonts in a non-standard location.
* A clear error message naming the packages to install, so the
  operator knows what to do.

Usage:

    from dossier_engine.archive.fonts import find_font
    regular = find_font("regular")      # Path to DejaVuSans.ttf
    bold    = find_font("bold")         # Path to DejaVuSans-Bold.ttf

If fonts become a startup-time concern (fail-fast instead of
fail-on-first-archive), callers can invoke ``check_fonts_available()``
during app bootstrap.
"""

from __future__ import annotations

import os
from pathlib import Path


# Paths are tried in order; first hit wins. Keep Debian/Ubuntu first
# because that's the current deployment target — but the alternatives
# aren't theoretical, they're for CI images, dev laptops, and future
# Alpine/RHEL deployments.
_CANDIDATES: dict[str, list[str]] = {
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      # Debian/Ubuntu
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",               # Alpine
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",    # RHEL/Rocky/Fedora
        "/usr/share/fonts/TTF/DejaVuSans.ttf",                  # Arch
        "/Library/Fonts/DejaVuSans.ttf",                        # macOS (user-installed)
        "/System/Library/Fonts/Supplemental/DejaVuSans.ttf",    # macOS (rare)
    ],
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/Library/Fonts/DejaVuSans-Bold.ttf",
    ],
    "italic": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Oblique.ttf",
        "/Library/Fonts/DejaVuSans-Oblique.ttf",
    ],
    "mono": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/Library/Fonts/DejaVuSansMono.ttf",
    ],
}

# Filenames used when searching ``DOSSIER_FONT_DIR`` (the env-var
# escape hatch). Keep in sync with the candidate lists above — these
# are the DejaVu project's canonical filenames.
_FILENAMES: dict[str, str] = {
    "regular": "DejaVuSans.ttf",
    "bold": "DejaVuSans-Bold.ttf",
    "italic": "DejaVuSans-Oblique.ttf",
    "mono": "DejaVuSansMono.ttf",
}


def find_font(style: str) -> Path:
    """Locate a DejaVu font file for the given style.

    ``style`` is one of: ``"regular"``, ``"bold"``, ``"italic"``,
    ``"mono"``. Returns the first path that exists on disk.

    Search order:
    1. ``$DOSSIER_FONT_DIR/<filename>`` if the env var is set.
    2. The platform candidate list for this style.

    Raises ``FileNotFoundError`` with a clear, actionable message
    if no font can be found. The error names the packages to
    install on common distros so the operator doesn't have to
    guess.
    """
    if style not in _CANDIDATES:
        raise ValueError(
            f"Unknown font style: {style!r}. "
            f"Valid styles: {sorted(_CANDIDATES)}"
        )

    # Env-var override — deployments with fonts in a non-standard
    # location set DOSSIER_FONT_DIR=/opt/fonts or similar.
    env_dir = os.environ.get("DOSSIER_FONT_DIR")
    if env_dir:
        candidate = Path(env_dir) / _FILENAMES[style]
        if candidate.is_file():
            return candidate

    for path_str in _CANDIDATES[style]:
        path = Path(path_str)
        if path.is_file():
            return path

    # Exhausted all options. Tell the operator what to do.
    tried = (
        [str(Path(env_dir) / _FILENAMES[style])] if env_dir else []
    ) + _CANDIDATES[style]
    raise FileNotFoundError(
        f"DejaVu font ({style}) not found. Install one of:\n"
        f"  - Debian/Ubuntu:  apt-get install fonts-dejavu\n"
        f"  - Alpine:         apk add ttf-dejavu\n"
        f"  - RHEL/Rocky:     dnf install dejavu-sans-fonts\n"
        f"  - macOS:          brew install --cask font-dejavu\n"
        f"Or set DOSSIER_FONT_DIR to a directory containing "
        f"{_FILENAMES[style]}. Tried: {tried}"
    )


def check_fonts_available() -> None:
    """Call once at app startup to fail-fast if fonts are missing.

    Invokes ``find_font`` for every style the archive needs,
    raising ``FileNotFoundError`` immediately if any are missing.
    This turns "opaque failure on first archive request" into
    "clear failure at app boot," which is easier to diagnose in
    container deployments.

    Call sites that want lazy discovery (e.g. a test that never
    renders an archive) should simply not call this.
    """
    for style in _CANDIDATES:
        find_font(style)
