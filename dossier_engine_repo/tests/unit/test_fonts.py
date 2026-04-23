"""Tests for dossier_engine.fonts — font discovery.

These exercise the refactor around Bug 17: the hardcoded Debian
font paths in ArchivePDF were replaced with a candidate-path
lookup plus a DOSSIER_FONT_DIR override. The tests pin down three
properties the refactor should have:

1. On a machine with DejaVu installed in a known location (the CI
   environment), ``find_font`` returns a real, readable file path.
2. When no candidate exists and no override is set, the error
   names the distro packages — an operator seeing this message in
   a container log should know what to install.
3. The ``DOSSIER_FONT_DIR`` override works when the candidate list
   would otherwise miss (deployments with fonts in
   non-standard locations).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dossier_engine.archive.fonts import find_font, check_fonts_available


class TestFindFont:

    def test_returns_existing_path_for_known_style(self):
        """Smoke test. On a machine with fonts-dejavu installed
        (CI's baseline), find_font returns a real file. If this
        fails in CI, the CI image is missing fonts and the archive
        endpoint will break on first use."""
        path = find_font("regular")
        assert isinstance(path, Path)
        assert path.exists()
        assert path.stat().st_size > 0

    def test_all_four_styles_resolve(self):
        """Archive uses regular, bold, italic, mono — all four
        must be findable, not just the first one. A partial font
        install (say, just DejaVuSans.ttf without the bold
        variant) is a real failure mode on stripped containers
        and would slip past a one-style smoke test."""
        for style in ("regular", "bold", "italic", "mono"):
            path = find_font(style)
            assert path.exists(), f"{style} not found at {path}"

    def test_unknown_style_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown font style"):
            find_font("serif")  # not one of the four supported

    def test_missing_everywhere_gives_actionable_error(
        self, monkeypatch,
    ):
        """When no path in the candidate list exists and no env
        override is set, the error must name the packages an
        operator should install — not a generic 'file not found'."""
        # Replace the candidate list with nonsense paths so the
        # search exhausts without finding anything.
        from dossier_engine.archive import fonts
        monkeypatch.setattr(fonts, "_CANDIDATES", {
            style: ["/nonexistent/" + s + ".ttf" for s in [style]]
            for style in fonts._CANDIDATES
        })
        monkeypatch.delenv("DOSSIER_FONT_DIR", raising=False)

        with pytest.raises(FileNotFoundError) as exc_info:
            find_font("regular")

        msg = str(exc_info.value)
        # The error should name multiple distro packages so the
        # operator knows the fix regardless of their platform.
        assert "fonts-dejavu" in msg     # Debian/Ubuntu
        assert "ttf-dejavu" in msg       # Alpine
        assert "dejavu-sans-fonts" in msg  # RHEL
        assert "DOSSIER_FONT_DIR" in msg

    def test_env_dir_override_wins(self, monkeypatch, tmp_path):
        """When DOSSIER_FONT_DIR is set, a font present there is
        used even if the normal candidates would also find one.
        This is the escape hatch for deployments with fonts in
        /opt/fonts or similar."""
        # Seed tmp_path with a fake DejaVuSans.ttf (content
        # doesn't matter for the existence check).
        (tmp_path / "DejaVuSans.ttf").write_bytes(b"fake ttf")
        monkeypatch.setenv("DOSSIER_FONT_DIR", str(tmp_path))

        path = find_font("regular")
        assert path == tmp_path / "DejaVuSans.ttf"

    def test_env_dir_missing_file_falls_through(
        self, monkeypatch, tmp_path,
    ):
        """If DOSSIER_FONT_DIR is set but the expected filename
        isn't there, we fall through to the platform candidates
        rather than erroring out. Lets a deployment set
        DOSSIER_FONT_DIR for some styles but not others."""
        # tmp_path intentionally empty — no DejaVuSans.ttf.
        monkeypatch.setenv("DOSSIER_FONT_DIR", str(tmp_path))
        path = find_font("regular")
        # Should have resolved via the platform candidates, not
        # the empty override dir.
        assert path.parent != tmp_path
        assert path.exists()


class TestCheckFontsAvailable:

    def test_passes_on_installed_system(self):
        """Startup-time invariant: if CI can render archives,
        check_fonts_available() must not raise."""
        check_fonts_available()  # Should not raise.

    def test_fails_if_any_style_missing(self, monkeypatch):
        """If any one of the four styles can't be found,
        check_fonts_available raises — it's fail-fast, not
        fail-quiet. This is what makes the check useful at
        startup: you learn about the broken archive path at app
        boot, not on the first archive request that happens to
        hit in production."""
        from dossier_engine.archive import fonts
        # Break only the bold style.
        patched = dict(fonts._CANDIDATES)
        patched["bold"] = ["/nonexistent/DejaVuSans-Bold.ttf"]
        monkeypatch.setattr(fonts, "_CANDIDATES", patched)
        monkeypatch.delenv("DOSSIER_FONT_DIR", raising=False)

        with pytest.raises(FileNotFoundError):
            check_fonts_available()
