# Dossier Platform — Consolidated Code Review

*8 passes across ~30,000 lines of Python + ~3,400 lines of YAML/Markdown. Frontend excluded per instruction.*

**Legend:** ~~strikethrough~~ = fixed & tested in this session

---

## Session summary

| Status | Count | Items |
|---|---|---|
| ✅ Fixed & verified | 6 | Bugs 1, 2, 44, 47, 72 (coverage), 73 |
| 🛑 Consciously deferred | 1 | Bug 45 (stub filesystem — handled by upcoming MinIO migration) |
| 🧪 Test suite | **678/678** passing | engine 641, file_service 19, common/signing 18 |
| 🏃 `test_requests.sh` | **25/25 OK lines, exit 0** | D1–D9 all green end-to-end |
| 🔐 End-to-end proof | ✅ | Upload tokens carry `dossier_id`; move calls reject cross-dossier grafts |
| 📦 Pending | 67 bugs + 57 obs + 27 dups + 6 meta | See below |

---

## Summary

- **73 bugs** (72 original + Bug 73 surfaced during fix work), **6 crossed off**, 1 consciously deferred
- **57 structural observations** — code works today but will decay
- **27 duplication targets** — copy-paste to consolidate
- **6 meta patterns** — systemic issues that will keep producing bugs until addressed

---

## Bugs

### Must-fix — correctness, security, data integrity (21)

| # | Pass | Summary | Location | Status |
|---|------|---------|----------|--------|
| ~~1~~ | 1 | ~~`remove_relations` — `r["relation_type"]` on frozen dataclass → `TypeError` on first use. Zero test coverage.~~ | `engine/pipeline/relations.py:440-443` | ✅ **Fixed.** Attribute access now used. Verified: reverting reproduces the exact `TypeError: 'DomainRelationEntry' object is not subscriptable`. |
| ~~2~~ | 1 | ~~Same dispatch path also triggers on add-validator resolution for removes. Partner bug to #1.~~ | `engine/pipeline/relations.py:442` | ✅ **Fixed** with same one-line change. |
| 5 | 2 | `check_dossier_access` docstring claims default-deny but code + test assert default-allow. | `routes/access.py:94-98` |  |
| 6 | 2 | Alembic failure fallback runs `create_tables()` — half-migrated schema risk. | `app.py:329-334` |  |
| 7 | 2 | Batch endpoint emits audit events per item before transaction commit. | `routes/activities.py` batch handler |  |
| 14 | 3 | Cross-dossier `used` refs silently dropped from PROV-JSON export. | `routes/prov.py:218-225` |  |
| 15 | 3 | Archive tempfile leak fills `/tmp` on heavy use. | `routes/prov.py:752-755` |  |
| 16 | 3 | ~80 lines of duplicate PROV-JSON build between `/prov` and `/archive`. | `routes/prov.py:151-288` vs `697-742` |  |
| 17 | 3 | Hardcoded font paths break on Alpine / RHEL / macOS / slim containers. | `archive.py:240-243` |  |
| 30 | 4 | `move_bijlagen_to_permanent` silently swallows per-file exceptions. | `dossier_toelatingen/tasks/__init__.py:139-150` |  |
| 31 | 4 | `aanvrager.rrn` used directly as role string — PII leaks into audit logs/ES/Sentry. | `dossier_toelatingen/handlers/__init__.py:37-42` |  |
| ~~44~~ | 5 | ~~File service falls back to `temp/file_id` regardless of `dossier_id` — defeats dossier scoping.~~ | `file_service/app.py:156-212` | ✅ **Fixed.** Fallback removed; download handler serves only from `<dossier_id>/bijlagen/<file_id>`. Cross-tenant exfiltration via temp is closed. |
| 🛑 45 | 5 | **Deferred.** No path traversal defense. | `file_service/app.py:129, 186, 230-235` | 🛑 **Deliberately skipped** — traversal is a filesystem-stub concern. The upcoming MinIO migration uses flat S3 object keys where `../` has no traversal meaning. |
| ~~47~~ | 5 | ~~Upload tokens dossier-agnostic; file uploaded for dossier X could be moved into dossier Y.~~ | `routes/files.py:62-67` | ✅ **Fixed via dossier-binding.** `/files/upload/request` now requires `dossier_id` in the body and signs it into the token. File service writes it as `intended_dossier_id` into the temp `.meta`. `/internal/move` compares the binding against the move target and 403s on mismatch. No ad-hoc SQL in task code — the invariant is intrinsic to the file. Residual PROV-pollution risk noted below. |
| 55 | 5 | `lineage.find_related_entity` doesn't filter by `dossier_id` defensively. | `lineage.py:76-77` |  |
| 57 | 6 | `routes/entities.py` three endpoints skip `inject_download_urls`. | `routes/entities.py:42-186` |  |
| 58 | 6 | `POST /{workflow}/validate/{name}` has no authentication. | `routes/reference.py:117-171` |  |
| 62 | 6 | `/entities/{type}/{eid}/{vid}` doesn't verify `entity_id` matches. | `routes/entities.py:141-146` |  |
| 63 | 7 | 404 before access check enables dossier-existence enumeration. | `routes/dossiers.py:79-81`, `routes/entities.py:203-205` |  |
| 68 | 7 | Initial-schema Alembic migration mutated retroactively. | `alembic/versions/a3c1e7d4f890_add_domain_relations.py` |  |
| 71 | 8 | Test-only activities shipped in production workflow. | `dossier_toelatingen/workflow.yaml:671-742` |  |
| ~~72~~ | 8 | ~~`bewerkRelaties` zero test coverage + 500s on first `remove_relations` use (Bug 1/2 path).~~ | `dossier_toelatingen/workflow.yaml:744+` | ✅ **Coverage added** via 7 tests in `TestProcessRemoveRelations`. No longer DOA. |

### Residual risk after Bug 44 + 47 fixes

Data leak blocked: the file_service returns 404 from download (no temp fallback) and 403 from move (dossier-binding mismatch). No bytes cross dossier boundaries.

Still open: if Bob references Alice's `file_id` in his own dossier's `dienAanvraagIn`, the engine persists the entity with that reference. The file_service refuses to copy the bytes (403 logged), so any future download against it returns 404 — but Bob's PROV graph carries a broken reference. Closing this too is Fix 4 from the earlier plan: route-level validation at activity submit time (a `file_id`'s `intended_dossier_id` must match the URL's `dossier_id`). Clean next step if PROV hygiene matters; not blocking exfiltration.

### Should-fix — robustness (36)

| # | Pass | Summary | Location | Status |
|---|------|---------|----------|--------|
| 4 | 2 | `Session` type annotation never imported. | `db/models.py:238` |  |
| 9 | 2 | N+1 in dossier detail view — 50 activities = 100 queries. | `routes/dossiers.py:176-185` |  |
| 12 | 2 | `_parse_scheduled_for` silently returns None on unparseable dates. | `worker.py:58-69` |  |
| 13 | 2 | Deprecated `@app.on_event("startup")`. | `app.py:287` |  |
| — | 2 | Alembic subprocess has no timeout. | `app.py:322-326` |  |
| — | 2 | `file_service.signing_key` default accepted at startup. | `app.py`, `file_service/app.py:69` |  |
| — | 2 | No plugin-load cross-check that `handler:`/`validator:` names resolve. | `plugin.py` |  |
| — | 2 | Worker's recorded tasks don't pass `anchor_entity_id`/`anchor_type`. | `worker.py:587-594` |  |
| 20 | 3 | `_PendingEntity` missing several fields → `AttributeError`. | `engine/context.py:33-51` |  |
| 25 | 3 | `common_index.reindex_all` loads all dossiers into memory. | `search/common_index.py:135-136` |  |
| 27 | 3 | `DossierAccessEntry.activity_view: str` too narrow. | `entities.py:16` |  |
| 28 | 3 | `POCAuthMiddleware` silently overwrites on duplicate usernames. | `app.py:229-244` |  |
| 19 | 3 | `GET /dossiers` has no `response_model`. | `routes/dossiers.py:239-253` |  |
| — | 3 | Archive has no size cap. | `archive.py` |  |
| — | 3 | `app.py:69` appends `SYSTEM_ACTION_DEF` by reference (no `deepcopy`). | `app.py:69` |  |
| 34 | 4 | `authorize_activity` catches broad `Exception`. | `authorization.py:102-103, 130-131` |  |
| 35 | 4 | `reindex_common_too` does 3N queries for N dossiers. | `dossier_toelatingen/search/__init__.py:165-194` |  |
| 38 | 4 | No per-user authorize cache. | `engine/pipeline/eligibility.py:64-89` |  |
| 39 | 4 | `TaskEntity.status: str` should be `Literal[...]`. | `entities.py:73` |  |
| 42 | 4 | Field validators take raw dict, no User context. | `field_validators.py` |  |
| 43 | 4 | `Aanvrager.model_post_init` raises `ValueError` without Pydantic shape. | `dossier_toelatingen/entities.py:44-48` |  |
| 46 | 5 | `POST /files/upload/request` accepts unbounded `request_body: dict`. | `routes/files.py:49` |  |
| 48 | 5 | `.meta` filename not sanitized. | `file_service/app.py:134-144` |  |
| 50 | 5 | Migration fallback uses module-level `SYSTEM_ACTION_DEF` with bare name. | `migrations.py:279-285` |  |
| 53 | 5 | `lineage.find_related_entity` frontier growth unbounded. | `lineage.py:65-103` |  |
| 54 | 5 | `lineage.find_related_entity` returns `None` for both "not found" and "ambiguous". | `lineage.py:89` |  |
| 56 | 6 | README claims externals in both `used`/`generated` allowed; code + test reject. | `README.md:1140` |  |
| 59 | 6 | Unregistered validators silently skip. | `engine/pipeline/validators.py:48-49` |  |
| 60 | 6 | `alembic/env.py` nested `asyncio.run()` hazard. | `alembic/env.py:99` |  |
| 64 | 7 | Plugin guidebook uses `schema:` where loader reads `model:`. | `docs/plugin_guidebook.md:59` |  |
| 65 | 7 | Same `schema:` vs `model:` bug repeated in external-ontologies section. | `docs/plugin_guidebook.md:635, 639, 643` |  |
| 66 | 7 | Relation validator keying rules undocumented. | `engine/pipeline/relations.py:355-412` |  |
| 67 | 7 | `_errors.py` payload key collision. | `routes/_errors.py:28` |  |
| 69 | 7 | Tombstone role shape inconsistent between dossiertype template and workflow.yaml. | `dossiertype_template.md:44-49` vs `workflow.yaml:148` |  |
| 70 | 8 | `test_requests.sh` outputs dead `/prov/graph` URL. | `test_requests.sh:216`, `routes/prov.py:5` |  |
| ~~73~~ | (impl) | ~~`conftest.py` TRUNCATE list omits `domain_relations`.~~ | `tests/conftest.py:181-189` | ✅ **Fixed.** Added. |

### Lower-priority (16)

| # | Pass | Summary |
|---|------|---------|
| 18 | 3 | `/prov/graph/timeline` uses local dict lookups; shares logic with `dossiers.py:176-185` which hits the DB. |
| 21 | 3 | `inject_download_urls` skips `list[FileId]`. |
| 22 | 3 | `classify_ref` misclassifies bare URLs without scheme. |
| 23 | 3 | `path` vs `DOSSIER_AUDIT_LOG_PATH` env precedence undocumented. |
| 24 | 3 | `emit_audit` swallows all exceptions. |
| 26 | 3 | `recreate_index` doesn't refresh between delete/create. |
| 29 | 3 | `configure_iri_base` mutates module globals; test-order landmine. |
| 32 | 4 | `finalize_dossier` docstring documents reading `state.used_rows` — field doesn't exist. |
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

## Structural observations (57)

### Code organization
- Worker split into `poll.py`, `execute.py`, `retry.py`, `requeue.py`, `signals.py`
- Unify relation shape in `ActivityState` (3 typed lists + 1 dict)
- Split `prov.py` (792 lines)
- Extract `prov_columns_layout.py`
- Untangle import-inside-function cycles
- Rationalize `namespaces.py` singleton + scattered fallbacks

### Plugin surface
- Centralize plugin validation — 3 load-time validators exist, 5 more missing
- Plugin interface table promises 15 field validations; only 3 are actually checked
- `authorize_activity` pre-creation vs post-creation modes — split into two functions
- Load-time validation for `status:` dict-form shape
- `eligible_activities` column: `Text` → `JSONB`
- `set_dossier_access` — write-on-change only; dedup 6 copies of the view list
- Remove legacy `handle_beslissing` (marked "kept for backward compatibility")
- Back-compat "behandelaar" role needs an owner + removal deadline
- `systemAction` sub-types
- Document `systeemgebruiker` role grants
- Lineage walker needs per-walk cache + distinguishable return values

### Documentation drift
- Pipeline doc's "UPDATE must happen after persistence" is factually wrong
- Pipeline doc's `ActivityState` field table is ~⅓ of actual fields, presented as complete
- README claims external-overlap is allowed; code + tests reject
- Guidebook uses wrong YAML key (`schema:` vs `model:`)
- Dossiertype template's tombstone block shape doesn't match production workflow
- Template's endpoint docs omit the workflow-name prefix
- Relation validator keying rules (three styles) undocumented

### Performance / observability
- Cache `SearchSettings()` at module load
- `is_singleton` should cache
- `derive_status` should prefer `dossier.cached_status` first
- `check_workflow_rules` should pass `known_status`
- Archive size cap/warning
- Reindex pagination
- No per-user eligibility cache

### Test / deployment concerns
- Test fixtures use direct `Repository` instances against real Postgres — no unit isolation story documented
- `test_requests.sh` is an executable spec that isn't in CI *(confirmed passing in this session)*
- Schema-versioning tests require declaring test-only activities in production YAML
- Dependency-override-friendly auth for tests
- Signing key rotation support
- Migration framework needs top-level audit log
- `DataMigration.transform` signature should widen
- Cross-workflow task permission model

### Specific refactors named
- Add "Reads/Writes docstring matches state fields" lint
- Share layout between archive and columns graph
- `activity_view` mode complexity reduction
- Pipeline architecture doc's `ActivityState` hazard documented but not enforced

---

## Duplication (27)

| # | What |
|---|------|
| D1 | Four copies of "load dossier graph data" (prov, prov_columns, archive × 2) |
| D2 | Two copies of PROV-JSON build |
| D3 | `prov_type_value`/`agent_type_value` helpers under-used |
| D4 | Audit emission boilerplate (~15 sites) |
| D5 | 4 copies of latest-version-per-entity_id subquery |
| D6 | Repository cache returns list directly — caller mutation corrupts cache |
| D7 | `reindex_all` vs `reindex_common_too` 90% identical loops |
| D8 | `get_typed` vs `get_singleton_typed` share 80% body |
| D9 | `set_dossier_access` 6 copies of behandelaar/beheerder view list |
| D10 | 3 `reindex_*` loops share structure |
| D11 | `upload_file`/`download_file` repeat 7-param token extraction |
| D12 | `informed_by` normalization in 4 places |
| D13 | `_supersede_matching` + `cancel_matching_tasks` share latest-by-type pattern |
| D14 | Tombstone tests share structure differing from regular version tests |
| D15 | `DossierAccessEntry` fields duplicate what `access.py` narrates |
| D16 | Validator-fn registration pattern repeated without shared helper |
| D17 | Three endpoints in `routes/entities.py` repeat access-check preamble |
| D18 | Plugin load sequence repeated per plugin |
| D19 | `scheduled_for` parsing could be in one helper |
| D20 | `_activity_visibility.parse_activity_view` usage split across 3 route files |
| D21 | Four routes hand-roll a "filter activities by user access" loop |
| D22 | `emit_audit` boilerplate with the same 7 fields per call site |
| D23 | "Find systemAction activity def" pattern in 2 places |
| D24 | Alembic initial schema indices duplicated by Python model `__table_args__` — drift risk |
| D25 | Both archive.py and prov.py do their own PROV-JSON prefix building |
| D26 | `sign_token` + `verify_token` share payload-string building logic |
| D27 | Test setup helpers exist in 4+ test files with slight variations |

---

## Meta-patterns (6)

**M1. The "Reads/Writes contract in docstring" discipline has no enforcement.** Pipeline-architecture doc explicitly names this hazard. `finalization.py:52` lies about reading `state.used_rows`. Lint would catch drift at review time.

**M2. "Silent skip" as a default policy.** Unregistered validators skip, unrecognized activity_view modes skip, missing systemAction falls back to bare-name stale copy, `post_activity_hook` failures swallowed, bijlage move per-file failures swallowed, audit log errors swallowed.

**M3. Hardcoded paved-path values.** Fonts in `archive.py:240-243`, `systeemgebruiker` in `entities.py:105`, signing-key default in `app.py:41`, `id.erfgoed.net` in `prov_iris.py:53-54`. Many fail silently today.

**M4. Documentation drift across README, plugin guidebook, dossiertype template, pipeline architecture doc.** None subject to a test. A test that parses guidebook YAML examples and runs them through the plugin loader would catch Bugs 64/65 automatically.

**M5. Executable specs that don't execute.** `test_requests.sh` and guidebook YAML examples drift because they're outside the automated test suite.

**M6. "Test" is a namespace, not a load-time gate.** Bug 71 ships `testDienAanvraagInV2` into production because the workflow loader has no concept of "test-only."

---

## What was shipped this session

### Round 1 — Bug 1/2 (remove_relations TypeError)

1. `engine/pipeline/relations.py:445`: `r["relation_type"]` → `r.relation_type`
2. New test class `TestProcessRemoveRelations` with 7 methods.
3. `tests/conftest.py` TRUNCATE list: added `"domain_relations"` (Bug 73).

### Round 2 — Bug 44 + Bug 47 (file service security cluster)

**Core design:** dossier-binding is intrinsic to the file. The engine requires `dossier_id` at upload-token mint time, signs it into the token, and the file_service stamps it into the temp `.meta` as `intended_dossier_id`. At move time, the file_service compares that binding against the move's target dossier. Mismatch → 403. No ad-hoc SQL in task code; the task just says "move file X to dossier Y" and the file_service enforces the invariant locally where both values live in scope.

Concretely:

4. **`file_service/app.py` download handler**: removed temp-fallback. Serves only from `<dossier_id>/bijlagen/<file_id>`. (Bug 44.)
5. **`routes/files.py` `/files/upload/request`**: now requires `dossier_id` in the request body, signs it into the token, returns it in the response. 422 if missing.
6. **`file_service/app.py` upload handler**: writes `intended_dossier_id` into the temp `.meta` file alongside filename/content_type/size/uploaded_by.
7. **`file_service/app.py` `/internal/move`**: compares `meta["intended_dossier_id"]` against the move's `dossier_id`. Mismatch → 403 with detailed message. Empty intended or missing `.meta` skips the check (back-compat for legacy temp files, closes as old data drains).
8. **`dossier_toelatingen/tasks/__init__.py` `move_bijlagen_to_permanent`**: purely describes intent — `file_id + dossier_id` — no SQL lookups, no user-identity threading. Logs dossier-binding 403s distinctly from transport errors.
9. **`tests/test_file_service.py`**: new `TestMoveEnforcesDossierBinding` (5 tests): mismatch rejected, match accepted, empty-intended allowed, missing-meta allowed, `intended_dossier_id` written to meta verified via direct disk read.
10. **`tests/test_file_service.py`**: new `TestDownloadNoLongerFallsBackToTemp` (2 tests): download-before-move → 404; download-after-move → 200.
11. **`tests/test_file_service.py`**: extended `_sign()` and `_upload()` helpers with optional `dossier_id` parameter.
12. **`tests/integration/test_http_routes.py`**: updated 3 upload-request tests — now assert `dossier_id` is required (422 when missing), flows through to the signed token, and is returned in the response body.
13. **`test_requests.sh`**: updated `upload_file()` helper to take `dossier_id` as a 4th argument. Updated all 13 call sites across D1–D9 to pass the correct dossier UUID from their enclosing block.

### Verification performed

- **Bug 1/2 unit-level:** reverted the fix, confirmed test reproduces `TypeError: 'DomainRelationEntry' object is not subscriptable`, restored.
- **Integration-level (Postgres):** **641 engine tests passing + 18 signing + 19 file_service = 678/678**.
- **End-to-end (shell spec):** `bash test_requests.sh` → **25 `OK:` assertions, 5 summary-pass lines, exit 0**. D1–D9 all green.
- **End-to-end proof binding is live:** `fs.log` shows upload URLs carrying `dossier_id=d1000000-...`, `dossier_id=d5000000-...`, etc. Move calls carry only `file_id + dossier_id`. Zero 403s because all happy-path flows have matching binding; mismatch rejection covered by the 5 new unit tests.

### Where to go next (in priority order)

1. **Bug 68 — Alembic repair migration.** Write an idempotent `ensure_domain_relations` migration.
2. **Bug 71 — Test activities in production workflow.** Split into `workflow_test.yaml` gated by env var. Also introduces the pattern for M6.
3. **Bug 31 — RRN as role string.** PII cleanup.
4. **Bug 15 + 17 + 16 — archive cluster.** All three touch the same code path.
5. **Meta M1 + M4 + M5 — make docs executable.**
6. **Open decision: route-level file_id binding check at activity submit time.** Would close the residual PROV-pollution risk from Bug 47. Data leak already blocked; decision is about PROV hygiene versus simplicity.
