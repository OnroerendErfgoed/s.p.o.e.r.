"""Doc-drift guard — pipeline phase docstrings must reference real
state fields.

M1 relief. The ``engine/pipeline/*.py`` modules document each
phase function's data flow via ``Reads:`` and ``Writes:`` lines
in the docstring — a human-readable contract that says which
``ActivityState`` fields the phase consumes and produces. The
orchestrator's readability depends on these being accurate.

They drift. ``finalize_dossier`` (``finalization.py``) claimed
to read ``state.used_rows`` — no such field exists on
``ActivityState``. Nothing was wrong at runtime (the function
didn't actually try to read the missing field) but the doc was
lying about the data flow, which defeats the whole point of
using docstring contracts instead of static types.

This harness parses every ``async def`` in the pipeline package,
extracts ``state.X`` references from the Reads/Writes blocks in
its docstring, and checks them against
``ActivityState.__dataclass_fields__``. Any reference to a
non-existent field fails the test with the file + function +
field name so the fix is easy.

Sibling harnesses:
* ``test_guidebook_yaml.py`` — M4/M5 relief. Guidebook YAML
  examples must parse and use canonical workflow keys.
"""

from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

import pytest

from dossier_engine.engine.state import ActivityState


PIPELINE_DIR = (
    Path(__file__).parent.parent.parent
    / "dossier_engine" / "engine" / "pipeline"
)


# Fields that docstrings may reference that aren't "real"
# ActivityState fields but are still legitimate mentions. Keep this
# list empty to start — if real violations turn up, decide case by
# case whether to widen this allow-list or fix the docstring.
# The goal of the harness is to force the conversation on every
# drift, not to silently accept it.
ALLOWED_NON_FIELD_REFS: frozenset[str] = frozenset()


# Regex for a state-field reference: ``state.<ident>`` where
# <ident> is a Python identifier. Deliberately doesn't match
# ``state.<ident>.<subattr>`` (anything on the right of the first
# dot is arbitrary attribute access we can't validate statically
# — e.g. ``state.dossier.workflow`` references the ``workflow``
# attribute on whatever ``dossier`` happens to be, not a field on
# ActivityState itself).
_STATE_REF = re.compile(r"\bstate\.([a-zA-Z_][a-zA-Z0-9_]*)")


def _extract_reads_writes_refs(docstring: str) -> set[str]:
    """Return the set of ``state.X`` identifiers referenced in the
    Reads: and Writes: sections of a docstring.

    The sections can span multiple lines with continuation
    indentation; we rely on the regex finding every ``state.X``
    occurrence and ignore the structure otherwise. That's wider
    than strictly necessary — a ``state.X`` in the prose body of
    the docstring will also be picked up — but that's fine: prose
    references to state fields should be real too.
    """
    if not docstring:
        return set()
    return set(_STATE_REF.findall(docstring))


def _iter_async_functions(module_path: Path):
    """Yield ``(function_name, docstring)`` for every ``async def``
    at module top-level in ``module_path``.

    Nested functions (defined inside another function) are skipped
    — phases are always top-level, and nested helpers tend to have
    different docstring conventions.
    """
    source = module_path.read_text()
    tree = ast.parse(source, filename=str(module_path))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            yield node.name, ast.get_docstring(node) or ""


@pytest.fixture(scope="module")
def activity_state_fields() -> frozenset[str]:
    """The single source of truth for what's allowed on the
    right-hand side of ``state.``."""
    return frozenset(f.name for f in fields(ActivityState))


@pytest.fixture(scope="module")
def pipeline_modules() -> list[Path]:
    if not PIPELINE_DIR.is_dir():
        pytest.skip(f"Pipeline dir not found at {PIPELINE_DIR}")
    # Skip dunder and private modules like ``_identity.py`` —
    # those aren't phases, just shared helpers, and they may not
    # follow the Reads/Writes convention.
    return sorted(
        p for p in PIPELINE_DIR.glob("*.py")
        if not p.name.startswith("_") and p.name != "__init__.py"
    )


class TestPhaseDocstringFieldReferences:
    """Every ``state.X`` reference in a pipeline phase's docstring
    must correspond to a real ``ActivityState`` field."""

    def test_pipeline_dir_has_modules(self, pipeline_modules):
        """Sanity: fail loudly if the pipeline dir lookup is
        broken rather than silently reporting zero violations."""
        assert len(pipeline_modules) >= 5, (
            f"Expected ≥5 pipeline modules, found "
            f"{len(pipeline_modules)}. Path resolution may be "
            f"wrong: {PIPELINE_DIR}"
        )

    def test_every_state_ref_in_docstrings_is_a_real_field(
        self, pipeline_modules, activity_state_fields,
    ):
        """Parse every ``async def`` in the pipeline package,
        pull ``state.X`` references from its docstring, and check
        each X against ActivityState's declared fields."""
        violations = []
        for module_path in pipeline_modules:
            for fn_name, docstring in _iter_async_functions(
                module_path,
            ):
                refs = _extract_reads_writes_refs(docstring)
                for ref in sorted(refs):
                    if ref in activity_state_fields:
                        continue
                    if ref in ALLOWED_NON_FIELD_REFS:
                        continue
                    violations.append(
                        f"{module_path.name}::{fn_name} "
                        f"references ``state.{ref}`` in its "
                        f"docstring but ActivityState has no "
                        f"such field."
                    )

        if violations:
            raise AssertionError(
                f"{len(violations)} docstring/field drift "
                f"violation(s):\n\n" + "\n".join(violations)
                + "\n\nFix: either rename the docstring "
                f"reference to a real ActivityState field, or "
                f"add the field to ActivityState if the docstring "
                f"is describing intended future state. Don't add "
                f"to ALLOWED_NON_FIELD_REFS without discussion — "
                f"the whole point of this harness is to force the "
                f"conversation."
            )


class TestActivityStateFieldsAreDiscoverable:
    """Meta-check: the harness's reflection on ActivityState
    works. If the dataclass grows a field and someone switches
    away from ``@dataclass``, the whole harness silently becomes
    a no-op — test here so that failure is loud."""

    def test_known_fields_present(self, activity_state_fields):
        """A handful of well-known fields must be in the set. If
        these are missing, something's broken with the
        introspection approach."""
        for required in ("dossier_id", "user", "activity_def", "repo"):
            assert required in activity_state_fields, (
                f"ActivityState introspection is broken: "
                f"{required!r} not in {activity_state_fields}"
            )

    def test_field_set_is_reasonably_sized(self, activity_state_fields):
        """The dataclass has ~30 fields. A set of <10 or >100
        would mean the reflection isn't picking up what we
        expect."""
        assert 10 < len(activity_state_fields) < 100, (
            f"ActivityState has {len(activity_state_fields)} "
            f"fields — outside expected range. Introspection "
            f"may be returning wrong thing."
        )
