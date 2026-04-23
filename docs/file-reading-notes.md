# File-reading notes — Round 34

**Purpose.** As I split files and eventually write the tree doc, I read
each file. This document captures observations per file — what it does,
what's weird, what might duplicate something elsewhere. Populated
incrementally as I work through the splits and the tree doc pass.

**Use.** At the end of Round 34, I'll scan this for duplication flags,
file-purpose drift, or patterns worth surfacing to the user. Findings
get promoted to `dossier-review.md` as new observations.

**Format.** One section per file, in order I read them. Keep notes
short — purpose, notable patterns, anything that smells.

---

## Already-read files (from prior rounds)

These are files I read in earlier rounds and have a mental model of.
Listed here for completeness; no new notes unless re-read.

- `archive.py` → split in Round 34 Split 1
- `db/models.py` → split in Round 34 Split 2
- `app.py` → read extensively in Round 33 (Bug 13, lifespan refactor)
- `entities.py` → read in Rounds 27, 31, 32 (Bugs 27, 39)
- `plugin.py` (partial — validators section) → read in Rounds 26-32
- Various `routes/*.py` → read in Round 29 (Bug 9) and Round 31 (Bug 27)
- `engine/pipeline/used.py` → read in Round 34 template-rewrite for
  auto_resolve correction
- `engine/pipeline/finalization.py` → read for status-dict form (Obs 59)
- `engine/pipeline/authorization.py` → read in Bug 34 recon

---

## New observations (this round)

### `engine/pipeline/relations/` (split 3)

- **declarations.py** — YAML introspection. Pure read-only queries on
  activity_def. `_validate_ref_types` lives here because it's a
  declaration-driven check (reads from_types/to_types from the
  workflow-level declaration) even though it fires during processing.
- **process.py** — the driver. `process_relations` is the phase entry
  point; calls `_parse_relations` (adds) and `_parse_remove_relations`
  (supersedes) and `_dispatch_validators` (fires plugin validators).
- **dispatch.py** — `_handle_domain_add`, `_handle_process_control`,
  `_resolve_validator`, `_dispatch_validators`. The per-kind + validator
  plumbing.
- **Observation**: process.py → dispatch.py one-way import. Both
  import from declarations.py. Clean dependency tree.
- **Style note**: `_parse_remove_relations` does not delegate to a
  `_handle_domain_remove` the way `_parse_relations` delegates to
  `_handle_domain_add`/`_handle_process_control`. Remove is domain-only
  and simpler, so it's inlined. Potential symmetry win if remove grows
  more complex; not worth changing now.

