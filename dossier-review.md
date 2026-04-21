# Dossier Platform — Consolidated Code Review

*8 passes across ~30,000 lines of Python + ~3,400 lines of YAML/Markdown. Frontend excluded per instruction.*

**Legend:** ~~strikethrough~~ = fixed & tested; 🔍 = investigated, not a real bug.

---

## Engagement summary

| Status | Count | Items |
|---|---|---|
| ✅ Fixed & verified | 16 | Bugs 1, 2, 15, 16, 17, 32, 44, 47, 64, 65, 68, 70, 72 (coverage), 73, 74, 75 + Obs-2 (duplicate "external") |
| 🔍 Investigated, not a bug | 1 | Bug 14 — cross-dossier refs are `type=external` rows |
| 🛑 Deferred / accepted | 4 | Bug 31 (RRN acceptable), Bug 45 (MinIO migration), Bug 63 (403 is correct HTTP), Bug 71 (test activities, deploy-time removal) |
| 🧪 Test suite | **740/740** passing | engine 687, toelatingen 16, file_service 19, common/signing 18 |
| 🏃 `test_requests.sh` | **25/25 OK, exit 0, zero deadlocks, zero worker crashes** | D1–D9 green |
| ✂️ Duplication closed | **D1, D2, D4, D22, D25** | Graph-loader consolidation + audit-emit wrapper |
| 🧰 Harnesses installed | **3** | Guidebook YAML lint + phase-docstring lint + CI shell-spec wrapper |
| 🤖 CI wired | **GitHub Actions** | `.github/workflows/ci.yml` — 4 jobs: pytest, shell-spec, doc-harnesses, migrations-append-only |
| 📦 Pending | ~59 bugs + 57 obs + 22 dups + 5 meta (partial relief) | See below |

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
| 📝 63 | 7 | **Accepted — keep 403.** Enumeration via 403-vs-404 response-code differential flagged as a security concern. For this deployment the tradeoff falls on semantic correctness: dossier UUIDs are cryptographically random (128 bits of entropy), the system runs behind SSO, `dossier.denied` audit events fire on every 403 so probing shows up in SIEM, and HTTP-client tooling relies on correct status codes for caching / routing / retries. Collapsing 403 to 404 would break that contract to close a leak with negligible real-world impact in this environment. RFC 9110 §15.5.5 permits 404-for-hidden-existence but it's not the right default here. Enumeration detection is a Wazuh dashboard + alert-rule concern, not an application concern — the `dossier.denied` stream already carries everything Wazuh needs (actor, dossier, reason, timestamp). | Decided. |
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
| ~~70~~ | 8 | ~~`test_requests.sh` outputs dead `/prov/graph` URL.~~ | ✅ **Fixed** — four echo sites updated to `/prov/graph/timeline` (the user-visible visualization route). `prov.py` module docstring also corrected — it documented a `/prov/graph` endpoint that doesn't exist; now lists the four real ones. Verified end-to-end: `/prov/graph/timeline` returns 401 without auth (route registered), the old `/prov/graph` returns 404 (proves the URL was dead). |
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

**M5. Executable specs that don't execute.** ✅ **Full relief shipped.** Two pieces:
- `scripts/ci_run_shell_spec.sh` — self-contained wrapper that stands up file_service + app + worker, waits for readiness, runs `test_requests.sh`, reports OK count / summary count / traceback count, exits 0/1/2/3 for pass/fail/stack-never-up/env-missing. Surfaced Bug 75 on first run.
- `.github/workflows/ci.yml` — the wrapper is now invoked by the `shell-spec` job on every PR and every push to `main`. The guidebook's Python code blocks still aren't validated (each references dotted-import paths for fictional classes; full relief there would need a fixture-module approach we haven't attempted), but the much higher-value shell-spec M5 target is now fully covered.

**M6. "Test" is a namespace, not a load-time gate.** Bug 71 accepted — deploy-time checklist keeps test activities out of production.

---

## Structural observations & duplications

Structural observations (57) unchanged from the 8-pass sweep; see earlier review revisions for full list. Key callouts worth revisiting:

- Worker split into `poll.py`/`execute.py`/`retry.py`/`signals.py` — the `worker.py` file grew this session with Bug 75's resilience logic. Split soon.
- `prov.py` at 523 lines (was 792) — further splitting possible.
- `prov_columns.py` layout algorithm (~280 lines inside `register_columns_graph`) wants extraction.

Duplications (22 remaining; 5 closed): D1 (graph-rowset loader), D2 (PROV-JSON build), D4 (audit emission boilerplate), D22 (emit_audit 7-field repetition — merged with D4 since they were the same pattern), D25 (PROV-JSON prefix building). The audit pair closed via `emit_dossier_audit` in `audit.py`; the graph-rowset cluster via `dossier_engine/prov_json.py`. D3, D5–D21, D23–D24, D26, D27 remain.

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

### Round 9 — CI wiring (GitHub Actions)
`.github/workflows/ci.yml` — four parallel jobs:
- **pytest** — runs all three test suites (common, engine, file_service) against a Postgres service container with health check. Pip cache keyed on `pyproject.toml` hash.
- **shell-spec** — installs the five repos, stages `/tmp/dossier_run/config.yaml` inline, invokes `scripts/ci_run_shell_spec.sh`. Uploads service logs as artifact on failure (`if: failure()`, 7-day retention).
- **doc-harnesses** — runs harness 1 + harness 3 in a separate job. No Postgres needed; clean signal for doc-drift failures.
- **migrations-append-only** — runs `scripts/check_migrations_append_only.py` with `fetch-depth: 0` so `origin/main` is available for the diff comparison.

Good GHA idioms applied: `concurrency:` group with `cancel-in-progress: true`, `actions/setup-python@v5` with built-in pip cache, service-container `pg_isready` health check, service logs uploaded only on failure. Runs on every `push` to main and every `pull_request` targeting main.

Verified: workflow YAML parses cleanly (four jobs, all steps listed); the migrations-check script round-trips correctly (exit 0 on clean tree, exit 1 with a clear named-file error when a migration is modified, reverts cleanly); CI config shape matches the dev `config.yaml` (same database URL, iri_base, plugins, auth mode).

### Round 10 — Bug 63 accepted + Duplication D4/D22 closure
- **Bug 63 reclassified as 📝 accepted** (not a real bug for this deployment) with HTTP-semantics rationale captured: dossier UUIDs carry 128 bits of entropy, the system sits behind SSO, `dossier.denied` audit events already fire on every 403 so probing is SIEM-visible, and collapsing 403→404 would break client/proxy tooling that relies on proper status codes. RFC 9110 §15.5.5 permits 404-for-hidden but it's not the right default here. Follow-up recorded: SIEM alert on high-frequency `dossier.denied` from a single actor makes enumeration *observable* without obscuring the existence signal.
- **`emit_dossier_audit` helper** added to `audit.py` — encapsulates the 5 fields that every dossier-scoped audit call repeated (`actor_id=user.id`, `actor_name=user.name`, `target_type="Dossier"`, `target_id=str(dossier_id)`, `dossier_id=str(dossier_id)`). Wraps the lower-level `emit_audit` which stays as the primitive for non-dossier-scoped events.
- **7 call sites converted** across `routes/access.py` (×2), `routes/activities.py` (×2), `routes/dossiers.py`, `routes/prov.py`. Boilerplate per site dropped from ~9 lines to ~5.
- **4 new tests** in `TestEmitDossierAudit`: wire-level equivalence with the long form (SIEM rule preservation), UUID stringification contract, reason+extra propagation, silent-when-unconfigured.
- **audit.py docstring** updated to show the new preferred usage pattern.

D4 and D22 both closed — they turned out to be the same pattern (audit emission boilerplate) under two review entries.

### Round 11 — `set_dossier_access` refactor (Obs-1, Obs-2, Obs-5 closed)
- **Three view-list constants** extracted in `dossier_toelatingen_repo/dossier_toelatingen/handlers/__init__.py`: `_AANVRAGER_VIEW`, `_BEHANDELAAR_VIEW`, `_BEHEERDER_VIEW`. Before the refactor these were inline at six `access_entries.append(...)` call sites — adding a new entity type meant six edits, and a miss silently hid the type from a role.
- **Three role-minting helpers** extracted: `_kbo_role`, `_rrn_role`, `_gemeente_role`. Encapsulates the role-string vocabulary; rename a prefix in one place rather than grepping across a file.
- **Bug fixed: duplicate `"external"` in aanvrager view** (kbo + rrn entries each had `"external"` twice). Inert today because access check does membership testing, but confusing — now fixed as a side effect of the constant extraction.
- **Behandelaar access restructured** on two axes: per-URI entries (each `oe:behandelaar`'s `uri` is itself a role on the access list for identity-scoped access) + one bare `"behandelaar"` entry for the global staff role. Dedup-by-URI preserved. The dual population is documented in a block comment at the call site so future readers don't have to reconstruct why both kinds of entries coexist.
- **Handler body shrunk from 76 lines to 58** with no view-list repetition anywhere.
- **Obs-3 (write-on-change) deliberately not done** — product decision to keep the full provenance graph means every activity run still produces a new `oe:dossier_access` version. The Observation stays open in the review as a possible future optimization if prov-graph churn ever becomes a problem.

- **16 new unit tests** in a brand-new `dossier_toelatingen_repo/tests/unit/test_set_dossier_access.py` — the `dossier_toelatingen_repo` had no tests directory before this round. Also added a minimal `[tool.pytest.ini_options]` with `asyncio_mode = "auto"` so the suite runs under the same convention as the engine. Tests use a lightweight `_FakeContext` that provides only the three methods the handler actually calls (`get_typed`, `get_singleton_typed`, `get_entities_latest`), no DB. Coverage: beheerder always present, aanvrager kbo+rrn variants, duplicate-`external` bug regression, verantwoordelijke organisatie, behandelaar empty/single/multiple/duplicate-URI/missing-URI cases, full-dossier end-to-end, view-constant invariants (aanvrager ⊆ behandelaar ⊆ beheerder).

### Verification performed
- **Test suite:** **740/740** (engine 687, toelatingen 16, signing 18, file_service 19). Grew by 67 tests across the engagement.
- **Shell spec via harness 2:** `bash scripts/ci_run_shell_spec.sh` → 25 OK assertions, 5 summary-pass lines, exit 0, zero tracebacks, zero worker crashes. D1–D9 green after the handler refactor, including the `wijsVerantwoordelijkeOrganisatieAan` side-effect path that calls `set_dossier_access`.
- **Harness 1, 2, 3** all green, all have synthetic-drift tests confirming they catch the bug shape they claim to catch.
- **CI workflow** authored, statically validated, and [dev]-extras fix applied so pytest-asyncio + httpx install in CI.

### Round 12 — Bug 70 + doc-drift on prov routes
- **Bug 70 fixed.** `test_requests.sh` had four echo lines pointing at a bare `/prov/graph` URL that doesn't exist on the server. Fixed to `/prov/graph/timeline` (the user-visible, visibility-filtered route). Verified end-to-end: timeline returns 401 without auth (route registered), the old bare URL returns 404 (proves the original URL was dead).
- **Incidental doc-drift fixed.** `prov.py`'s module docstring claimed the module exposed `/prov` and `/prov/graph` — the second endpoint doesn't exist. Docstring rewritten to list the four real endpoints (`/prov`, `/prov/graph/timeline`, `/prov/graph/columns`, `/archive`), so future readers don't build on the same wrong mental model. This is M4 territory but surfaces again here; a harness to lint module docstrings against the endpoint router is a possible future addition, not done this round.

### Where to go next (in priority order)

1. **Meta M2 — "silent skip" review.** Survey all `logger.error` + `pass` patterns (unregistered validators, audit emission failures, bijlage move per-file failures, etc.), decide case-by-case which should propagate vs swallow. Mostly a design discussion with targeted fixes at the end.
2. **Any of the open "must-fix" bugs worth taking on** — Bugs 5, 6, 7, 30, 55, 57, 58, 62 all still open. Priority depends on deployment context; 5 (access-check docstring/code drift at a security boundary) and 58 (unauthenticated `/validate` endpoint) are the most user-visible.

The two "optional" items previously on this list are now closed out:
- **Obs-3** (write-on-change for `set_dossier_access`) — deferred by product decision. Keeping the full provenance graph is intended behaviour, not a pending optimization. Filed alongside Bugs 31/45/71 under deferred/accepted.
- **Bug 63 follow-up** (enumeration alerting) — not an application concern. The `dossier.denied` stream already carries everything needed; dashboard + alert rule is a Wazuh config task, owned by SIEM operators.
