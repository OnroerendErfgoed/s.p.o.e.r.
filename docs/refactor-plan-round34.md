# Structural refactor plan — Round 34 (Phase 1)

**Purpose.** Concrete proposals for each file split, for your review before any
code moves. No code has changed yet; this doc is the plan.

**Guiding principles.**
- **No functional changes.** Every split preserves behavior. Tests pass before
  and after each split (checkpoint per split).
- **Stable import paths where possible.** When `foo.py` becomes `foo/`, the
  package `__init__.py` re-exports the previously-top-level names so external
  `from dossier_engine.foo import X` imports still resolve. When an import
  path legitimately changes (e.g. a private helper becomes package-private),
  callers are updated in the same split.
- **One concept per file.** The goal isn't "every file under N lines" — it's
  "each file has one clear job." Some files stay large because their job is
  intrinsically large.
- **Symmetric splits beat minimum-cost splits.** If `worker.py` splits into
  "polling", "execution", "failure handling", it should split cleanly along
  those lines rather than mechanically pulling out whichever 200 lines fit
  easiest.

**Not doing this round.**
- No rename of existing module boundaries (`engine/`, `routes/`, `db/`,
  `search/`, `auth/` stay as top-level packages).
- No relocation of data classes already living in their own files
  (`entities.py`, `activity_names.py`, etc.).
- No merge of files that are already small and focused.

---

## 1. Giants

### `worker.py` (1438 lines) → `worker/` package with 6 files

Rationale: the worker has clearly-separable concerns — polling, task execution
(one kind per function), failure handling, the main loop, and the CLI entry
point. They're already separated by symbol, just not by file.

Proposed layout:

```
dossier_engine/worker/
├── __init__.py                # re-exports: Worker classes, main(), worker_loop()
├── polling.py                 # _build_scheduled_task_query, find_due_tasks,
│                              #   _claim_one_due_task, _is_task_due,
│                              #   _parse_scheduled_for  (~180 lines)
├── execution.py               # process_task, _execute_claimed_task,
│                              #   _refetch_task, _resolve_triggering_user
│                              #   (~200 lines)
├── task_kinds.py              # _process_recorded, _process_scheduled_activity,
│                              #   _process_cross_dossier, complete_task
│                              #   (~400 lines — one function per kind plus
│                              #   the shared complete_task)
├── failure.py                 # _record_failure, _compute_next_attempt_at,
│                              #   _is_missing_schema_error, _select_dead_lettered_tasks,
│                              #   requeue_dead_letters (~300 lines)
├── loop.py                    # worker_loop, _worker_loop_body (~250 lines)
└── cli.py                     # main() entry point (~50 lines)
```

Key imports re-exported from `__init__.py`: `main`, `worker_loop`,
`requeue_dead_letters`, `complete_task`, `process_task`. The rest become
package-private.

Risk: tests that import private helpers (e.g. `_record_failure` from
worker) may need their imports updated. I'll find these before the split
and plan the import rewrites.

### `plugin.py` (1162 lines) → `plugin/` package with 5 files

Rationale: this file mixes four different concerns — the `Plugin` and
`PluginRegistry` dataclasses, the registry-building functions, the
load-time validators, and small shared helpers. Validators are the
biggest chunk and naturally split by what they validate.

Proposed layout:

```
dossier_engine/plugin/
├── __init__.py                # re-exports: Plugin, PluginRegistry, FieldValidator,
│                              #   all validate_* functions, all build_* functions
├── model.py                   # Plugin (dataclass), PluginRegistry, FieldValidator
│                              #   (~250 lines)
├── registries.py              # build_entity_registries_from_workflow,
│                              #   build_callable_registries_from_workflow,
│                              #   _import_dotted, _import_dotted_callable
│                              #   (~280 lines)
├── validators/                # Load-time validators. Subpackage because
│                              #   there are 5 of them and they're independent.
│   ├── __init__.py            # re-exports all five
│   ├── version_references.py  # validate_workflow_version_references (~80 lines)
│   ├── side_effects.py        # validate_side_effect_conditions,
│   │                          #   validate_side_effect_condition_fn_registrations
│   │                          #   (~150 lines)
│   └── relations.py           # validate_relation_declarations,
│                              #   validate_relation_validator_registrations,
│                              #   the _WORKFLOW_RELATION_KEYS / _ACTIVITY_RELATION_KEYS
│                              #   frozensets (~280 lines)
└── normalize.py               # _normalize_plugin_activity_names
                               #   (auto-qualification logic — plugin.py:1080)
                               #   (~90 lines)
```

Imports from outside: nothing changes for `from dossier_engine.plugin import
Plugin, PluginRegistry, ...`. All re-exported.

Risk: this is the file with the most cross-references from tests and other
modules. The `__init__.py` re-export list has to be precise.

### `db/models.py` (750 lines) → `db/models/` package with 3 files

Rationale: `Row` dataclasses (8 of them) and `Repository` (one class, 500+
lines by itself) are different things. Repository has enough methods that
splitting IT further by concern is also sensible.

Proposed layout:

```
dossier_engine/db/models/
├── __init__.py                # re-exports all Row classes + Repository
├── rows.py                    # DossierRow, ActivityRow, AssociationRow,
│                              #   EntityRow, UsedRow, RelationRow, AgentRow,
│                              #   DomainRelationRow, Base
│                              #   (~220 lines — 8 small dataclass-like classes)
└── repository.py              # Repository class — ~500 lines
                               #   Keep this one file for now rather than
                               #   splitting Repository itself. See note below.
```

**Note on splitting Repository further.** Repository has ~40 methods. A
natural further split would be by table: entity operations, activity
operations, relation operations, agent operations. But methods cross-
reference each other frequently (`get_dossier` calls into entity operations),
and a method-count reduction isn't a legibility win by itself. **Leaving
Repository as one file for this round.** If it keeps growing, revisit.

Risk: low. Row classes are fully self-contained; Repository is one class.

### `archive.py` (641 lines) → `archive/` package with 4 files

Rationale: PDF rendering (the `ArchivePDF` class), SVG timeline rendering
(`render_timeline_svg`), the orchestrator (`generate_archive`), and small
helpers — four distinct concerns sharing one file.

Proposed layout:

```
dossier_engine/archive/
├── __init__.py                # re-exports: generate_archive, render_timeline_svg
├── orchestrator.py            # generate_archive (~200 lines — the async main)
├── pdf.py                     # ArchivePDF class + _esc (~280 lines)
├── svg_timeline.py            # render_timeline_svg (~160 lines)
└── colors.py                  # _hex_to_rgb, color constants (~15 lines)
```

Risk: low. Few external callers (`generate_archive` is the main entry).

---

## 2. Medium-large files

### `routes/activities.py` (594 lines) — split into 3 files

Rationale: this file does (a) typed-per-activity route registration,
(b) workflow-scoped generic route registration, and (c) the core
`_run_activity` execution wrapper. Three distinct responsibilities.

Proposed layout:

```
dossier_engine/routes/activities/
├── __init__.py                # re-exports register()
├── register.py                # register() entry point (~70 lines)
├── typed.py                   # _register_typed_route,
│                              #   _register_workflow_scoped_generic
│                              #   (~280 lines)
└── run.py                     # _run_activity, _resolve_plugin_and_def,
                               #   _emit_activity_success (~260 lines)
```

### `engine/pipeline/relations.py` (588 lines) — split into 3 files

Rationale: this file has (a) workflow-reading helpers (what's declared),
(b) the main processing pipeline (`process_relations` + `_parse_relations`),
(c) per-kind handlers (domain add, process control, remove), and
(d) validator dispatch. (c)+(d) share a file naturally; (a) is reference
data; (b) is the driver.

Proposed layout:

```
dossier_engine/engine/pipeline/relations/
├── __init__.py                # re-exports process_relations
├── declarations.py            # _relation_declarations, allowed_relation_types_for_activity,
│                              #   _allowed_operations, _relation_kind,
│                              #   _relation_type_declaration (~120 lines)
├── process.py                 # process_relations, _parse_relations,
│                              #   _parse_remove_relations, _validate_ref_types
│                              #   (~200 lines)
└── dispatch.py                # _handle_domain_add, _handle_process_control,
                               #   _resolve_validator, _dispatch_validators (~270 lines)
```

### `routes/prov.py` (510 lines) — split into 2 files

Rationale: the file has the route-registration function AND a 170-line
HTML/JS template string for the graph visualization (`_build_graph_html`
at line 501). The HTML template is data, not logic.

Proposed layout:

```
dossier_engine/routes/prov/
├── __init__.py                # re-exports register_prov_routes
├── register.py                # register_prov_routes (~340 lines after
│                              #   the HTML template extraction)
└── graph_template.py          # _build_graph_html + the HTML string (~170 lines)
```

Alternative: just extract the HTML to a raw `.html` file and read it. I
prefer the Python-module approach because the template uses f-string
interpolation; extracting to raw HTML would need a templating layer.

### `routes/prov_columns.py` (450 lines) — split into 2 files

Exact same shape as prov.py — it has `register_columns_graph` + a
`_build_columns_html` template at line 433.

Proposed layout:

```
dossier_engine/routes/prov_columns/
├── __init__.py                # re-exports register_columns_graph
├── register.py                # register_columns_graph (~370 lines)
└── columns_template.py        # _build_columns_html (~80 lines)
```

### `app.py` (454 lines) — stays single file

Rationale: it's the app factory. Splitting it hides the startup order,
which is the important thing this file documents. At 454 lines it's
readable. Leave it.

**Exception**: I noted during the Bug 13 (Round 33) work that
`_run_alembic_migrations` is 70+ lines of subprocess-running + error
classification. That function could move to `db/alembic.py`. Small win;
keeps app.py focused on FastAPI wiring.

Minor: extract `_run_alembic_migrations` to `db/alembic.py`. ~70 lines moved.
Net app.py: 384 lines. Still fine.

### `engine/pipeline/side_effects.py` (463 lines) — split into 2 files

Rationale: the file has orchestration (`execute_side_effects`,
`_execute_one_side_effect`) and three helper functions — condition
evaluation, auto-resolve-used for side-effect contexts, and persistence
of side-effect-generated entities. The helpers could sensibly live
together as "side-effect internals."

Proposed layout:

```
dossier_engine/engine/pipeline/side_effects/
├── __init__.py                # re-exports execute_side_effects
├── execute.py                 # execute_side_effects, _execute_one_side_effect
│                              #   (~180 lines)
└── helpers.py                 # _condition_met, _auto_resolve_used,
                               #   _persist_se_generated (~280 lines)
```

### `engine/pipeline/generated.py` (374 lines) — stays single file

Rationale: 374 lines is borderline. The file has one job (process generated
entities). Functions cross-reference tightly. **Leave it.**

### `engine/pipeline/tasks.py` (370 lines) — stays single file

Rationale: same as generated.py. One job, tight coupling. **Leave it.**

---

## 3. Routes module reorganization — BELOW THE BAR

Current `routes/` has 15 flat files. After the splits above:

```
dossier_engine/routes/
├── __init__.py
├── _activity_visibility.py     (158)
├── _errors.py                  (30)
├── _models.py                  (204)
├── _serializers.py             (72)
├── _typed_doc.py               (196)
├── access.py                   (305)
├── activities/                 (split from activities.py)
├── admin_search.py             (97)
├── dossiers.py                 (311)
├── entities.py                 (288)
├── files.py                    (107)
├── prov/                       (split from prov.py)
├── prov_columns/               (split from prov_columns.py)
└── reference.py                (213)
```

**Proposal: no further reorganization.** The file splits already reduce
`routes/` to a clearer shape (three logical sub-packages emerge:
activities, prov, prov_columns). Creating additional sub-packages
(e.g. grouping `_*.py` helpers under `_helpers/`, or pulling
`access.py`+`dossiers.py`+`entities.py` into a `core/` sub-package)
adds navigation depth without commensurate clarity. The `_`-prefixed
files are already clearly private. The public files each correspond
to a conceptual URL-tree root.

**If you want more aggressive reorganization**, the natural shape would
be by URL prefix — `/dossiers/*` routes in one sub-package, `/{workflow}/*`
routes in another. But those don't cleanly split the current files
(activities.py registers under both URL trees). I'd rather leave
`routes/` flat-ish and have each file cleanly named.

---

## 4. Files I'm NOT touching

- `entities.py` (198) — small, focused, just touched in Rounds 27/31/32 for Bugs 27/39.
- `lineage.py` (226) — small, focused.
- `migrations.py` (342) — big-ish but one concern (system-action migration helpers). Leave.
- `prov_iris.py` (344), `sentry.py` (319), `audit.py` (298), `namespaces.py` (181), `fonts.py` (152), `file_refs.py` (184), `prov_json.py` (213), `activity_names.py` (93) — all single-concern, reasonable size.
- `engine/context.py` (312) — one class (ActivityContext). Leave.
- `engine/state.py` (275), `engine/__init__.py` (259), `engine/refs.py` (175), `engine/lookups.py` (122) — all OK.

---

## Summary of file count before / after

Before:
- Top-level `.py` files: 16 (plus `__init__.py`)
- `routes/` files: 15
- `engine/` files: 4
- `engine/pipeline/` files: 11
- `db/` files: 4
- **Total engine `.py` files: ~50**

After (if all splits above are executed):
- Top-level `.py` files: 13 (worker.py, plugin.py, archive.py, app.py become packages; 1 stays)
- New top-level packages: `worker/`, `plugin/` (+`plugin/validators/`), `archive/`
- `routes/` files: 14 (activities.py, prov.py, prov_columns.py become packages)
- `engine/pipeline/` files: 9 (relations.py, side_effects.py become packages)
- `db/` has `alembic.py` added, `models.py` becomes package
- **Total engine `.py` files: ~72** (more files but each smaller and more focused)

Net: ~22 more files, but no single file over ~500 lines except Repository.
Average file size drops from ~330 to ~230 lines.

---

## Execution order proposal (once plan is approved)

1. **Isolated package splits, low-risk first.** `archive.py` → `archive/`. Small, self-contained, few callers.
2. **`db/models.py` → `db/models/`.** Row classes + Repository into separate files. Lots of internal callers but they all import from `dossier_engine.db.models` which stays as a package import.
3. **`engine/pipeline/relations.py` and `side_effects.py` → packages.** Pipeline internals; well-tested.
4. **`routes/activities.py`, `prov.py`, `prov_columns.py` → packages.** Route registration; tests exercise via HTTP so splits are transparent if re-exports are correct.
5. **`plugin.py` → `plugin/` (with `plugin/validators/` sub-package).** The biggest validator-count; save for after the easier ones.
6. **`worker.py` → `worker/`.** Most complex. Save for last because worker tests are integration-heavy.
7. **Extract `_run_alembic_migrations` to `db/alembic.py`.** Trivial; can slip in anywhere.
8. **File tree doc** (`docs/file_tree.md`).
9. **`docs/pipeline_architecture.md` revisit.**

Full suite runs between each step; any red stops the round.

---

## Open questions for your review

1. **Plugin validators sub-package.** Is `plugin/validators/` (sub-package) too deep vs. just `plugin/validators.py` (single file, ~450 lines)? I lean sub-package because the five validators are independent concerns with different test surfaces, but a single file would work too. Your call.

2. **Leaving Repository as one file.** 500+ lines, one class. Splitting it further (by table) would reduce the file but spread one logical unit across multiple files. My lean: leave it. Push back if you want it split.

3. **Leaving `generated.py` and `tasks.py` unsplit.** Both are ~370 lines. The functions are tightly coupled. My lean: leave them. Push back if you want them split.

4. **HTML templates in `routes/prov/` and `routes/prov_columns/`.** I propose keeping them as Python modules with f-string interpolation. Alternative is raw `.html` files with a simple read+substitute. Either works. Current approach is simpler.

5. **`app.py` staying monolithic.** I want to keep startup order visible in one place. If you want it split further, let me know.

6. **Execution order.** Any preference for which splits to do first? My order is "easiest/most-isolated first" so any problem shows up before I've committed to the harder splits.
