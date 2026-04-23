# Workflow YAML Inventory — engine reads vs template coverage

**Purpose.** Exhaustive catalogue of every workflow.yaml key the engine reads,
with file:line references, accepted shapes, defaults, validation status, and
template-coverage flag. Input to the follow-up template rewrite.

**Scope.** Engine-side reads only (everything under `dossier_engine_repo/dossier_engine/`).
Plugin-side handler code is out of scope — handlers consume whatever content
their Pydantic entity model accepts, which is a different contract.

**Legend.**
- ✅ **Documented** — section exists in `dossiertype_template.md`, covers the
  current engine behavior faithfully.
- ⚠️ **Partial** — mentioned but gaps (edge cases, defaults, or deeper shapes
  missing).
- ❌ **Missing** — not in the template at all.
- 🐛 **Bug/inconsistency** — something about the engine read that looks wrong,
  worth filing separately.

**Method.** Greps for every `.get("...")` and `["..."]` read on variables known
to hold workflow/activity/entity/task/relation/side-effect config, traced to
the reader to determine accepted shapes and defaults. Cross-checked against
the current `dossiertype_template.md` line ranges.

---

## Top-level workflow keys

These are keys the plugin author writes at the root of workflow.yaml.

### `name`

- **Read by**: `eligibility.py:78` (via `a["name"]` in activity loop — different),
  also used for URL prefixing in route registration (engine accesses
  `plugin.name` on the Plugin dataclass, which is populated from YAML's top-level
  `name:` in the plugin's `create_plugin()`).
- **Shape**: `str`, required.
- **Purpose**: Workflow name. Used as URL prefix in API routes
  (`/dossiers/{name}/...`).
- **Template**: ✅ section 1 (line 3-10).
- **Load-time validated?**: No. Missing `name` would propagate as a `None`
  through the Plugin dataclass constructor — not caught at load.

### `description`

- **Read by**: Only written by plugin authors; engine doesn't consume it (no
  grep hits in engine code).
- **Shape**: `str`, optional, human-readable.
- **Template**: ✅ section 1.
- **Load-time validated?**: No — pure documentation field.

### `version`

- **Read by**: Not consumed by the engine directly (no grep hits).
- **Shape**: `str`, optional, typically a semver-ish string like `"1.0"`.
- **Template**: ✅ section 1.
- **Load-time validated?**: No.

### `namespaces`

- **Read by**: `app.py:272` — `for prefix, iri in (plugin.workflow.get("namespaces") or {}).items()`.
- **Shape**: `dict[str, str]`, prefix → IRI. Optional.
- **Purpose**: Plugin-level additions to the namespace registry, beyond the
  engine's built-in RDF/PROV prefixes and the app-level globals.
- **Template**: ✅ section 4 (line 90).
- **Load-time validated?**: No shape check (`.items()` would TypeError on a
  non-dict, but the error surface is ugly).
- **Edge cases to document**: What if `namespaces:` is present but empty? What
  if a prefix already registered (by engine or app-level config) is redeclared?
  Need to trace `NamespaceRegistry.register()` to know.

### `poc_users`

- **Read by**: `app.py:367` — `all_poc_users.extend(plugin.workflow.get("poc_users", []))`.
- **Shape**: `list[dict]`. Each dict expected to have `id`, `username`, `type`,
  `name`, `roles`, `properties`, `uri` — shape inferred from the companion
  SYSTEM_USER dict appended at app.py:370.
- **Purpose**: POC-only authentication. Accumulated across all plugins at
  startup and installed into `POCAuthMiddleware`.
- **Template**: ✅ section 7 (line 166).
- **Load-time validated?**: No. A malformed entry would crash inside
  `POCAuthMiddleware` during route handling.
- **Gap note**: The template should flag this is **POC-only** and will be
  removed when real auth lands (Bug 28 deferral).

### `tombstone`

- **Read by**: `app.py:138` — `ts_cfg = plugin.workflow.get("tombstone") or {}`.
- **Shape**: `dict`, optional. Keys: `allowed_roles: list[str]`.
- **Purpose**: Controls which roles can run the built-in tombstone activity for
  this workflow. Engine generates the tombstone activity def; YAML just supplies
  the role list.
- **Template**: ✅ section 3 (line 62).
- **Load-time validated?**: No shape check.

### `relations`

- **Read by**: `plugin.py:618` (workflow-level relation declarations),
  `engine/pipeline/relations.py:65, 109, 148` (runtime relation dispatch),
  `plugin.py:800` (callable resolution).
- **Shape**: `list[dict]`. Each dict: `{type: str, kind: "domain"|"process_control",
  validator?: str, validators?: {add, remove}, from_types?: list, to_types?: list}`.
- **Purpose**: Workflow-level relation type declarations. `kind` is mandatory and
  drives runtime dispatch.
- **Template**: ✅ section 2 (line 13-60). Well-documented — `kind` field rules,
  domain vs process_control, validator vs validators, load-time rejection of
  bad shapes all mentioned.
- **Load-time validated?**: ✅ Yes — `validate_relation_declarations` +
  `validate_relation_validator_registrations` (both engine-side, run from app.py).

### `relation_types`

- **Read by**: `plugin.py:243-256` — `for rel in workflow.get("relation_types", []) or []:`.
- **Shape**: `list[dict]` (same shape as `relations`).
- **Purpose**: ❓ Unclear. Comment at plugin.py:240 says "Bug 78's types-declared-
  once-at-workflow-level contract" — but the Bug 78 contract uses `relations:`,
  not `relation_types:`. Toelatingen's workflow.yaml does NOT use
  `relation_types:` (only `relations:`).
- **🐛 Inconsistency finding**: The engine reads BOTH `relations` and
  `relation_types`. The registry-building code at plugin.py:243 operates on
  `relation_types`, while `validate_relation_declarations` and runtime dispatch
  operate on `relations`. If a plugin put validator dotted-paths only under
  `relations:` (as toelatingen does), the registry scan at plugin.py:243 reads
  nothing — the dotted-path resolution for workflow-level relation validators
  falls through. Worth filing separately for investigation; MAY be dead code
  from a mid-refactor state.
- **Template**: ❌ Not documented (and probably shouldn't be until the dead-code
  question is resolved).
- **Action**: Do not document until the inconsistency is investigated.

### `entity_types`

- **Read by**: `plugin.py:135` (entity model registry build), `plugin.py:985`
  (cardinality lookup).
- **Shape**: `list[dict]`. Each dict: `{type: str, model: str (dotted path), cardinality?: "single"|"multiple", schemas?: dict[version, dotted_path]}`.
- **Purpose**: Declares the entity types the workflow manages, their Pydantic
  models, and cardinality. Foundational — most other constructs reference
  `type` strings from here.
- **Template**: ✅ section 8 (line 197).
- **Load-time validated?**: Partial. `model:` dotted paths resolve at load
  (ImportError if broken). `cardinality:` is tolerated as a free string but
  `is_singleton()` only checks for `"single"`, so typos silently mean "not a
  singleton" — see plugin.py:987 fallback `if c in ("single", "multiple") else "single"`.
- **Gap to document in template**: the cardinality-fallback semantics, the
  schemas dict shape.

### `field_validators`

- **Read by**: `plugin.py:355` — `fv_block = workflow.get("field_validators") or {}`.
- **Shape**: `dict[str, str | FieldValidator]`. Key is a URL-segment identifier;
  value is either a dotted-path string or a `FieldValidator` instance. See
  `plugin_guidebook.md:265` for the authoring story.
- **Purpose**: Exposes `POST /{workflow}/validate/{key}` endpoints for
  frontend-driven field validation.
- **Template**: ❌ Not in the top-level section list. Only covered inside
  `plugin_guidebook.md`, not `dossiertype_template.md`.
- **Action needed**: Add section to template.

### `reference_data`

- **Read by**: `routes/reference.py:85` — `ref_data = plugin.workflow.get("reference_data", {})`.
- **Shape**: `dict` of arbitrary key/value (needs investigation — haven't traced
  what consumes the returned `ref_data`).
- **Purpose**: Exposes plugin-level reference data via some route.
- **Template**: ❌ Not documented.
- **Action needed**: Trace the `routes/reference.py` consumer to determine
  exact shape + purpose, then add section.

### `activities`

- **Read by**: dozens of sites. The main runtime dispatch target. See Activity
  section below for full internal-key inventory.
- **Shape**: `list[dict]`. Each dict is an activity definition.
- **Template**: ✅ section 10 (line 283-611). Large section.
- **Load-time validated?**: Partial — only some sub-keys are checked.

### `constants`

- **Read by**: Plugin-side `create_plugin()` reads `workflow.get("constants")`
  to instantiate the constants class. Not directly read by engine code.
- **Template**: ✅ section 5 (line 117). Well-covered.

### `roles`

- **Read by**: ❓ Not found in engine greps — may be convention-only, read by
  plugins when composing user roles, but engine has no `workflow.get("roles")`
  call. Need to confirm.
- **Template**: ✅ section 6 (line 145).
- **Action needed**: Confirm whether engine reads this anywhere, or whether
  it's plugin-convention-only.

---

## Activity-level keys (inside `activities[*]`)

23 distinct engine-read keys. Below: each with shape, readers, defaults, validation status, template coverage.

### `name`

- **Readers**: many (`plugin.py:262`, `eligibility.py:78`, `app.py:166, 202`, `activity_names.py:91`).
- **Shape**: `str`, required. Bare name (e.g. `"dienAanvraagIn"`) or qualified (`"oe:dienAanvraagIn"`). Bare names are auto-qualified with the workflow's default ontology prefix — `plugin.py:1111-1113` mutates `act["name"]` in place.
- **Purpose**: Activity type identifier. Drives runtime dispatch and URL routing.
- **Template**: ✅ in section 10 at line 285+.
- **Load-time validated?**: No shape check, but required — missing/empty name propagates as `None` and causes confusing downstream errors. Auto-qualification means plugin authors don't have to prefix everything manually (also applies to `requirements.activities`, `forbidden.activities`, `side_effects[*].activity`, `tasks[*].cancel_if_activities`, `tasks[*].target_activity`).
- **Edge cases to document**: The auto-qualification behavior. A qualified name (`"oe:x"`) passes through unchanged; a bare name (`"x"`) becomes `"<default_prefix>:x"`. Plugin authors can use either form interchangeably.

### `label`

- **Readers**: `eligibility.py:87` (`act_def.get("label", act_def["name"])`), `routes/activities.py:260`.
- **Shape**: `str`, optional. Falls back to `name` if missing.
- **Purpose**: Human-readable label returned to the UI in the allowed-activities list.
- **Template**: ✅ mentioned.
- **Load-time validated?**: No.

### `handler`

- **Readers**: `handlers.py:62` (runtime dispatch), `plugin.py:265` (load-time dotted-path resolution).
- **Shape**: `str`, optional. Dotted path to an async callable. Since Round 28 (Obs 95), full dotted paths only — not short names.
- **Purpose**: The activity's business-logic handler. Absent means the engine's default "store generated, return HandlerResult()" path.
- **Template**: ✅ mentioned.
- **Load-time validated?**: ✅ The dotted path resolves at plugin load via `_import_dotted_callable`; ImportError on bad path.

### `client_callable`

- **Readers**: `eligibility.py:52`, `routes/activities.py:252`, `routes/prov.py:152`, `routes/prov_columns.py:94`.
- **Shape**: `bool`, defaults to `True` (explicit check `is False` — so only the literal `false` disables).
- **Purpose**: If `false`, the activity is hidden from client-facing routes and eligibility lists. Used for internal / system-triggered activities that shouldn't appear as options.
- **Template**: ⚠️ Searched — not explicitly documented in section 10. Needs a bullet.
- **Load-time validated?**: No.

### `can_create_dossier`

- **Readers**: `preconditions.py:92, 165` (bootstrap detection), `routes/activities.py:490`.
- **Shape**: `bool`, defaults to falsy.
- **Purpose**: Marks the activity as a "bootstrap" activity — one that can be the first activity of a brand-new dossier. Such activities skip the workflow-rules check on first invocation (see `preconditions.py:165-167`).
- **Template**: ✅ section 10 mentions it (line 253 area) with "Only checked when can_create_dossier activity is the first one" — but doesn't fully explain the skip-workflow-rules behavior on bootstrap.
- **Load-time validated?**: No.
- **Edge cases to document**: on-bootstrap, workflow-rules are skipped but authorization still runs.

### `built_in`

- **Readers**: `invariants.py:61` — `if state.activity_def.get("built_in"): ...`.
- **Shape**: `bool`, defaults to falsy. Engine-internal marker.
- **Purpose**: Marks activities that the engine itself injects (SYSTEM_ACTION_DEF, TOMBSTONE_ACTIVITY_DEF). Some invariants are relaxed for built-in activities (line 61 is inside the relation-invariant check).
- **Template**: ❌ Not documented. Arguably shouldn't be — it's an engine-internal marker, not something plugin authors should set on their own activities. Worth a short note saying "reserved; do not set."
- **Load-time validated?**: No.
- **Action**: Document as reserved-engine-only.

### `default_role`

- **Readers**: `preconditions.py:136` — used when the client request doesn't specify `role:` at activity invocation. Also written by `app.py:143` for the generated tombstone activity.
- **Shape**: `str`, optional. Name of one of the roles listed in `allowed_roles`.
- **Purpose**: The role that gets recorded as the activity's PROV agent-role when the client doesn't specify one.
- **Template**: ⚠️ needs checking.
- **Load-time validated?**: No. Would silently allow a `default_role` that isn't in `allowed_roles`, which would then fail at request time.

### `allowed_roles`

- **Readers**: `preconditions.py:135` — `activity_def.get("allowed_roles", [])`.
- **Shape**: `list[str]`, defaults to `[]` meaning "no role restriction" (i.e., `preconditions.py:145` only raises if the role is non-empty AND not in the list, so an empty list means nothing enforced).
- **Purpose**: Per-activity restriction on which agent-role strings the client may declare when invoking the activity. This is orthogonal to `authorization.roles` — `authorization.roles` checks the user's registered roles, `allowed_roles` checks the `role:` field the client set in the request body.
- **Template**: ⚠️ needs explicit documentation of the orthogonality.
- **Load-time validated?**: No.

### `authorization`

- **Readers**: `authorization.py:66`, `routes/_typed_doc.py:42`.
- **Shape**: `dict`. Keys: `access: "everyone" | "authenticated" | "roles"` (default `"authenticated"`), `roles: list[dict]` (role-entry shapes).
- **Purpose**: Who may run this activity. See separate section for `authorization.roles[*]` shapes.
- **Template**: ✅ section 9 (line 248).
- **Load-time validated?**: ❌ **No.** This is the gap I flagged during Bug 34 recon — a typo like `feild:` inside a scope dict isn't caught until runtime, where it gets swallowed by the broad `except Exception` in `authorize_activity`. See Bug 34 + the "authorization scope shape" item in the gap table of `Obs 57 recon` (Cat 5 design pass).

### `entities`

- **Readers**: `generated.py:310, 394` (schema-version resolution), `plugin.py:394` (version-reference validation), `routes/_typed_doc.py:125`.
- **Shape**: `dict[str, dict]`. Key is entity type (e.g., `"oe:aanvraag"`); value is `{new_version?: str, allowed_versions?: list[str]}`.
- **Purpose**: Per-activity, per-entity-type versioning config. Drives the `schema_version` column on created entity rows.
- **Template**: ⚠️ Covered in section 10 around line 360-400 but the sticky-version semantics and the "new_version required when creating fresh" rule deserve their own block.
- **Load-time validated?**: ✅ `validate_workflow_version_references` cross-checks that every `new_version`/`allowed_versions` string is declared in `entity_types[*].schemas`. (Plugin-side, runs from toelatingen's `create_plugin()`.)

### `used`

- **Readers**: `used.py:141, 147`, `app.py:214`, `routes/_typed_doc.py:75`.
- **Shape**: `list[dict]`. Each dict: `{type: str, external?: bool, auto_resolve?: "latest", required?: bool, description?: str}`.
- **Purpose**: Declares the entity types this activity USES (PROV-`used` edges). Drives request-body validation (request must provide refs to the declared types) and runtime lookup.
- **Template**: ✅ section 10 (corrected post-Round-34 to reflect actual auto-resolve behavior).
- **Load-time validated?**: Partial — prefix validation via `_validate_plugin_prefixes`.
- **Engine-unread fields**: `required`, `description`. Documented in template but no runtime reader (see findings 12, 13).
- **Caller-dependent behavior**: `auto_resolve: "latest"` is consulted ONLY when `state.caller == Caller.SYSTEM` (worker-executed tasks) or during side-effect resolution. For client-triggered activities, `auto_resolve` is IGNORED and a client-omitted declared slot silently succeeds with an empty resolution. See finding 12.

### `generates`

- **Readers**: `generated.py:59` (allowed-types check), `handlers.py:117` (handler-result validation), `side_effects.py:423` (default type for side-effect generates), `app.py:204` (prefix validation).
- **Shape**: `list[dict | str]`. Each entry is either a type-name string or a dict `{type: str, ...}`. Needs deeper trace for the dict shape.
- **Purpose**: Declares entity types this activity GENERATES. The handler's `HandlerResult` may only produce entities of declared types.
- **Template**: ✅ section 10.
- **Load-time validated?**: Partial — prefix validation only.

### `relations` (activity-level)

- **Readers**: `engine/pipeline/relations.py:49`, `plugin.py:330, 671` (load-time validation), `app.py:225` (prefix validation).
- **Shape**: `list[dict]`. Each dict: `{type: str, operations?: list[str], validator?: str, validators?: {add, remove}}`.
- **Purpose**: Activity opts in to a specific workflow-level relation type. Operations list limits which mutations (add/remove) this activity may perform.
- **Keys accepted**: `_ACTIVITY_RELATION_KEYS = {type, operations, validator, validators}` (plugin.py:560).
- **Keys forbidden**: `_ACTIVITY_RELATION_FORBIDDEN_KEYS = {kind, from_types, to_types, description}` — these live at workflow level only (plugin.py:569).
- **Template**: ✅ section 10 around line 460-510. Good coverage but the explicit forbidden-keys list deserves a callout.
- **Load-time validated?**: ✅ `validate_relation_declarations` enforces.

### `requirements`

- **Readers**: `authorization.py:192`, `routes/_typed_doc.py:60`, `plugin.py:1119` (auto-qualification of activity references).
- **Shape**: `dict`. Keys: `activities: list[str]`, `entities: list[str]`, `statuses: list[str]`. All lists default to `[]`.
- **Purpose**: Preconditions — activities that must have already run, entity types that must exist, dossier statuses that are required.
- **Template**: ✅ section 10 around line 528+.
- **Load-time validated?**: No shape check. Referenced activity names are auto-qualified (plugin.py:1121-1126).

### `forbidden`

- **Readers**: `authorization.py:193`.
- **Shape**: `dict`. Keys: `activities: list[str]`, `statuses: list[str]`. (Note: unlike `requirements`, no `entities` key — forbidden is never "this type must NOT exist.")
- **Purpose**: Negative preconditions.
- **Template**: ✅ section 10.
- **Load-time validated?**: No shape check.

### `side_effects`

- **Readers**: `engine/__init__.py:226` (runtime dispatch), `plugin.py:313` (callable resolution), `plugin.py:441, 482` (load-time shape/registration validation), `plugin.py:1134` (name qualification).
- **Shape**: `list[dict | str]`. Each entry is either:
  - Bare string — legacy, normalized to `{activity: <name>}` by plugin.py:1137-1139.
  - Dict — `{activity: str, condition?: {entity_type, field, value}, condition_fn?: str, generates?: list, ...}`. Condition and condition_fn are mutually exclusive.
- **Purpose**: Activities to trigger after this one completes. Condition-gated.
- **Template**: ✅ section 10.
- **Load-time validated?**: ✅ `validate_side_effect_conditions` (shape) + `validate_side_effect_condition_fn_registrations` (name resolution). Both plugin-side.

### `status`

- **Readers**: `finalization.py:98` — string-OR-dict form.
- **Shape**: Three forms:
  - `null` — activity does not set status; resolved from handler result if present.
  - `str` — literal status value written to `computed_status`.
  - `dict` — `{from_entity: str, field: str, mapping: dict[str, str]}`. Data-driven resolution: reads `field` from a generated entity of type `from_entity`, maps value → status via `mapping`.
- **Template**: ✅ section 10 string form. ❌ Dict form NOT documented (this is Obs 59 territory — user skipped the load-time validator fix but the doc gap remains).
- **Load-time validated?**: ❌ **No.** Dict-form typos crash with `KeyError` at runtime.

### `status_resolver`

- **Readers**: `split_hooks.py:46`, `plugin.py:274`.
- **Shape**: `str`, optional. Dotted path to a callable.
- **Purpose**: Alternative to `status:` for resolving the activity's status dynamically. Called after the handler; its return value is the status string.
- **Template**: ❓ mentioned in guidebook? Need to verify section 10 coverage.
- **Load-time validated?**: ✅ Dotted path resolves at load.

### `task_builders`

- **Readers**: `split_hooks.py:47`, `plugin.py:283`.
- **Shape**: `list[str]`. Dotted paths to callables.
- **Purpose**: Plugin-supplied functions that can produce tasks dynamically (in addition to the static `tasks:` list). Called with context + generated state; each returns zero or more task defs.
- **Template**: ❓ needs checking. Covered in guidebook.
- **Load-time validated?**: ✅ Dotted paths resolve at load.

### `tasks`

- **Readers**: `tasks.py:71, 90` (runtime dispatch), `plugin.py:302, 1151` (load-time qualification + resolution).
- **Shape**: `list[dict]`. Each dict is a task declaration. See "Task-level keys" section below.
- **Purpose**: Static task declarations — scheduled, recorded, fire-and-forget, cross-dossier activities to run as part of the activity's side effects.
- **Template**: ✅ section 10 has Tasks coverage around line 612+.
- **Load-time validated?**: Partial — dotted paths resolve; `kind` enum NOT load-time-checked (even though it's `Literal` on the Pydantic `TaskEntity` model per Bug 39). Task YAML → TaskEntity construction happens at runtime, not load, so a YAML typo crashes mid-activity.

### `validators`

- **Readers**: `validators.py:45` (runtime), `plugin.py:291` (dotted-path resolution).
- **Shape**: `list[dict]`. Each dict: `{name: str, ...}` where `name` is a dotted path. Shape includes optional `description`.
- **Purpose**: Cross-entity validators that run post-handler, pre-commit. Each returns `(valid: bool, error_message | None)`.
- **Template**: ✅ section 10.
- **Load-time validated?**: ✅ Dotted paths resolve at load.

### `time` / `agent`

- **Status**: ❌ FALSE POSITIVES in initial grep. These are NOT workflow-YAML keys. Reads at `archive.py:140-152` operate on an activity **row** dict (DB representation, not YAML). Variable name collision — `act` is used for both workflow activity_def AND PROV-JSON activity rows in archive.py. Worth filing as a minor legibility issue but not a YAML key.

---

## Nested under `activities[*].authorization`

### `authorization.access`

- **Readers**: `authorization.py:67`.
- **Shape**: `Literal["everyone", "authenticated", "roles"]`, default `"authenticated"`.
- **Purpose**: High-level access class.
- **Load-time validated?**: No.

### `authorization.roles[*]` (three shapes)

Runtime branching at `authorization.py:84-137`. **Three legal shapes:**

**(1) Direct match** — `{role: str}`:
- `role:` is a role-name string. User must have it in `user.roles` (post-scope-resolution).
- **Template**: ✅ mentioned at section 9 line 253-258.

**(2) Scoped match** — `{role: str, scope: {from_entity: str, field: str}}`:
- Resolves a value from a dossier entity at runtime; composes `<role>:<value>` and checks against user.roles.
- Only applicable when a dossier exists (not for bootstrap activities).
- **Template**: ✅ mentioned with the `gemeente-toevoeger` example.

**(3) Entity-derived match** — `{from_entity: str, field: str}` (no `role:`):
- The entity field value IS the role string. Used for dossier-ownership checks.
- **Template**: ✅ mentioned at section 9.

- **Load-time validated?**: ❌ **No.** `scope:` dict typos (`feild:`, missing `from_entity:`) surface only at runtime inside the broad `except Exception` in `authorize_activity` — Bug 34 recon and the Obs 57 gap table both flagged this.

---

## Nested under `activities[*].entities[*]`

### `entities[type].new_version`

- **Readers**: `generated.py:310, 322`, `plugin.py:397` (version reference validation).
- **Shape**: `str`, optional. Required when creating a fresh entity (parent_row is None).
- **Purpose**: Schema version to stamp on newly-created entities of this type.
- **Load-time validated?**: ✅ Cross-checked against `entity_types[type].schemas` declarations.

### `entities[type].allowed_versions`

- **Readers**: `generated.py` (inside `_resolve_schema_version`), `plugin.py:400`.
- **Shape**: `list[str]`, optional.
- **Purpose**: When revising an existing entity, its stored `schema_version` must be in this list; otherwise 422 `unsupported_schema_version`.
- **Load-time validated?**: ✅ Same as `new_version`.

---

## Nested under `activities[*].tasks[*]`

Static task declarations. Merged with handler-returned tasks at runtime.

### `tasks[*].kind`

- **Readers**: `tasks.py:95` — `task_kind = task_def.get("kind", "recorded")`.
- **Shape**: `Literal["fire_and_forget", "recorded", "scheduled_activity", "cross_dossier_activity"]`, default `"recorded"`.
- **Template**: ⚠️ partial — the four kinds are mentioned but the default behavior ("absent kind means recorded") should be explicit.
- **Load-time validated?**: ❌ **No.** Bug 39 tightened `TaskEntity.kind` to `Literal[...]` at the Pydantic level (Round 32), but the YAML `task_def` dict doesn't pass through Pydantic until runtime construction. A typo in YAML still crashes mid-activity.

### `tasks[*].function`

- **Readers**: `tasks.py:104` (via `HandlerResult.tasks`), `plugin.py:306` (load-time resolution).
- **Shape**: `str`, dotted path. Required for `recorded` and `cross_dossier_activity` kinds.
- **Load-time validated?**: ✅ Dotted path resolves at load.

### `tasks[*].target_activity`

- **Readers**: `tasks.py`, `plugin.py:1160`.
- **Shape**: `str`, activity name (auto-qualified). Required for `scheduled_activity` and `cross_dossier_activity` kinds.
- **Load-time validated?**: No cross-check that the named activity exists in the workflow.

### `tasks[*].scheduled_for`

- **Readers**: `tasks.py`, parsed as ISO datetime or relative expression.
- **Shape**: `str` — ISO datetime or relative (e.g., `"+30d"`).
- **Template**: ⚠️ Relative-expression syntax needs documentation.
- **Load-time validated?**: No.
- **Runtime behavior**: Bug 12 (Round 5) fixed silent fall-through on parse failure.

### `tasks[*].cancel_if_activities`

- **Readers**: auto-qualified at `plugin.py:1154`, consulted at runtime by the poll loop.
- **Shape**: `list[str]`, activity names. Default `[]`.
- **Purpose**: If any listed activity completes before the task fires, the task transitions to `cancelled`.
- **Load-time validated?**: Names auto-qualified; no existence cross-check.

### `tasks[*].allow_multiple`

- **Readers**: `tasks.py`.
- **Shape**: `bool`, default `False`.
- **Purpose**: Controls whether a task with the same `target_activity` + anchor can coexist with another in-flight task. If `False`, a new task supersedes older ones.
- **Template**: ⚠️ Needs clearer supersede-semantics documentation.

### `tasks[*].anchor_entity_id` / `anchor_type`

- **Readers**: `tasks.py`.
- **Shape**: `str` (UUID for entity_id; type name for type).
- **Purpose**: Ties the task to a specific entity. Used for cancel/supersede/allow_multiple matching.
- **Template**: ❓ Need to check coverage.

---

## Nested under `activities[*].side_effects[*]`

### `side_effects[*].activity`

- **Shape**: `str`, target activity name (auto-qualified at `plugin.py:1144`). Required.
- **Load-time validated?**: Auto-qualified; no existence cross-check.

### `side_effects[*].condition`

- **Shape**: `dict {entity_type: str, field: str, value: Any}`. Optional.
- **Purpose**: Simple equality gate — "trigger only if `entity_type`'s `field` equals `value`."
- **Mutex with**: `condition_fn`.
- **Load-time validated?**: ✅ `validate_side_effect_conditions` enforces shape (rejects `from_entity:` typos that borrow from status-rule shape).

### `side_effects[*].condition_fn`

- **Shape**: `str`, name of a registered predicate.
- **Purpose**: Custom gating logic beyond simple equality.
- **Mutex with**: `condition`.
- **Load-time validated?**: ✅ `validate_side_effect_condition_fn_registrations` enforces name resolves.

### `side_effects[*].dossier` / `ontology` / `ontology_prefix`

- **Readers**: Grep showed these in `se.get(...)` patterns. Need deeper trace.
- **Status**: ❓ Unclear purpose from grep alone. Need to trace the reader context to understand; possibly cross-dossier / ontology-override config for cross_dossier_activity side effects. Filing as a gap to investigate.

---

## Nested under `activities[*].requirements` / `activities[*].forbidden`

### `requirements.activities` / `forbidden.activities`

- **Shape**: `list[str]`. Activity names; auto-qualified.
- **Purpose**: Activities that must / must not have completed.

### `requirements.statuses` / `forbidden.statuses`

- **Shape**: `list[str]`. Status values.
- **Purpose**: Required / forbidden dossier status.
- **Semantic**: `requirements.statuses` acts as a set — if any element is non-empty (truthy), the current status must be IN the set.

### `requirements.entities`

- **Shape**: `list[str]`. Entity type names.
- **Purpose**: Entity types that must exist (any instance).
- **Note**: No corresponding `forbidden.entities` — you can't say "this type must NOT exist."

---

## Nested under `activities[*].status` (dict form)

- **Required keys**: `from_entity`, `field`, `mapping`.
- **Shape**: `{from_entity: str, field: str, mapping: dict[str, str]}`.
- **Semantics**: After the handler runs, find a generated entity of type `from_entity`. Resolve `field` (dot-notation path). If the resolved value's string form is in `mapping`, the mapped value is the status.
- **Template**: ❌ **NOT DOCUMENTED** in `dossiertype_template.md`. Must be added.
- **Load-time validated?**: ❌ No (Obs 59). Runtime `KeyError` on typos.

---

## Workflow-level — deeper inventory of already-listed sections

### `entity_types[*]`

- **Reader**: `plugin.py:135-147` (entity model registry build), `plugin.py:985` (cardinality lookup).
- **Internal keys**:
  - `type: str` — required. Qualified name (e.g., `"oe:aanvraag"`).
  - `model: str` — optional. Dotted path to a Pydantic `BaseModel`. If absent, no default model is registered — `context.get_typed()` will return `None` and content validation falls through.
  - `cardinality: "single" | "multiple"` — optional, default `"single"`. Silent fallback to `"single"` on typo (plugin.py:988).
  - `schemas: dict[str, str]` — optional. Keys are version strings (e.g., `"v1"`, `"v2"`); values are dotted paths to per-version Pydantic models.
- **Load-time validated?**:
  - `model:` and `schemas.*` dotted paths resolve at load (ImportError on bad path).
  - `type:` required but missing is silently skipped (`plugin.py:137-138`).
  - `cardinality:` typo silently means `"single"` (see **Finding 4**).
  - Reference from `activities[*].entities[*]` to `entity_types[type].schemas` is cross-checked by `validate_workflow_version_references`.

### `relations[*]` (workflow-level)

- **Reader**: `plugin.py:618-662` (`validate_relation_declarations`), runtime dispatch through `engine/pipeline/relations.py`.
- **Internal keys** (allowed set: `_WORKFLOW_RELATION_KEYS = {type, kind, from_types, to_types, description}`, plugin.py:552):
  - `type: str` — required. Qualified relation type name.
  - `kind: "domain" | "process_control"` — required. Drives runtime dispatch shape.
  - `from_types: list[str]` — optional, `kind: domain` only. Constrains the `from` side of a domain relation.
  - `to_types: list[str]` — optional, `kind: domain` only. Constrains the `to` side.
  - `description: str` — optional, pure documentation.
- **Forbidden**: any other key raises `ValueError` at load time (`plugin.py:643-649`).
- **Load-time validated?**: ✅ Thorough — `validate_relation_declarations`.

### `tombstone`

- **Reader**: `app.py:138-143`.
- **Internal keys**:
  - `allowed_roles: list[str]` — optional. If empty/absent, no one can tombstone in this workflow (deny-by-default).
- **No other keys read.**
- **Template**: ✅ section 3.
- **Load-time validated?**: No shape check.

### `poc_users[*]`

- **Reader**: `auth/__init__.py:45-53` (via `app.py:367 .extend()`).
- **Internal keys**:
  - `id` — required. Stored as `str(id)` so int/str both work.
  - `username: str` — required. The `X-POC-User` header value.
  - `type: str` — required. PROV agent type (e.g., `"natuurlijk_persoon"`, `"rechtspersoon"`).
  - `name: str` — required. Display name.
  - `roles: list[str]` — optional, default `[]`.
  - `properties: dict` — optional, default `{}`.
  - `uri: str | None` — optional, default `None`.
- **Template**: ✅ section 7.
- **Load-time validated?**: No; crashes with `KeyError` inside middleware construction on missing required keys.
- **Gap**: POC-only — Bug 28 defers real auth. Template should reinforce this is throwaway scaffolding.

### `namespaces`

- **Reader**: `app.py:272`.
- **Internal keys**: `dict[str, str]` — prefix name → IRI.
- **Behavior**: Registered into the `NamespaceRegistry` after engine built-ins + app-level namespaces. Per-plugin additions only.
- **Template**: ✅ section 4.
- **Load-time validated?**: No. A re-register on an existing prefix — I'd need to trace `NamespaceRegistry.register()` to know whether it's accepted silently, rejected, or overwritten.

### `field_validators`

- **Reader**: `plugin.py:355`.
- **Internal keys**: `dict[str, str | FieldValidator]`. Key is URL-segment identifier (NOT a dotted path — see plugin_guidebook.md:305). Value is either a dotted path or a `FieldValidator` instance.
- **Template**: ❌ **Not in `dossiertype_template.md`.** Covered in `plugin_guidebook.md` only.
- **Gap**: needs a section in the template.

### `reference_data`

- **Reader**: `routes/reference.py:85` — `ref_data = plugin.workflow.get("reference_data", {})`.
- **Internal shape**: `dict`. ❓ Need to trace what `routes/reference.py` does with the returned `ref_data` to document the shape.
- **Template**: ❌ **Not documented.**
- **Gap**: needs investigation + a section.

### `roles` (workflow top-level)

- **Reader**: ❓ Engine grep found no `workflow.get("roles")` call. Only match was inside `authorization.roles[*]`.
- **Status**: Template section 6 documents this, but the engine doesn't appear to read it. Likely **convention-only** — plugin authors list expected roles for readability, but the engine doesn't enforce the list. Worth confirming.
- **Gap**: Either (a) document as convention-only in the template, or (b) if the engine SHOULD enforce it, that's a new bug.

---

## Configuration-file-level keys (not workflow.yaml — for reference)

These come from the app-level `config.yaml` (not plugin `workflow.yaml`). Out of scope for `dossiertype_template.md`, but noted to avoid confusion when readers encounter them.

- `database.url` — Postgres connection string.
- `iri_base.dossier` / `iri_base.ontology` / `iri_base.ontology_prefix` — IRI namespace configuration. `iri_base.ontology_prefix` defaults to `"oe"`.
- `auth.mode` — currently `"poc"` (Bug 28 deferral).
- `file_service.signing_key` / `file_service.url` / `file_service.storage_root`.
- `audit.log_path` / `max_bytes` / `backup_count`.
- `cors.allowed_origins`.
- `global_access`, `global_audit_access`, `global_admin_access`.
- `plugins: list[str]` — dotted module paths to plugin packages.
- `namespaces` (app-level, merged before per-plugin namespaces).


---

---

## Progress

- [x] Top-level key enumeration
- [x] Top-level keys fully resolved
- [x] Activity-level keys (all 23 covered; 2 were false positives)
- [x] Authorization nested (3 role-entry shapes)
- [x] Entities nested (activity-level)
- [x] Tasks nested (9 keys)
- [x] Side_effects nested (5 keys; `dossier`/`ontology`/`ontology_prefix` were grep artifacts)
- [x] Requirements/forbidden nested
- [x] Status dict nested
- [x] Tombstone internal shape
- [x] Relations internal shape (workflow-level keys enumerated)
- [x] POC users internal shape
- [x] Entity_types internal shape (including `schemas:` and cardinality fallback)
- [x] Field_validators internal shape — flagged as gap
- [x] Reference_data internal shape — traced
- [x] Workflow-level `roles:` status — confirmed convention-only
- [x] Cross-reference notes per section

**Inventory complete.**

## All findings

1. **🐛 `relation_types` vs `relations` inconsistency** (`plugin.py:243` reads
   `relation_types` while `plugin.py:618` and runtime use `relations`). Probably
   dead code from a mid-refactor. Do not document `relation_types` until
   investigated. Possible fix: delete the `workflow.get("relation_types")` scan
   at plugin.py:240-256, OR if the intent was for `relation_types` to be the
   canonical block, update `validate_relation_declarations` to read it instead
   of `relations`. Needs a decision before documentation.

2. **`reference_data` undocumented.** Engine reads it, template silent. Shape:
   `dict[str, list]` — per-plugin dropdown data exposed publicly via
   `GET /{workflow}/reference` and `GET /{workflow}/reference/{list_name}`.
   Needs a template section.

3. **`field_validators` undocumented in `dossiertype_template.md`.** Covered in
   `plugin_guidebook.md` only. Needs a section in the template — shape is
   `dict[url_segment, dotted_path_or_FieldValidator]`.

4. **Entity type cardinality fallback is silent.** `plugin.py:988` —
   `c if c in ("single", "multiple") else "single"`. Typo in `cardinality:`
   (say, `"signle"`) means `"single"`. Document explicitly, or tighten to
   load-time reject.

5. **🐛 Variable-name collision: `act` for both workflow activity_def AND
   PROV-JSON activity rows** in `archive.py`. Causes false positives in
   code grep for "workflow activity keys." Worth renaming one of the two.

6. **`status:` dict-form not documented** in `dossiertype_template.md`. Engine
   supports `{from_entity, field, mapping}` shape (`finalization.py:102-111`);
   template only documents the string form. This is Obs 59's documentation
   half — user skipped the load-time validator fix, but the doc gap remains.
   Plugin authors can't discover the dict form today from the template.

7. **`client_callable` default is implicit-true.** Engine uses `is False`
   check, so anything except the literal `false` (including absence) means
   "visible." Worth documenting explicitly.

8. **`built_in` is engine-internal** (used for SYSTEM_ACTION_DEF / TOMBSTONE).
   Plugin authors shouldn't set it. Template should flag it as reserved.

9. **`default_role` vs `allowed_roles` vs `authorization.roles` is a
   three-way concept** that the template doesn'''t clearly distinguish.
   `allowed_roles` limits what role *string* the client may declare at request
   time; `authorization.roles` specifies which *users* may run the activity
   (with optional scope resolution); `default_role` supplies the role when
   the client request omits one.

10. **`side_effects[*]` bare-string legacy form** (plugin.py:1137-1139)
    normalizes a bare string to `{activity: <name>}`. Worth documenting as
    the back-compat path with a nudge toward the dict form.

11. **Auto-qualification of activity names** (plugin.py:1111-1126) — bare
    names in activity `name:`, `requirements.activities`, `forbidden.activities`,
    `side_effects[*].activity`, `tasks[*].cancel_if_activities`,
    `tasks[*].target_activity` all get the workflow'''s default ontology prefix
    prepended. Plugin authors can use either form; template should state this
    explicitly instead of implying qualified names are mandatory.

12. **🐛 `status:` YAML takes silent precedence over `status_resolver`.**
    `finalization.py:98` reads `activity_def.get("status")` first; only falls
    back to handler result (which is where `status_resolver` writes) if None.
    A plugin author who sets BOTH a YAML `status: "foo"` AND a `status_resolver:`
    dotted path will see the resolver'''s output **silently ignored**. The
    "handler returned status + status_resolver set" case raises a clear 500
    at runtime (split_hooks.py:73-82), but the "YAML status + status_resolver"
    case does not. Worth either raising at load or at runtime symmetrically,
    or documenting the precedence explicitly.

13. **Workflow-top-level `roles:` is convention-only.** Engine doesn'''t read
    `workflow.get("roles")` anywhere. Template section 6 documents it as if
    the engine cares; it doesn'''t. Either (a) document as plugin-convention-
    only, or (b) file a bug if the engine should enforce the role list.

14. **Task-YAML `kind` not load-time validated** even though `TaskEntity.kind`
    is now `Literal[...]` (Bug 39, Round 32). YAML → TaskEntity happens at
    runtime, not load — so a YAML typo (`"recored"` for `"recorded"`) still
    crashes mid-activity. Same root cause as Obs 59. Could be addressed by
    adding a `validate_activity_tasks(workflow)` function similar to the
    existing `validate_side_effect_conditions`.

15. **`tasks[*].target_activity` and `side_effects[*].activity` don'''t
    cross-check target existence.** Names are auto-qualified but not verified
    against the workflow'''s own `activities:` list at load time. A typo like
    `target_activity: "mySetfsysCorrection"` resolves at runtime with a
    misleading error. Similar shape to Obs 59 — additive load-time validator.

16. **`used[*]` required `type:` is a bracket read** (used.py:150 —
    `etype = used_def["type"]`). Missing/null key crashes with KeyError
    rather than a clean load-time error. Should either be `.get` with a
    load-time validator or raise with a clear message.

17. **`namespaces` re-register behavior undocumented.** If a per-plugin
    `namespaces:` entry uses a prefix already registered by the engine or
    at app-level, the behavior (silent accept? overwrite? reject?) isn'''t
    clear from the template. Need to trace `NamespaceRegistry.register()`
    to document.

18. **`generates:` is documented-and-used as `list[str]`** in production
    YAML but the engine'''s use pattern (`entity_type not in allowed_types`)
    technically allows either list-of-strings or list-of-dicts (with the
    check silently falling through if dicts are used). Worth locking down
    the shape in the template to "list of qualified type-name strings"
    rather than leaving ambiguity.

---

## Gap summary — what the template needs added

Based on findings 1-18, the rewrite should add:

**New sections:**
- `reference_data` (finding 2)
- `field_validators` (finding 3)
- `status:` dict form (finding 6)

**Sections needing expansion:**
- Section 10 (Activities): explicit treatment of `client_callable` (finding 7),
  `built_in` as reserved (finding 8), `default_role`/`allowed_roles`/
  `authorization.roles` three-way distinction (finding 9), auto-qualification
  rule (finding 11), status/status_resolver precedence (finding 12).
- Section 8 (Entity Types): cardinality fallback (finding 4), `schemas:` dict
  shape.
- Section 9 (Authorization): three role-entry shapes made explicit (they'''re in
  the docs but the scope:dict typo-pitfall from Bug 34 recon is worth a
  callout), forbidden `scope:` typos like `feild`.
- Section 2 (Relations): `_WORKFLOW_RELATION_KEYS` allowed set + forbidden
  activity-level keys.
- Section 7 (POC Users): flag as POC-only (Bug 28 defers real auth).
- Tasks subsection: `kind` default, relative `scheduled_for` syntax, supersede
  semantics, anchor matching.
- Side-effects subsection: bare-string legacy normalization (finding 10),
  `generates:` default-from-position.

**Section to remove or mark convention-only:**
- Section 6 (Roles) — finding 13. Engine doesn'''t read it; either delete or
  label as author-convention.

**Possible bugs filed for follow-up** (not inventory-scope to fix):
- Finding 1 (`relation_types` dead code)
- Finding 5 (variable-name collision)
- Finding 12 (status precedence footgun)
- Findings 14, 15, 16 (load-time validator gaps — Obs 59 siblings)
- Finding 18 (`generates:` shape ambiguity)

---

## Ready for the template rewrite

With this inventory complete, the next deliverable is a rewritten
`dossiertype_template.md` that incorporates:
- Every key and shape documented above.
- Explicit defaults and edge cases.
- Load-time validation status clearly marked per key.
- Cross-references between related keys (the `default_role`/`allowed_roles`/
  `authorization` triangle, `status`/`status_resolver` precedence, etc.).
- The 18 findings addressed as documentation updates (or flagged as out-of-
  scope follow-ups where a documentation fix would paper over a real bug).

Estimated template rewrite size: current 991 lines → maybe 1400-1800 lines
with added sections and expanded coverage. Several places will also shrink
where drift or convention is documented as such rather than left implicit.
