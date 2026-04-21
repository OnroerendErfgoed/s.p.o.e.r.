"""Doc-drift guard — plugin guidebook YAML examples must be valid.

M5/M4 relief. The plugin guidebook contains many YAML examples
that document how to structure `workflow.yaml`. Historically those
examples drifted from what the loader accepts (Bugs 64/65: the
guidebook used `schema:` where the loader reads `model:`) because
nothing forced them to stay current.

This harness extracts every fenced ```yaml block from the guidebook
and checks two properties:

1. Every block parses as valid YAML. Cheap sanity check that
   catches broken indentation, stray tabs, unquoted colons.

2. Blocks that look like workflow fragments (top-level
   `entity_types:` or `activities:`) get a **structural** check:
   every key on every entry must be in the canonical allowed-set
   derived from production `workflow.yaml` + the loader's reads.

We intentionally don't try to run the full plugin loader against
each block. The examples use dotted Python paths like
`"my_workflow.entities.Aanvraag"` that don't exist — they're
shape-demonstrations, not runnable fixtures. The structural check
is narrow enough to avoid false positives but wide enough to catch
the `schema:` vs `model:` class of bug.

Sibling harnesses:
* `test_phase_docstrings.py` — M1 relief. Docstring "Reads/Writes"
  field references must refer to real ActivityState fields.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


GUIDEBOOK = (
    Path(__file__).parent.parent.parent.parent
    / "docs" / "plugin_guidebook.md"
)


# Canonical keys derived by running ``yaml.safe_load`` on the
# production workflow.yaml and collecting every key ever set on an
# entry. These are the only keys the loader + pipeline recognize.
#
# Keep in sync with dossier_toelatingen/workflow.yaml — if a new
# field lands there, it needs to land here too. The sibling test
# ``test_canonical_keys_match_production`` guards this.
ACTIVITY_KEYS = frozenset({
    "allowed_roles", "authorization", "can_create_dossier",
    "client_callable", "default_role", "description", "entities",
    "forbidden", "generates", "handler", "label", "name",
    "relations", "requirements", "side_effects", "status",
    "status_resolver", "task_builders", "tasks", "used",
    "validators",
})

ENTITY_TYPE_KEYS = frozenset({
    "cardinality", "description", "model", "revisable",
    "schemas", "type",
})


def _extract_yaml_blocks(markdown: str) -> list[tuple[int, str]]:
    """Return ``(line_number, yaml_text)`` pairs for every
    fenced ```yaml block in the document.

    Line number is 1-indexed and points at the opening fence,
    so pytest failure messages correlate directly to the
    guidebook file."""
    blocks: list[tuple[int, str]] = []
    pattern = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)
    for m in pattern.finditer(markdown):
        line_no = markdown[:m.start()].count("\n") + 1
        blocks.append((line_no, m.group(1)))
    return blocks


# Parametrise over the blocks so each one shows as its own pytest
# case — a single broken block shows up in the report as a named
# failure rather than the whole file going red at once.
def _block_id(block: tuple[int, str]) -> str:
    line_no, _ = block
    return f"line_{line_no}"


@pytest.fixture(scope="module")
def guidebook_blocks() -> list[tuple[int, str]]:
    if not GUIDEBOOK.exists():
        pytest.skip(f"Guidebook not found at {GUIDEBOOK}")
    return _extract_yaml_blocks(GUIDEBOOK.read_text())


class TestGuidebookYamlParses:
    """Every fenced yaml block must be valid YAML. If this fails,
    the guidebook has a syntax error — pytest output names the
    opening line so you can find it."""

    def test_at_least_one_yaml_block_exists(self, guidebook_blocks):
        """Sanity: the guidebook should have some YAML examples.
        If not, the extractor regex is broken or the doc shape
        changed radically and the whole harness needs review."""
        assert len(guidebook_blocks) >= 5, (
            f"Expected ≥5 YAML blocks in {GUIDEBOOK.name}, "
            f"found {len(guidebook_blocks)}. Extractor regex "
            f"may be broken."
        )

    def test_every_block_parses_as_yaml(self, guidebook_blocks):
        errors = []
        for line_no, block in guidebook_blocks:
            try:
                yaml.safe_load(block)
            except yaml.YAMLError as e:
                errors.append(
                    f"{GUIDEBOOK.name}:{line_no}\n"
                    f"  Invalid YAML: {e}"
                )
        if errors:
            raise AssertionError(
                f"{len(errors)} YAML block(s) failed to parse:\n\n"
                + "\n\n".join(errors)
            )


class TestGuidebookWorkflowFragmentsUseCanonicalKeys:
    """Blocks with top-level ``entity_types:`` or ``activities:``
    are workflow fragments. Every key on every entry must be in
    the loader's known-set; unknown keys usually mean a typo that
    the loader silently ignores (the Bug 64/65 shape).

    The allowed-set is derived from production ``workflow.yaml``
    and guarded by a sibling test that keeps both in sync."""

    def test_entity_types_use_only_canonical_keys(self, guidebook_blocks):
        errors = []
        for line_no, block in guidebook_blocks:
            data = yaml.safe_load(block)
            if not isinstance(data, dict):
                continue
            for et in data.get("entity_types") or []:
                if not isinstance(et, dict):
                    continue
                unknown = set(et.keys()) - ENTITY_TYPE_KEYS
                if unknown:
                    name = et.get("type", "<unknown>")
                    errors.append(
                        f"{GUIDEBOOK.name}:{line_no} — entity_types "
                        f"entry {name!r} uses unrecognized key(s): "
                        f"{sorted(unknown)}. "
                        f"Allowed: {sorted(ENTITY_TYPE_KEYS)}."
                    )
        if errors:
            raise AssertionError(
                "Unrecognized entity_types keys in guidebook:\n\n"
                + "\n".join(errors)
            )

    def test_activities_use_only_canonical_keys(self, guidebook_blocks):
        errors = []
        for line_no, block in guidebook_blocks:
            data = yaml.safe_load(block)
            if not isinstance(data, dict):
                continue
            for act in data.get("activities") or []:
                if not isinstance(act, dict):
                    continue
                unknown = set(act.keys()) - ACTIVITY_KEYS
                if unknown:
                    name = act.get("name", "<unknown>")
                    errors.append(
                        f"{GUIDEBOOK.name}:{line_no} — activities "
                        f"entry {name!r} uses unrecognized key(s): "
                        f"{sorted(unknown)}. "
                        f"Allowed: {sorted(ACTIVITY_KEYS)}."
                    )
        if errors:
            raise AssertionError(
                "Unrecognized activity keys in guidebook:\n\n"
                + "\n".join(errors)
            )


class TestCanonicalKeysMatchProduction:
    """The allowed-key sets above are derived from production
    ``workflow.yaml``. If someone adds a new field to production
    YAML without updating this file, new guidebook examples that
    use the field would (wrongly) fail.

    This test re-derives the sets from production and compares.
    If it fails, either:
    (a) production added a new field — add it to the frozenset
        above, ship together.
    (b) the guidebook moved to reference new keys — see (a).
    """

    @pytest.fixture
    def production_workflow(self):
        wf_path = (
            Path(__file__).parent.parent.parent.parent
            / "dossier_toelatingen_repo"
            / "dossier_toelatingen" / "workflow.yaml"
        )
        if not wf_path.exists():
            pytest.skip(f"Production workflow not found at {wf_path}")
        return yaml.safe_load(wf_path.read_text())

    def test_activity_keys_are_superset_of_production(
        self, production_workflow,
    ):
        production_keys = set()
        for act in production_workflow.get("activities", []):
            production_keys.update(act.keys())
        missing = production_keys - ACTIVITY_KEYS
        assert not missing, (
            f"Production workflow.yaml uses activity keys not in "
            f"test_guidebook_yaml.py's ACTIVITY_KEYS allowlist: "
            f"{sorted(missing)}. Add them to the frozenset."
        )

    def test_entity_type_keys_are_superset_of_production(
        self, production_workflow,
    ):
        production_keys = set()
        for et in production_workflow.get("entity_types", []):
            production_keys.update(et.keys())
        missing = production_keys - ENTITY_TYPE_KEYS
        assert not missing, (
            f"Production workflow.yaml uses entity_types keys not "
            f"in test_guidebook_yaml.py's ENTITY_TYPE_KEYS "
            f"allowlist: {sorted(missing)}. Add them to the "
            f"frozenset."
        )
