# Running notes during Round 34 refactor

This file captures observations accumulated while splitting files and
reading through modules. At the end of the refactor I'll review these
and surface the substantive ones in the inventory's findings section.

## Observations so far

### Redundant inline import in `side_effects/helpers.py`

Found during Split 4. The original `side_effects.py` line 270-ish
redundantly re-imported ``ActivityContext`` inside ``_condition_met``
even though it was already imported at module top. Removed during the
split. Minor cleanup; no functional change.

### Flaky test-ordering in `tests/integration/test_http_activities.py` — pre-existing

Discovered during Split 3 verification. The file passes as 17/17 green
about 50% of the time; the other 50% produces 2-6 errors/failures —
different tests each time, mostly connection-refused or IntegrityError
autoflush races. Each failing test passes in isolation.

Unrelated to the Round 34 refactor (the failing tests don't touch the
relations pipeline). Likely a shared-state race between tests in the
same file — session-scoped DB fixtures cleaning up asynchronously
against ongoing connections. **Worth investigating outside this round.**

The test suite does NOT use `pytest-xdist` or similar parallelism; this
is sequential flakiness.

## Pattern watch list (things to look for as I read files)

- **Duplicate helpers** — same logic appearing in multiple files because
  nobody factored it out.
- **Dead code** — functions not imported anywhere (like `_auto_resolve`
  pattern we saw earlier).
- **Inconsistent naming** — two files doing the same thing with
  different names, or two things with the same name.
- **Path fragility** — `Path(__file__).parent.parent[.parent]` chains
  that'll break if directory depth changes.
- **Silent fallbacks** — `c if c in (...) else "single"` style defaults
  that hide typos (already have one: entity_types cardinality).
- **Cross-module imports that reveal architectural leaks** — e.g. routes
  importing engine internals, engine importing routes.
- **Old comments referring to removed code** — review-doc smell.
- **Private-helper leakage** — functions prefixed `_` but imported
  from tests or other modules.

### O — Relative-import depth changes are silent traps

During Split 6 (`plugin.py` → `plugin/`), several `from .activity_names`
and `from .namespaces` lazy-imports inside function bodies broke because
`.` now means the new `plugin/` package instead of top-level
`dossier_engine/`. These weren't caught by smoke-import tests because
the imports are lazy (triggered only when those functions run). Caught
by the unit suite.

**For the remaining worker split:** grep for `from \.\w` in the source
file BEFORE moving anything, and plan the `.` → `..` adjustments upfront.

### O — Stale path references in docstrings after reorg

Found during file-tree documentation pass. Eight files have docstring
or comment references to pre-reorg module paths:

* ``entities.py`` references ``routes/_activity_visibility.py``
  (now ``routes/_helpers/activity_visibility.py``)
* ``app.py`` references ``plugin.py`` (now a package)
* ``engine/pipeline/tombstone.py`` references ``pipeline/invariants.py``
  (now ``pipeline/_helpers/invariants.py``)
* ``engine/context.py`` mentions tests path — harmless
* ``routes/prov.py`` references ``dossier_engine.prov_json``
  (now ``dossier_engine.prov.json_ld``)
* ``routes/activities/run.py`` references ``dossier_engine.audit``
  (now ``dossier_engine.observability.audit``)
* ``archive/pdf.py`` twice references ``dossier_engine.fonts``
  (now ``dossier_engine.archive.fonts``)

None break behavior. Fixing them during the file-tree-doc pass as I
read each file.
