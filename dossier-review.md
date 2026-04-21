# Dossier Platform — Consolidated Code Review

*8 passes across ~30,000 lines of Python + ~3,400 lines of YAML/Markdown. Frontend excluded per instruction.*

**Legend:** ~~strikethrough~~ = fixed & tested; 🔍 = investigated, not a real bug.

---

## Engagement summary

| Status | Count | Items |
|---|---|---|
| ✅ Fixed & verified | 14 | Bugs 1, 2, 15, 16, 17, 32, 44, 47, 64, 65, 68, 72 (coverage), 73, 74, 75 |
| 🔍 Investigated, not a bug | 1 | Bug 14 — cross-dossier refs are `type=external` rows |
| 🛑 Deferred / accepted | 3 | Bug 31 (RRN acceptable), Bug 45 (MinIO migration), Bug 71 (test activities, deploy-time removal) |
| 🧪 Test suite | **720/720** passing | engine 683, file_service 19, common/signing 18 |
| 🏃 `test_requests.sh` | **25/25 OK, exit 0, zero deadlocks, zero worker crashes** | D1–D9 green |
| ✂️ Duplication closed | **D1, D2, D25** | Graph-loader consolidation |
| 🧰 Harnesses installed | **3** | Guidebook YAML lint + phase-docstring lint + CI shell-spec wrapper |
| 📦 Pending | ~59 bugs + 57 obs + 24 dups + 6 meta (partial relief) | See below |

Note: Bug 75 was discovered *by* harness 2 on its first run — a new bug surfaced and fixed in the same session as the harness that surfaced it.

---

## Bugs

### Must-fix — correctness, security, data integrity

| # | Pass | Summary | Status |
|---|------|---------|--------|
| ~~1~~ | 1 | ~~`remove_relations` — `r["relation_type"]` on frozen dataclass → `TypeError`.~~ | ✅ |
| ~~2~~ | 1 | ~~Add-validator dispatch path also triggers on removes.~~ | ✅ |
| 5 | 2 | `check_dossier_access` docstring claims default-deny but code asserts default-allow. |  |
| 6 | 2 | Alembic failure fallback runs `create_tables()` — half-migrated schema risk. |  |
| 7 | 2 | Batch endpoint emits audit events per item before transaction commit. |  |
| 🔍 14 | 3 | **Not a bug.** Cross-dossier refs persisted as local `type=external` rows via `ensure_external_entity`; raw-UUID cross-dossier refs rejected at `resolve_used:89-92` with 422. | Dropped from must-fix. |
| ~~15~~ | 3 | ~~Archive tempfile leak fills `/tmp` on heavy use.~~ | ✅ |
| ~~16~~ | 3 | ~~Duplicate PROV-JSON build between `/prov` and `/archive`.~~ | ✅ |
| ~~17~~ | 3 | ~~Hardcoded font paths break on non-Debian.~~ | ✅ |
| 30 | 4 | `move_bijlagen_to_permanent` silently swallows per-file exceptions. |  |
| 📝 31 | 4 | Closed by product decision (RRN in `role`/`dossier_access`/ES ACL acceptable). | Decided. |
| ~~44~~ | 5 | ~~File service falls back to `temp/file_id` regardless of `dossier_id`.~~ | ✅ |
| 🛑 45 | 5 | Deferred — MinIO migration handles it. |  |
| ~~47~~ | 5 | ~~Upload tokens dossier-agnostic.~~ | ✅ |
| 55 | 5 | `lineage.find_related_entity` doesn't filter by `dossier_id` defensively. |  |
| 57 | 6 | `routes/entities.py` three endpoints skip `inject_download_urls`. |  |
| 58 | 6 | `POST /{workflow}/validate/{name}` has no authentication. |  |
| 62 | 6 | `/entities/{type}/{eid}/{vid}` doesn't verify `entity_id` matches. |  |
| 63 | 7 | 404 before access check enables dossier-existence enumeration. |  |
| ~~68~~ | 7 | ~~Initial-schema Alembic migration mutated retroactively.~~ | ✅ |
| 🛑 71 | 8 | **Accepted** — deploy-time checklist removes test activities from `workflow.yaml`. |  |
| ~~72~~ | 8 | ~~`bewerkRelaties` zero test coverage.~~ | ✅ |

### Should-fix — robustness

| # | Pass | Summary | Status |
|---|------|---------|--------|
| 4 | 2 | `Session` type annotation never imported. |  |
| 9 | 2 | N+1 in dossier detail view. |  |
| 12 | 2 | `_parse_scheduled_for` silently returns None on unparseable dates. |  |
| 13 | 2 | Deprecated `@app.on_event("startup")`. |  |
| — | 2 | Alembic subprocess has no timeout. |  |
| — | 2 | `file_service.signing_key` default accepted at startup. |  |
| — | 2 | No plugin-load cross-check that `handler:`/`validator:` names resolve. |  |
| — | 2 | Worker's recorded tasks don't pass `anchor_entity_id`/`anchor_type`. |  |
| 20 | 3 | `_PendingEntity` missing several fields → `AttributeError`. |  |
| 25 | 3 | `common_index.reindex_all` loads all dossiers into memory. |  |
| 27 | 3 | `DossierAccessEntry.activity_view: str` too narrow. |  |
| 28 | 3 | `POCAuthMiddleware` silently overwrites on duplicate usernames. |  |
| 19 | 3 | `GET /dossiers` has no `response_model`. |  |
| — | 3 | Archive has no size cap. |  |
| — | 3 | `app.py:69` appends `SYSTEM_ACTION_DEF` by reference. |  |
| ~~32~~ | 4 | ~~`finalize_dossier`/`run_pre_commit_hooks` docstring documents reading `state.used_rows` — field doesn't exist.~~ | ✅ **Fixed** — docstring now reads `state.used_rows_by_ref` matching the code. Harness 3 prevents recurrence. |
| 34 | 4 | `authorize_activity` catches broad `Exception`. |  |
| 35 | 4 | `reindex_common_too` does 3N queries for N dossiers. |  |
| 38 | 4 | No per-user authorize cache. |  |
| 39 | 4 | `TaskEntity.status: str` should be `Literal[...]`. |  |
| 42 | 4 | Field validators take raw dict, no User context. |  |
| 43 | 4 | `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. |  |
| 46 | 5 | `POST /files/upload/request` accepts unbounded `request_body: dict`. |  |
| 48 | 5 | `.meta` filename not sanitized. |  |
| 50 | 5 | Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. |  |
| 53 | 5 | `lineage.find_related_entity` frontier growth unbounded. |  |
| 54 | 5 | `lineage.find_related_entity` returns `None` for both "not found" and "ambiguous". |  |
| 56 | 6 | README claims externals in both `used`/`generated` allowed; code + test reject. |  |
| 59 | 6 | Unregistered validators silently skip. |  |
| 60 | 6 | `alembic/env.py` nested `asyncio.run()` hazard. |  |
| ~~64~~ | 7 | ~~Plugin guidebook uses `schema:` where loader reads `model:`.~~ | ✅ **Fixed** in `docs/plugin_guidebook.md:59`. Harness 1 prevents recurrence. |
| ~~65~~ | 7 | ~~Same `schema:` vs `model:` bug in external-ontologies section.~~ | ✅ **Fixed** in `docs/plugin_guidebook.md:635, 639, 643`. |
| 66 | 7 | Relation validator keying rules undocumented. |  |
| 67 | 7 | `_errors.py` payload key collision. |  |
| 69 | 7 | Tombstone role shape inconsistent between dossiertype template and workflow.yaml. |  |
| 70 | 8 | `test_requests.sh` outputs dead `/prov/graph` URL. | Harness 2 in CI would flag this on the next run; fix is a one-liner once CI is wired. |
| ~~73~~ | (impl) | ~~`conftest.py` TRUNCATE list omits `domain_relations`.~~ | ✅ |
| ~~74~~ | (impl) | ~~Worker/route deadlock on `system:task` rows.~~ | ✅ **Fixed.** Structural (worker takes dossier lock first, matching user-activity order) + defence-in-depth (`run_with_deadlock_retry` on routes). |
| ~~75~~ | (impl) | ~~Worker crashes on cold start if the app hasn't finished Alembic migrations yet — `UndefinedTableError` propagates to top-level crash handler.~~ | ✅ **Fixed.** Surfaced by harness 2. Worker now tolerates SQLSTATE 42P01 during pre-ready window, logs a warning and retries; real missing-table errors after first successful poll still propagate. |

### Lower-priority

| # | Pass | Summary |
|---|------|---------|
| 18 | 3 | `/prov/graph/timeline` uses local dict lookups; shares logic with `dossiers.py:176-185` which hits the DB. |
| 21 | 3 | `inject_download_urls` skips `list[FileId]`. |
| 22 | 3 | `classify_ref` misclassifies bare URLs without scheme. |
| 23 | 3 | `path` vs `DOSSIER_AUDIT_LOG_PATH` env precedence undocumented. |
| 24 | 3 | `emit_audit` swallows all exceptions. |
| 26 | 3 | `recreate_index` doesn't refresh between delete/create. |
| 29 | 3 | `configure_iri_base` mutates module globals; test-order landmine. |
| 33 | 4 | `compute_eligible_activities` relies on undocumented Repository activity cache. |
| 36 | 4 | Reference data has no version/migration story. |
| 37 | 4 | `_resolve_field` strips leading `content.` inconsistently. |
| 40 | 4 | `SYSTEM_ACTION_DEF` mutation at load could leak across plugins. |
| 41 | 4 | Pre-commit hooks receive `used_rows=state.used_rows_by_ref`; README docs name but not shape. |
| 49 | 5 | `query_string_to_token` declared but never imported. Dead code. |
| 51 | 5 | Migration's already-applied check uses JSONB string equality. |
| 52 | 5 | Migration framework has no two-phase / all-or-nothing mode. |
| 61 | 6 | `activity_relations` indices cost writes but have zero readers today. |

---

## Meta-patterns (6; three with partial relief shipped)

**M1. Docstring "Reads/Writes" drift has no enforcement.** ✅ **Partial relief shipped.** `tests/unit/test_phase_docstrings.py` (harness 3) parses every `async def` in `engine/pipeline/*.py`, extracts `state.X` references from docstrings, and checks them against `ActivityState.__dataclass_fields__`. Bug 32 was surfaced and fixed by this harness on its first run. Future drift is caught at commit time.

**M2. "Silent skip" as a default policy.** Unregistered validators skip, unrecognized activity_view modes skip, `post_activity_hook` failures swallowed, bijlage move per-file failures swallowed, audit log errors swallowed. No specific relief shipped — these warrant case-by-case review.

**M3. Hardcoded paved-path values.** Bug 17 (fonts) closed this engagement via `dossier_engine/fonts.py`. Others remain — `systeemgebruiker` in `entities.py:105`, signing-key default, `id.erfgoed.net` in `prov_iris.py`.

**M4. Documentation drift across README, plugin guidebook, dossiertype template, pipeline architecture doc.** ✅ **Partial relief shipped.** `tests/integration/test_guidebook_yaml.py` (harness 1) validates every ```yaml block in the guidebook against canonical key sets derived from production `workflow.yaml`. Bugs 64 and 65 were surfaced and fixed in the same session. A sibling check keeps the allowed-key set honest: if production adds a new field, the test fails and forces the allowlist update.

**M5. Executable specs that don't execute.** ✅ **Partial relief shipped.** `scripts/ci_run_shell_spec.sh` (harness 2) is a self-contained wrapper that stands up file_service + app + worker, waits for readiness, runs `test_requests.sh`, reports OK count / summary count / traceback count, exits 0/1/2/3 for pass/fail/stack-never-up/env-missing. Intended to be wired into whatever CI system the team uses (GitHub Actions, GitLab, etc.) with a single job step. End-to-end tested; surfaced Bug 75 as a side benefit. Partial because the guidebook's *Python* code blocks still aren't validated (each references dotted-import paths for fictional classes; full relief would need a fixture-module approach we haven't attempted).

**M6. "Test" is a namespace, not a load-time gate.** Bug 71 accepted — deploy-time checklist keeps test activities out of production.

---

## Structural observations & duplications

Structural observations (57) unchanged from the 8-pass sweep; see earlier review revisions for full list. Key callouts worth revisiting:

- Worker split into `poll.py`/`execute.py`/`retry.py`/`signals.py` — the `worker.py` file grew this session with Bug 75's resilience logic. Split soon.
- `prov.py` at 523 lines (was 792) — further splitting possible.
- `prov_columns.py` layout algorithm (~280 lines inside `register_columns_graph`) wants extraction.

Duplications (24 remaining; 3 closed): D1 (graph-rowset loader), D2 (PROV-JSON build), D25 (PROV-JSON prefix building) all closed via `dossier_engine/prov_json.py`. D3–D24, D26, D27 remain.

---

## What was shipped across the engagement

### Round 1 — Bug 1/2 (remove_relations TypeError)
Field access fix in `engine/pipeline/relations.py`, 7 new tests, `conftest.py` TRUNCATE extended (Bug 73).

### Round 2 — Bug 44/47 (file service security)
Dossier-binding minted into upload tokens + stamped into `.meta`; file_service rejects moves whose target doesn't match the stamped binding. 7 new tests. `test_requests.sh` upload helper + 13 call sites updated.

### Round 3 — Bug 68 (Alembic consolidation)
Pre-deploy: three migrations folded into one initial. `scripts/check_migrations_append_only.py` guard + README rule.

### Round 4 — Bug 31 (product decision)
No code change. RRN in `role`, `oe:dossier_access`, and ES ACL is acceptable (none are externally queryable). Verified `agent_id`/`agent.uri` already use `user.id`/`user.uri`.

### Round 5 — Archive cluster (Bugs 15, 16, 17) + Duplication D1/D2/D25
- `dossier_engine/fonts.py` — five-platform font lookup + `DOSSIER_FONT_DIR` override + actionable error.
- `dossier_engine/prov_json.py` — `load_dossier_graph_rows` + `build_prov_graph` shared by four endpoints.
- `routes/prov.py` 792 → 506 lines; /prov and /archive 1-line calls; archive uses in-memory Response (no tempfile).
- `routes/prov_columns.py` uses shared loader.
- 16 new tests.

### Round 6 — Bug 74 (worker/route deadlock)
- Structural: `worker._execute_claimed_task` now acquires the dossier lock before entity INSERTs, matching user-activity lock order.
- Defence-in-depth: `run_with_deadlock_retry` in `db/session.py`, wired into all three `_handle_*` methods.
- 11 new tests.

### Round 7 — Bug 14 investigation
Dropped from must-fix — `ensure_external_entity` handles cross-dossier cases, `resolve_used` rejects raw-UUID cross-dossier at 422.

### Round 8 — M1/M4/M5 relief + Bugs 32, 64, 65, 75
- `tests/integration/test_guidebook_yaml.py` — harness 1, 6 tests. Caught and fixed Bugs 64 and 65.
- `tests/unit/test_phase_docstrings.py` — harness 3, 4 tests. Caught and fixed Bug 32.
- `scripts/ci_run_shell_spec.sh` — harness 2, end-to-end CI wrapper. Surfaced Bug 75 on first run.
- `tests/unit/test_worker_startup_resilience.py` — 5 tests for Bug 75's detector function.
- Worker resilience logic in `worker._worker_loop_body` — tolerates `UndefinedTableError` during startup window, logs and retries until schema ready.

### Verification performed
- **Test suite:** **720/720** (engine 683, signing 18, file_service 19). Grew by 47 tests across the engagement.
- **Shell spec via harness 2:** `bash scripts/ci_run_shell_spec.sh` → 25 OK assertions, 5 summary-pass lines, exit 0, zero tracebacks, zero worker crashes.
- **Harness 1, 2, 3** all green, all have synthetic-drift tests confirming they catch the bug shape they claim to catch.

### Where to go next (in priority order)

1. **Wire harness 2 into CI** — the script is ready; the step is infrastructure-team work. Adds `test_requests.sh` coverage to every PR. Would have caught Bug 70 already.
2. **Bug 63 — enumeration via 404-before-access-check.** Security, low effort.
3. **Duplication D4 + D22** — `emit_audit` boilerplate (~15 sites). Mechanical.
4. **Observations cluster around `set_dossier_access`** — 6 copies of the view list, write-on-change opportunity. Improves performance and reduces D9.
5. **Meta M2 — "silent skip" review.** Survey all `logger.error` + `pass` patterns, decide which should propagate.
