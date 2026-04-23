# Dossier Engine — File Tree

A role-per-file guide to the engine. Each entry describes what the
file does, what it exports, and how it fits with its neighbours.
Produced by reading each file directly.

For the phase-by-phase execution flow, see
[pipeline_architecture.md](pipeline_architecture.md). For plugin
authoring, see [plugin_guidebook.md](plugin_guidebook.md). For the
workflow YAML contract, see [../dossiertype_template.md](../dossiertype_template.md).

---

## Top-level layout

```
dossier_engine/
├── __init__.py                — public re-export: create_app
├── app.py                     — FastAPI app factory, startup wiring
├── entities.py                — engine-provided entity models
├── file_refs.py               — FileId type + download_url auto-injection
├── lineage.py                 — PROV graph traversal (cross-type entity lookup)
├── migrations.py              — data migration framework
├── archive/                   — PDF/A-3 archive generation
├── auth/                      — authentication middleware
├── db/                        — Postgres rows, repository, session, graph loader
├── engine/                    — activity execution pipeline
├── observability/             — audit logging + Sentry integration
├── plugin/                    — Plugin dataclass + validators + registries
├── prov/                      — PROV vocabulary (IRIs, JSON-LD, namespaces)
├── routes/                    — HTTP API surface
├── search/                    — Elasticsearch integration
└── worker/                    — task worker process (python -m dossier_engine.worker)
```

---

## Top-level files

### `__init__.py`

Re-exports `create_app` so callers can write
`from dossier_engine import create_app`.

### `app.py` (386 lines)

FastAPI app factory. The `create_app(config_path)` function is the
single-entry startup path for the web tier:

1. Loads `config.yaml` and constructs the `PluginRegistry` (via
   `load_config_and_registry`, also exposed for the worker's startup
   path).
2. Runs Alembic migrations via
   `db.alembic._run_alembic_migrations` (fail-fast — any migration
   error aborts startup rather than masking a partially-migrated
   schema).
3. Initializes the DB async engine and session factory
   (`db.init_db`).
4. Configures Sentry for web-tier error reporting
   (`observability.sentry.init_sentry_fastapi`).
5. Configures audit-log routing
   (`observability.audit.configure_audit_logging`).
6. Registers all HTTP routes via `routes.register_routes`, threading
   the plugin registry, auth dependency, and global access rules
   through.
7. Wires per-plugin search-route factories so each plugin can add
   workflow-specific search endpoints.

Re-exports `SYSTEM_USER` (from `auth`) and
`_run_alembic_migrations` (from `db.alembic`) so code importing from
`dossier_engine.app` keeps working regardless of where those symbols
are actually defined.

Intentionally one file — startup order is the value the file
documents, and splitting it would fragment that.

### `entities.py` (199 lines)

Engine-provided entity Pydantic models that every workflow inherits
without declaring:

- `DossierAccess` / `DossierAccessEntry` — the access-control entity
  (`oe:dossier_access`) that governs who can view what in a dossier.
  `DossierAccessEntry.activity_view` is typed as
  `Literal["all", "own"] | list[str] | dict`; see the field's
  inline comment and `routes/_helpers/activity_visibility.py` for
  read-path semantics.
- `TaskEntity` — content model for `system:task` entities. Uses
  `Literal[...]` on `kind` and `status` so invalid YAML fails at
  Pydantic-construction time.
- `SystemNote` — attached to systemAction activities; free-form
  note text plus optional structured fields.
- `SYSTEM_ACTION_DEF` — the built-in activity definition injected
  into every plugin's workflow by the engine.

### `file_refs.py` (184 lines)

The `FileId` type (a `str` subclass) plus the response-walking logic
that auto-injects `<field>_download_url` sibling keys into GET
responses.

Naming rule: a field named `file_id` gets a `file_download_url`
sibling; a field named `brief` gets `brief_download_url`. The
injection walks entity dicts recursively so nested shapes
(`list[FileId]`, `dict[str, FileId]`) also get their URLs resolved.
Signed URLs are minted against the file_service using the signing
key from config.

### `lineage.py` (226 lines)

Activity-graph traversal for "find a related entity of a different
type." Unlike a pure derivation walk (which follows `derivedFrom`
and `used` edges between versions of the same logical entity), this
walker inspects each activity's full signature — both the entities
it used and the entities it co-generated, plus the activity it was
informed by — so it can hop sideways across entity types.

Canonical use case: anchoring a scheduled task to an entity that the
triggering activity didn't touch directly. For example,
`tekenBeslissing` uses a `beslissing` but not the `aanvraag` the
beslissing was about; the handler afterwards still needs the
aanvraag's `entity_id` to anchor a `trekAanvraagIn` deadline task.

### `migrations.py` (342 lines)

Data-migration framework. Applies content transforms to existing
entities, one dossier at a time, through the engine's normal
activity pipeline. Every migration produces a full PROV audit trail:
a `systemAction` activity per dossier with the old entity version in
`used` and the transformed version in `generated`, plus a
`system:note` recording the migration UUID and message.

Plugins declare a `MIGRATIONS` list and call
`run_migrations(plugin, registry)`. Each migration has a UUID and is
idempotent — re-running is a no-op on dossiers that already have its
note.

---

## `archive/` — PDF/A-3 archive generation

```
archive/
├── __init__.py        — re-exports generate_archive, render_timeline_svg
├── orchestrator.py    — the async generate_archive function
├── pdf.py             — ArchivePDF class (FPDF subclass)
├── svg_timeline.py    — static SVG timeline renderer
└── fonts.py           — DejaVu font path resolution
```

`generate_archive(session, dossier_id, dossier, registry, prov_json,
file_storage_root=None)` is the single public entry. Called from
`routes/prov.py` for the `/dossiers/{id}/archive` endpoint. Returns
PDF bytes.

The archive contains:

- Cover page with dossier metadata and actors list.
- Static SVG provenance timeline (no browser, no D3, no JavaScript).
- One section per entity type with version history.
- Embedded attachments — raw PROV-JSON for machine readability, and
  optionally the bijlagen file bytes if `file_storage_root` is set.

### `archive/__init__.py`

Docstring explaining the archive's role; re-exports
`generate_archive` and `render_timeline_svg`.

### `archive/orchestrator.py` (417 lines)

The async `generate_archive` function. Loads all dossier rows from
the DB (activities, entities, associations, used, agents), computes
per-activity timeline metadata, calls `render_timeline_svg` to
produce the timeline image, then drives `ArchivePDF` through its
cover/timeline/entities/attachments sections page by page.

### `archive/pdf.py` (49 lines)

`ArchivePDF` — a thin FPDF subclass with a workflow-aware header,
page-number footer, and DejaVu font setup (regular, bold, italic,
mono). Fonts are resolved through `archive.fonts.find_font`.

### `archive/svg_timeline.py` (197 lines)

`render_timeline_svg` — pure server-side Python SVG generation.
Takes activities, entities_by_type, agents, used_map, and derivation
edges; returns an SVG string. Used for the archive's timeline image
and available to any other consumer wanting the same static
rendering without a browser.

Also defines the color palette (`COL_BG`, `COL_ACTIVITY`,
`COL_ENTITY`, etc.), `_hex_to_rgb`, and `_esc` (XML escape).

### `archive/fonts.py` (152 lines)

DejaVu font discovery. `find_font("regular" | "bold" | "italic" |
"mono")` returns a filesystem path. Looks in `DOSSIER_FONT_DIR` if
set, otherwise searches standard distro locations. Raises a clear
error if fonts aren't installed — the archive needs Unicode glyph
coverage.

---

## `auth/` — authentication

```
auth/
└── __init__.py        — POC middleware + SYSTEM_USER + User dataclass
```

POC auth: looks up the `X-POC-User` header against the `poc_users`
list declared in each plugin's `workflow.yaml`. The middleware
builds a username → User map at startup.

Exports:

- `User` dataclass — what plugins see as `context.user`. Fields:
  `id`, `type`, `name`, `roles`, `properties`, optional `uri`.
- `SYSTEM_USER` — canonical
  `User(id="system", type="systeem", name="Systeem",
  roles=["systeem"], properties={})`. Used by worker and side-effect
  paths that act on behalf of the system rather than a logged-in
  user.
- `POCAuthMiddleware` — the FastAPI middleware class.
- `get_user` — the `Depends(...)` factory.

Production replacement: swap `POCAuthMiddleware` for a JWT/OAuth
middleware that produces the same `User` shape.

---

## `db/` — Postgres persistence

```
db/
├── __init__.py        — re-exports init_db, Repository, etc.
├── session.py         — async engine, session factory, deadlock-retry wrapper
├── alembic.py         — _run_alembic_migrations (subprocess runner)
├── graph_loader.py    — load all PROV rows for one dossier in one call
└── models/            — Row dataclasses + Repository
    ├── __init__.py    — re-exports Base + 8 Row classes + Repository
    ├── rows.py        — 8 SQLAlchemy row classes
    └── repository.py  — Repository class (session-bound data access)
```

Postgres 16+ required. The code uses native `UUID` columns (via
`sqlalchemy.dialects.postgresql.UUID`) and `JSONB` for `content`
fields. Every table is append-only — no UPDATEs, no DELETEs. Status
and other mutable properties produce new rows.

### `db/__init__.py`

Re-exports the public surface: `init_db`, `create_tables`,
`get_session_factory`, `run_with_deadlock_retry` (from
`session.py`), and `Repository` (from `models/`).

### `db/session.py` (147 lines)

Async engine and session factory management.

- `init_db(database_url, *, pool_size=..., max_overflow=...,
  pool_recycle=..., pool_timeout=...)` — creates the global engine
  and session factory.
- `create_tables()` — calls `Base.metadata.create_all`. Used by
  tests; production uses Alembic.
- `get_session_factory()` — returns the `async_sessionmaker`.
- `run_with_deadlock_retry(work, max_retries=3)` — runs an async
  work function inside a transaction, catches Postgres deadlock
  errors, retries with exponential backoff plus jitter. All HTTP
  write paths go through this.

### `db/alembic.py` (92 lines)

Migration runner. One function: `_run_alembic_migrations(db_url)`.
Runs `alembic upgrade head` in a subprocess (Alembic's `env.py`
calls `asyncio.run()` which can't nest inside uvicorn's loop).
Fail-fast: a missing `alembic.ini` or a non-zero exit code raises
`RuntimeError`, aborting startup. Re-exported from `app.py` for
callers that imported it from there historically.

### `db/graph_loader.py` (160 lines)

Bulk row loader. `load_dossier_graph_rows(session, dossier_id)`
fetches every row needed to reason about a dossier's provenance
graph — activities, entities, associations, used — plus agents, with
pre-built per-activity indexes. Returns a `DossierGraphRows`
dataclass. Used by the PROV-JSON builder, the columns graph, the
timeline graph, and the archive orchestrator.

### `db/models/__init__.py`

Re-exports `Base`, the 8 row classes (`DossierRow`, `ActivityRow`,
`AssociationRow`, `EntityRow`, `UsedRow`, `RelationRow`, `AgentRow`,
`DomainRelationRow`), the type aliases `UUID_DB` and `JSON_DB`, and
the `Repository` class.

### `db/models/rows.py` (227 lines)

SQLAlchemy row classes. Each is a thin dataclass with a table name,
columns, and relationships. `Base` is the shared `declarative_base`.
`UUID_DB` and `JSON_DB` are type shims over Postgres-native `UUID`
and `JSONB`.

Tables:

- `dossiers` (`DossierRow`) — one per logical dossier. Cached
  `computed_status`, `eligible_activities`, workflow name.
- `activities` (`ActivityRow`) — one per activity instance. Type,
  dossier link, role, `informed_by`, computed_status.
- `associations` (`AssociationRow`) — `wasAssociatedWith` edges from
  an activity to an agent (user), with the PROV role.
- `entities` (`EntityRow`) — one per entity version. Logical
  identity in `entity_id`, version identity in `id`. Content as
  JSONB. Links to the generating activity and (optionally) the
  version this one was derived from.
- `used` (`UsedRow`) — `used` edges from an activity to an entity
  version.
- `relations` (`RelationRow`) — activity-level relations
  (process-control: `oe:neemtAkteVan`).
- `agents` (`AgentRow`) — one per unique agent id seen.
- `domain_relations` (`DomainRelationRow`) — entity-to-entity/URI
  semantic edges (`oe:betreft`, `oe:gerelateerd_aan`) with the
  provenance activity.

### `db/models/repository.py` (555 lines)

The `Repository` class — a single session-bound gateway for all data
access. Kept in one file because methods cross-reference each other
heavily: activity operations call entity operations, entity
operations touch domain relations, all of them use agent ensure.
Splitting by table would fragment one logical unit.

Session-scoped caches inside `Repository` reduce redundant reads
within a single HTTP request:

- `_ensured_agents` — short-circuits `ensure_agent`.
- `_activities_cache` — populated on first
  `get_activities_for_dossier`; invalidated in place when a new
  activity is created in the same session.
- `_dossier_cache` — avoids repeated
  `SELECT FROM dossiers WHERE id = ?`.

Method groups (all async): dossier operations, activity operations,
entity operations, used/generated linking, domain relations, agent
ensure.

---

## `engine/` — activity execution pipeline

```
engine/
├── __init__.py        — execute_activity() orchestrator
├── context.py         — ActivityContext, HandlerResult, _PendingEntity, TaskResult
├── state.py           — ActivityState (phase-to-phase mutable carrier)
├── lookups.py         — lookup_singleton, resolve_from_trigger/prefetched
├── refs.py            — EntityRef parsing (prefix:type/eid@vid)
├── response.py        — build_replay_response (for idempotent replays)
├── errors.py          — ActivityError, CardinalityError
├── scheduling.py      — parse_scheduled_for (relative and ISO)
└── pipeline/          — the execution phases (below)
```

The orchestrator in `__init__.py` is the single entry point
`execute_activity(...)`. It constructs an `ActivityState` from
request arguments, runs the pipeline phases in order inside one DB
transaction, and returns a response dict.

### `engine/__init__.py` (259 lines)

Defines `execute_activity(repo, plugin, act_def, dossier_id,
activity_id, user, ...)`. Imports every phase function and calls
them in the fixed order documented in `pipeline_architecture.md`.

Re-exports everything plugins and route code need:

- From `context.py`: `ActivityContext`, `HandlerResult`, `TaskResult`.
- From `errors.py`: `ActivityError`, `CardinalityError`.
- From `refs.py`: `EntityRef`.
- From `state.py`: `Caller` enum.
- From `pipeline/_helpers/`: `compute_eligible_activities`,
  `filter_by_user_auth`, `derive_allowed_activities`, `derive_status`,
  `enforce_used_generated_disjoint`.

### `engine/context.py` (312 lines)

Handler-facing types — what plugin code sees at call time.

- `ActivityContext` — passed to every handler and validator. Wraps
  the repo, dossier id, resolved `used` entities, pending generated
  entities, the plugin reference, the user, and constants. Provides
  typed accessors: `get_typed(entity_type)`,
  `get_singleton_typed(...)`, `get_used_entity(...)`,
  `get_all_entities_typed(...)`. Handlers read entities through
  these — never directly from the repo.
- `_PendingEntity` — duck-typed stand-in for an `EntityRow` that
  hasn't been persisted yet. Lets handlers in the same activity
  read entities the current activity is generating (via
  `context.get_typed(...)`) before those rows exist in the DB.
  Quacks like `EntityRow` for every column handlers access.
- `HandlerResult` — what handlers return. Fields: `content` (dict
  for the primary generated entity), `status` (optional status
  override), `generated` (list of `(type, content)` tuples for
  additional entities), `tasks` (list of task dicts the engine
  should schedule).
- `TaskResult` — what cross-dossier task functions return. Fields:
  `target_dossier_id`, `content` (dict for the resulting activity).

### `engine/state.py` (275 lines)

`ActivityState` — the mutable dataclass threaded through every
pipeline phase. ~37 fields split into inputs (set by the
orchestrator from request arguments) and phase outputs (populated as
phases run). Each field carries a comment documenting which phase
sets it and which downstream phases read it.

Also defines:

- `Caller` enum — `CLIENT` / `SYSTEM` / `WORKER`. Used by `used.py`
  to gate the auto-resolve path.
- `UsedRef`, `ValidatedRelation`, `DomainRelationEntry` — frozen
  dataclasses for phase outputs. Typed containers catch shape bugs
  at development time.

### `engine/lookups.py` (122 lines)

Three functions for finding an entity from a partial specification.

- `lookup_singleton(plugin, repo, dossier_id, entity_type)` — the
  only sanctioned way for engine code to fetch a singleton entity.
  Enforces the cardinality invariant: checks the plugin's
  declaration before calling the repo, raises `CardinalityError` if
  the type is declared `multiple`. Direct calls to
  `repo.get_singleton_entity` bypass this check.
- `resolve_from_trigger(repo, dossier_id, trigger_activity_id,
  entity_type)` — given an activity that triggered the current one
  (a side effect's parent or a scheduled task's informing activity),
  find an entity of the requested type by inspecting what the
  trigger generated and used.
- `resolve_from_prefetched(repo, dossier_id,
  trigger_generated_rows, trigger_used_rows, entity_type)` — same
  logic as above but takes the trigger's generated/used lists as
  parameters, avoiding redundant queries when resolving multiple
  types from the same trigger.

### `engine/refs.py` (175 lines)

`EntityRef` — the single source of truth for the canonical ref
string format `prefix:type/entity_id@version_id`. The dataclass has
`type`, `entity_id`, `version_id` fields; `__str__` renders the
canonical form; `EntityRef.parse(s)` returns an `EntityRef` or
`None` (external URI).

No f-string concatenation of refs should live outside this file —
all callers go through `EntityRef(...)` / `EntityRef.parse(...)`.

### `engine/response.py` (63 lines)

`build_replay_response(plugin, repo, dossier_id, activity_row,
user)` — synthesizes the response for an idempotent replay. When
the engine sees a `PUT /activities/{activity_id}` whose
`activity_id` already exists in the DB, it doesn't re-execute; it
reconstructs the response from the activity row plus a fresh
dossier-state computation. Returned shape is a subset of what a
fresh `execute_activity` produces — `used` and `generated` lists are
empty because they aren't replayed.

### `engine/errors.py` (45 lines)

Two exception classes:

- `ActivityError(status_code, detail, payload=None)` — the client
  should know. `detail` is a human message; `payload` is an optional
  dict merged into the JSON response body alongside it. The route
  layer turns `ActivityError` into `HTTPException` via
  `routes/_helpers/errors.py::activity_error_to_http`.
- `CardinalityError` — a bug, not a client error. Raised when engine
  or handler code calls a singleton helper on a type the plugin
  declared `multiple`.

### `engine/scheduling.py` (95 lines)

`parse_scheduled_for(value: str, now: datetime) -> datetime` —
parses the two accepted forms of the `scheduled_for` YAML field on
task declarations:

- Relative offset: `+20d`, `+2h`, `+45m`, `+3w`. Resolved against
  `now`.
- Absolute ISO 8601: `2026-05-01T12:00:00Z` or
  `2026-05-01T12:00:00+00:00`. Parsed via `datetime.fromisoformat`
  after a Z-suffix tolerance pass.

Called from the task-scheduling pipeline phase
(`pipeline/tasks.py`) and from the worker's due-check
(`worker/polling.py`).

### `engine/pipeline/` — the execution phases

```
pipeline/
├── __init__.py              — empty package marker
├── preconditions.py         — idempotency, dossier-promotion, workflow rules
├── authorization.py         — can this user run this activity?
├── used.py                  — resolve used[] refs
├── handlers.py              — invoke the plugin handler
├── split_hooks.py           — status_resolver + task_builders hooks
├── generated.py             — process generated[] into persist-ready dicts
├── validators.py            — dispatch plugin-declared cross-entity validators
├── tasks.py                 — schedule + supersede tasks
├── finalization.py          — status + eligible_activities + cache
├── persistence.py           — DB writes (activity row + entities + links)
├── tombstone.py             — tombstone-shape validation (alternative flow)
├── relations/               — relations phase (sub-package)
├── side_effects/            — side-effects phase (sub-package)
└── _helpers/                — cross-phase helpers
```

For the phase-by-phase execution flow and ordering rationale, see
`pipeline_architecture.md`. The per-file descriptions below cover
what each file contains, not what each phase does conceptually.

### `engine/pipeline/preconditions.py` (173 lines)

Pre-execution gates. Three functions:

- `check_idempotency(state)` — detects whether this `activity_id`
  already exists; sets `state.idempotent_replay` accordingly.
- `ensure_dossier(state)` — loads the dossier row, or creates it
  if this is a `can_create_dossier` activity running first.
- `validate_workflow_rules(state)` — checks `requirements`
  (prior activities, dossier status, prior entity types),
  `forbidden` rules, and required-entity-type constraints.

### `engine/pipeline/authorization.py` (231 lines)

`authorize_activity(state)` — walks the activity's `authorization`
block (from YAML) and tries each role entry in turn. Three
role-matching patterns: direct string match, scoped match
(`<role>:<resolved-value>`), entity-derived match (the entity field
value IS the role string). Returns normally on first match; raises
`ActivityError(403, ...)` if no entry matches.

Also hosts `_resolve_field` (shared with side-effect condition
evaluation): walks a dotted path through an entity's content.

### `engine/pipeline/used.py` (200 lines)

`resolve_used(state)` — the two-pass resolution of the `used[]`
block.

1. `_resolve_explicit(state)` — turns every client-supplied ref into
   a row. External URIs are persisted on the fly (so the PROV graph
   stays complete) and recorded with `external: True`. Local refs
   are validated (exists, belongs to this dossier; cross-dossier
   refs are rejected).
2. `_auto_resolve_for_system_caller(state)` — runs **only** when
   `state.caller == Caller.SYSTEM`. Fills `auto_resolve: "latest"`
   slots via trigger scope → anchor entity → dossier-wide singleton.
   Client callers don't hit this path — they must supply every ref
   explicitly.

### `engine/pipeline/handlers.py` (151 lines)

`run_handler(state)` — calls the plugin handler registered for the
activity, if any, with an `ActivityContext`. The handler can:

- Return content for the activity's primary generated entity.
- Return a status override via `HandlerResult.status`.
- Append additional entities via `HandlerResult.generated`.
- Append tasks via `HandlerResult.tasks`.

Also resolves handler-generated entity identity through
`_helpers/identity.py::resolve_handler_generated_identity`.

### `engine/pipeline/split_hooks.py` (117 lines)

`run_split_hooks(state)` — the status_resolver and task_builders
alternative-to-handler hooks. Activities that declare a
`status_resolver` or `task_builders` in YAML get those functions
called here, producing status and tasks independently of the main
handler.

Enforces the "exactly one source per concern" rule: if a
`status_resolver` is declared AND `HandlerResult.status` is also
set, raises a clear 500. Same for `task_builders` vs
`HandlerResult.tasks`.

### `engine/pipeline/generated.py` (374 lines)

`process_generated(state)` — turns the `generated[]` block from the
request (plus any handler-appended entries) into normalized dicts
ready for persistence. Five sub-steps: (1) merge YAML-derived entries
with handler-appended ones, (2) resolve derived-from chains, (3)
apply schema version discipline, (4) validate content against
Pydantic models, (5) stamp generated-by metadata.

Exports `_resolve_schema_version` for the side-effects path (which
runs the same version resolution without going through the full
phase).

### `engine/pipeline/validators.py` (64 lines)

`run_custom_validators(state)` — dispatches plugin-declared
validators named in the activity's YAML `validators:` block. Each
validator receives an `ActivityContext` with resolved used entities
and pending generated entities, and returns either `None` (pass) or
an error message / tuple / `ActivityError`.

### `engine/pipeline/tasks.py` (370 lines)

`process_tasks(state)` and `cancel_matching_tasks(state)`.

`process_tasks` iterates YAML-declared tasks plus handler-appended
tasks from `HandlerResult.tasks`, constructs `system:task` entity
content for each (resolving `scheduled_for` via
`engine.scheduling.parse_scheduled_for`, computing identity,
supersede decisions), and adds them to
`state.handler_tasks_to_persist`.

`cancel_matching_tasks` walks `cancel_if_activities` on existing
scheduled tasks and creates a cancelled version of any task whose
type matches the current activity.

### `engine/pipeline/finalization.py` (232 lines)

`finalize_dossier(state)` — the last phase. Determines the
activity's status contribution (from YAML literal, handler override,
or entity-field mapping), resolves the dossier's current status,
computes the eligible-activity list filtered for the calling user,
caches both on the dossier row, and runs the plugin's
`post_activity_hook` (search index updates). Hook exceptions are
logged but don't roll back — finalization is advisory.

### `engine/pipeline/persistence.py` (215 lines)

Three functions called in order from the orchestrator:

- `create_activity_row(state)` — writes the `ActivityRow` and the
  `wasAssociatedWith` association linking the activity to the
  calling user. Runs after validation but before the handler so
  the row exists in the session when entities are persisted.
- `persist_outputs(state)` — writes generated entities, used links,
  activity-relation rows, domain-relation rows, and domain-relation
  removals. One batch of writes.
- `persist_handler_tasks(state)` — writes the task entities produced
  by `process_tasks`.

### `engine/pipeline/tombstone.py` (145 lines)

`validate_tombstone_shape(state)` — strict request-shape validator
for the built-in `tombstone` activity. Tombstone bypasses the normal
disjoint-used/generated rule in `_helpers/invariants.py` because
it's supposed to redact content in place via a
revision-with-null-content pattern. Validates that the request has
exactly the tombstone shape (one used ref, one generated ref with
matching identity) and nothing else.

### `engine/pipeline/relations/`

```
relations/
├── __init__.py        — re-exports process_relations, _validate_ref_types
├── declarations.py    — workflow/activity YAML introspection + _validate_ref_types
├── process.py         — process_relations entry + parse helpers
└── dispatch.py        — per-kind handlers + validator dispatch
```

#### `relations/__init__.py`

Re-exports the two public symbols: `process_relations` (the phase)
and `_validate_ref_types` (used by unit tests).

#### `relations/declarations.py` (171 lines)

YAML-introspection helpers — read-only queries over the workflow
dict:

- `_relation_declarations(activity_def)` — parses the activity's
  `relations:` block into a `{type: declaration}` dict.
- `allowed_relation_types_for_activity(plugin, activity_def)` —
  union of workflow-level and activity-level declared types.
- `_allowed_operations(activity_def, rel_type)` — set of
  `{add, remove}`.
- `_relation_kind(plugin, rel_type)` — `domain` or `process_control`.
- `_relation_type_declaration(plugin, rel_type)` — the full
  workflow-level declaration for a type.
- `_validate_ref_types(plugin, rel_type, from_ref, to_ref)` — the
  `from_types`/`to_types` gate for domain relations. Classifies each
  endpoint via `prov.iris.classify_ref` and checks against the
  declared constraint.

#### `relations/process.py` (209 lines)

The pipeline-phase driver.

- `process_relations(state)` — computes the allowed-types set
  (permission gate), then runs two parsing passes (add then remove)
  and finally dispatches validators.
- `_parse_relations(state, allowed)` — iterates the `relations`
  block, gates each entry against the allowed-types set, delegates
  to the kind-specific handler in `dispatch.py`.
- `_parse_remove_relations(state, allowed)` — same shape for the
  `remove_relations` block (domain-kind only).

#### `relations/dispatch.py` (240 lines)

Per-kind persistence helpers and validator firing.

- `_handle_domain_add(state, rel_item, rel_type, from_ref)` —
  persists a domain-kind add by IRI-expanding refs via
  `prov.iris.expand_ref` and appending to
  `state.validated_domain_relations`.
- `_handle_process_control(state, rel_item, rel_type)` — persists a
  process-control add by appending to `state.validated_relations`.
- `_resolve_validator(plugin, rel_type, op, act_level_entry)` —
  looks up the validator callable for a (type, operation) pair.
- `_dispatch_validators(state, allowed)` — iterates activity-level
  declared types, finds each validator, calls it with the collected
  relations set.

### `engine/pipeline/side_effects/`

```
side_effects/
├── __init__.py        — re-exports execute_side_effects
├── execute.py         — the recursive driver
└── helpers.py         — _condition_met, _auto_resolve_used, _persist_se_generated
```

#### `side_effects/__init__.py`

Docstring explaining side-effect semantics (recursive, system
caller, no client-facing blocks, conditions allowed, schema
versioning works the same, depth limit). Re-exports
`execute_side_effects`.

#### `side_effects/execute.py` (233 lines)

`execute_side_effects(state, side_effects_list, depth)` — the
recursive driver. For each entry in the list, checks the optional
condition via `_condition_met`, looks up the target activity
definition, and dispatches `_execute_one_side_effect` — which
auto-resolves used entities, calls the system handler, persists
the outputs, and recurses into the target activity's own side
effects.

#### `side_effects/helpers.py` (224 lines)

Three helpers scoped to one side-effect invocation.

- `_condition_met(side_effect_dict, state)` — evaluates the optional
  `condition:` dict (`{entity_type, field, value}`) or the
  dotted-path `condition_fn:`. Returns bool.
- `_auto_resolve_used(plugin, repo, dossier_id, used_defs,
  trigger_generated_rows, trigger_used_rows)` — fills
  `auto_resolve: "latest"` slots using trigger scope → singleton
  lookup.
- `_persist_se_generated(plugin, repo, dossier_id, se_activity_row,
  se_result, version_overrides)` — persists the entities the
  side-effect handler returned. Resolves identity (explicit fields,
  derivedFrom, or new-UUID), stamps the schema version via
  `_resolve_schema_version`.

### `engine/pipeline/_helpers/`

```
_helpers/
├── __init__.py        — empty package marker
├── eligibility.py     — compute_eligible_activities, filter_by_user_auth,
│                        derive_allowed_activities
├── status.py          — derive_status
├── invariants.py      — enforce_used_generated_disjoint
└── identity.py        — resolve_handler_generated_identity
```

Cross-phase helpers called by multiple phases. The leading
underscore on the package name signals "engine-private — not part of
any plugin contract."

#### `_helpers/eligibility.py` (102 lines)

Three functions:

- `compute_eligible_activities(plugin, repo, dossier_id)` — the full
  list of activities the workflow says are reachable given the
  current dossier state (status and prior activities), ignoring
  authorization.
- `filter_by_user_auth(plugin, activities, user, dossier_context)`
  — drops activities the user isn't authorized for.
- `derive_allowed_activities(plugin, repo, dossier_id, user)` —
  convenience wrapper that does both in order.

Called by finalization (to cache the list on the dossier) and by the
response builder (to reconstruct it for idempotent replays).

#### `_helpers/status.py` (35 lines)

`derive_status(activities)` — folds the list of activities'
computed statuses into the dossier's current status. Rule is "last
non-empty wins": walk activities newest-first, return the first
non-empty `computed_status`. Called by authorization (to evaluate
status requirements), finalization (to cache), the response builder,
and `routes/dossiers.py`.

#### `_helpers/invariants.py` (116 lines)

`enforce_used_generated_disjoint(state)` — checks that no entity
appears in both `used` and `generated`. Called as an explicit phase
between `resolve_used` and `process_generated` (catches
user-submitted overlaps) and again as a defensive check in the
tombstone flow.

#### `_helpers/identity.py` (110 lines)

`resolve_handler_generated_identity(plugin, repo, dossier_id,
entity_type, explicit_entity_id, explicit_derived_from,
prefetched_rows)` — resolves the `(entity_id, derived_from)` pair
for an entity the handler is generating, applying precedence:
explicit > derivedFrom-lookup > new UUID. Used by `handlers.py`
(main pipeline) and `side_effects/helpers.py` (side-effect
persistence).

---

## `observability/` — logging and error reporting

```
observability/
├── __init__.py        — package docstring
├── audit.py           — structured audit-log emission
└── sentry.py          — Sentry setup + error capture helpers
```

Two unrelated cross-cutting tools grouped by concern.

### `observability/audit.py` (298 lines)

Structured audit logging via the Python `logging` module.

- `configure_audit_logging(config)` — wires a dedicated logger
  (`dossier.audit`) with a JSON formatter, optional file output,
  log-level policy. Called from `app.py` at startup.
- `emit_dossier_audit(user, dossier_id, activity_type, activity_id,
  status, error=None, **extra)` — the canonical audit-emission
  function. Called from `routes/activities/run.py` (post-commit
  success) and the denial path. Best-effort — never raises.
- Helpers for building the structured log record.

### `observability/sentry.py` (319 lines)

Sentry integration. Three sections:

- Setup: `init_sentry_fastapi(config)` for the web app,
  `init_sentry_worker(config)` for the worker process. Both read
  the DSN from config, set `environment`, wire up FastAPI or
  asyncio integrations as appropriate.
- Capture helpers: `capture_task_retry`, `capture_task_dead_letter`,
  `capture_worker_loop_crash` — used by the worker for structured
  error reporting at specific lifecycle points.
- Context-tagging helpers that attach dossier_id, activity_type, and
  workflow name to every event.

---

## `plugin/` — plugin interface

```
plugin/
├── __init__.py        — re-exports public names
├── model.py           — Plugin + PluginRegistry + FieldValidator
├── registries.py      — build_*_registries + _import_dotted
├── validators.py      — 5 load-time validators
└── normalize.py       — _normalize_plugin_activity_names
```

The plugin interface is the contract between the engine and workflow
plugins. Every plugin's `create_plugin()` constructs a `Plugin`; the
app's `PluginRegistry` holds them all.

### `plugin/__init__.py`

Re-exports the public surface: the three dataclasses
(`FieldValidator`, `Plugin`, `PluginRegistry`), the four
registry-builders and two dotted-path helpers, the five load-time
validators, and `_normalize_plugin_activity_names`. This is the
single import source for plugin-interface consumers.

### `plugin/model.py` (285 lines)

The three dataclasses.

- `FieldValidator` — a small dataclass for one named field
  validator. Fields: `name`, `func` (sync or async callable),
  optional `request_model` and `response_model` Pydantic classes for
  OpenAPI typing.
- `Plugin` — the big one. Fields: `name`, `workflow` dict,
  `constants` (plugin-specific BaseSettings instance),
  `entity_models` and `entity_schemas` maps (from entity_types
  YAML), eight callable registries (`handlers`, `validators`,
  `task_handlers`, `condition_fns`, `relation_validators`,
  `status_resolvers`, `task_builders`, `field_validators`), optional
  `post_activity_hook`, optional `search_route_factory`, optional
  `common_doc_builder`, optional `reference_data`. Methods:
  `find_activity_def(name)`, `is_singleton(entity_type)`, others for
  activity and entity-type lookup.
- `PluginRegistry` — holds `dict[workflow_name, Plugin]`. Methods:
  `register(plugin)` (runs `_normalize_plugin_activity_names`
  automatically), `get(workflow_name)`, `all()`.

### `plugin/registries.py` (377 lines)

Registry construction: turning dotted paths from YAML into concrete
callables.

- `_import_dotted(path)` — resolves a dotted path to a Pydantic
  model class. Raises `ValueError` with a clear message on failure.
- `_import_dotted_callable(path, context="")` — same for any
  callable; the `context` is folded into the error message for YAML
  traceability.
- `build_entity_registries_from_workflow(workflow)` — walks
  `entity_types[*].model` and `entity_types[*].schemas` to build
  `entity_models` and `entity_schemas`.
- `build_callable_registries_from_workflow(workflow)` — walks every
  dotted-path reference in the workflow (handlers, validators, task
  functions, condition functions, relation validators, status
  resolvers, task builders) and builds the eight registry dicts.

### `plugin/validators.py` (476 lines)

Five load-time validators — each catches a category of plugin YAML
contract violation at startup rather than at request time.

- `validate_workflow_version_references(workflow, entity_schemas)`
  — every version string used in an activity's `entities:` block
  must be declared in the corresponding
  `entity_types[type].schemas`.
- `validate_side_effect_condition_fn_registrations(plugin)` — every
  `side_effects[*].condition_fn` dotted path must resolve.
- `validate_side_effect_conditions(workflow)` — every
  `side_effects[*].condition` dict must have exactly the three
  required keys.
- `validate_relation_declarations(workflow)` — the comprehensive
  check of the workflow- and activity-level `relations:` contract
  (kinds, operations, from_types/to_types at the right level,
  allowed-keys sets).
- `validate_relation_validator_registrations(plugin)` — every
  relation-validator name referenced in YAML must resolve in
  `plugin.relation_validators`.

Plus the shared constants: `_VALID_RELATION_KINDS`,
`_WORKFLOW_RELATION_KEYS`, `_ACTIVITY_RELATION_KEYS`,
`_ACTIVITY_RELATION_FORBIDDEN_KEYS`,
`_RELATION_VALIDATOR_DICT_KEYS`.

### `plugin/normalize.py` (99 lines)

`_normalize_plugin_activity_names(plugin)` — post-load normalization.
Qualifies bare activity names (`dienAanvraagIn`) to the workflow's
default prefix (`oe:dienAanvraagIn`), and qualifies cross-references
in `requirements.activities`, `forbidden.activities`,
`side_effects[*].activity`, `tasks[*].cancel_if_activities`,
`tasks[*].target_activity`. Idempotent — running twice is a no-op.

Called automatically from `PluginRegistry.register(plugin)`.

---

## `prov/` — PROV vocabulary

```
prov/
├── __init__.py        — package docstring only
├── iris.py            — IRI generation + qname helpers + expand/classify
├── json_ld.py         — build_prov_graph (PROV-JSON export)
├── namespaces.py      — NamespaceRegistry singleton
└── activity_names.py  — qualify / local_name helpers
```

All four files concern PROV vocabulary handling. `__init__.py` is
documentation only — callers import the submodule they need
(`from dossier_engine.prov.iris import ...`) rather than going
through the package root. Keeps the import graph legible.

### `prov/iris.py` (344 lines)

IRI generation and classification.

- `configure_iri_base(config)` — sets up the global ontology base
  URI from config. Called at startup.
- `prov_prefixes()` — returns the `@context` dict for PROV-JSON
  output (`prov`, `xsd`, `rdf`, `rdfs`, plus all plugin-registered
  prefixes).
- `entity_qname(entity_type, entity_id, version_id)` — builds the
  qualified name form for an entity version.
- `activity_qname(activity_type, activity_id)` — same for
  activities.
- `agent_qname(agent_id)` — same for agents.
- `activity_full_iri(activity_id, dossier_id)` — expands an activity
  ref to an absolute URI (used for cross-dossier `wasInformedBy`
  links).
- `prov_type_value(type_str)` — maps an entity type to its PROV
  `prov:type` value (`prov:Entity` or a type-specific subclass).
- `expand_ref(ref, dossier_id)` — turns a ref string into an
  absolute URI. External URIs pass through; local refs expand to
  `urn:dossier:<dossier-id>/entity/<eid>@<vid>` form.
- `classify_ref(ref)` — returns
  `"external_uri" | "entity" | "dossier"`.

### `prov/json_ld.py` (213 lines)

`build_prov_graph(rows, plugin, dossier_id)` — assembles the
PROV-JSON export from pre-loaded graph rows (produced by
`db.graph_loader.load_dossier_graph_rows`). Emits W3C PROV-JSON with
`@context`, `entity`, `activity`, `agent`, `wasGeneratedBy`, `used`,
`wasDerivedFrom`, `wasAssociatedWith`, `wasAttributedTo`,
`wasInformedBy`.

### `prov/namespaces.py` (181 lines)

`NamespaceRegistry` singleton — the global source of truth for
workflow and namespace prefixes.

- `namespaces()` — accessor for the global instance.
- `set_namespaces(instance)` — called at startup to wire it up.
- Methods on the instance: `register(prefix, uri)`,
  `prefix_for_uri(uri)`, `uri_for_prefix(prefix)`,
  `default_workflow_prefix`.

Built-in prefixes (`prov`, `xsd`, `rdf`, `rdfs`) are always present.
Per-workflow prefixes come from each plugin's YAML `namespaces:`
block plus the auto-registered prefix derived from `config.yaml`'s
`iri_base.ontology`.

### `prov/activity_names.py` (93 lines)

Two helpers for prefix handling on activity names:

- `qualify(name, default_prefix)` — if `name` contains `:`, returns
  it unchanged; otherwise prepends `default_prefix:`.
- `local_name(qualified)` — strips the prefix, returning the local
  part.

Used during plugin load (`normalize.py`) and at a handful of places
in the engine where activity names need to be compared in local
form.

---

## `routes/` — HTTP API

```
routes/
├── __init__.py        — register_routes() orchestrator
├── access.py          — shared access control
├── admin_search.py    — /admin/search/* engine-level endpoints
├── dossiers.py        — GET /dossiers, GET /dossiers/{id}
├── entities.py        — GET /dossiers/{id}/entities/...
├── files.py           — POST signed-upload-url endpoint
├── prov.py            — PROV-JSON + timeline-graph + archive endpoints
├── prov_columns.py    — column-layout graph endpoint
├── reference.py       — /{workflow}/reference, /{workflow}/validate
├── templates/         — Jinja templates for prov + columns visualizations
├── _helpers/          — private route-layer helpers
└── activities/        — activity execution endpoints
```

### `routes/__init__.py` (103 lines)

`register_routes(app, *, registry, get_user, global_access,
global_admin_access, ...)` — the orchestrator that wires up every
route module with its dependencies. Called from `app.py`'s
`create_app`.

Module dispatch: imports each leaf route module and calls its
`register(...)` function with the deps that module needs.

### `routes/access.py` (305 lines)

Shared access control logic used by every read endpoint.

Default-deny access flow:

1. `check_dossier_access(repo, dossier_id, user, global_access)` —
   looks for a matching entry first in `global_access` (from
   `config.yaml`), then in the per-dossier `oe:dossier_access`
   entity. Returns the matched entry or raises 403.
2. `check_audit_access(repo, dossier_id, user, global_audit_access)`
   — separate tier for full-provenance views. Matches against the
   dossier's `audit_access` list plus the global audit roles.
3. `get_visibility_from_entry(access_entry)` — extracts the `view:`
   list and `activity_view` from a matched access entry.

### `routes/admin_search.py` (97 lines)

Engine-level admin endpoints for the common (cross-workflow) search
index. All endpoints require `global_admin_access`:

- `POST /admin/search/common/recreate` — drop and recreate the
  common index. Destructive.
- `POST /admin/search/common/reindex-all` — iterate every dossier
  and rebuild. Slow.

### `routes/dossiers.py` (311 lines)

Two endpoints:

- `GET /dossiers/{id}` — full dossier detail: workflow, status,
  allowed-activity list filtered for the calling user, current
  entity snapshot (one version per logical entity, filtered by
  `view`), visible activity timeline (filtered by `activity_view`),
  audit_access flag.
- `GET /dossiers` — stub for cross-workflow listing. Plugins
  override with their own workflow-specific search endpoints
  registered under `/{workflow}/dossiers`.

### `routes/entities.py` (288 lines)

Three shapes for inspecting persisted entities:

- `GET /dossiers/{id}/entities/{type}` — every version of every
  logical entity of the given type.
- `GET /dossiers/{id}/entities/{type}/{entity_id}` — every version
  of one specific logical entity.
- `GET /dossiers/{id}/entities/{type}/{entity_id}/{version_id}` —
  one specific version.

All responses filtered by the matched access entry's `view` list.
Tombstoned versions appear in the response with `content: null` plus
`tombstonedBy` and `redirectTo`.

### `routes/files.py` (107 lines)

`POST /dossiers/{id}/files/upload-url` — mints a signed upload URL
the client posts file bytes to directly (never through the dossier
API itself). Used for bijlagen uploads in workflows that carry file
attachments.

### `routes/prov.py` (510 lines)

Three endpoints related to PROV export and visualization:

- `GET /dossiers/{id}/prov` — PROV-JSON export (audit-tier access).
- `GET /dossiers/{id}/prov/graph/timeline` — timeline graph,
  filtered by the user's `activity_view`.
- `GET /dossiers/{id}/archive` — PDF/A archive export (delegates to
  `archive.generate_archive`; audit-tier access).

The timeline graph is rendered via a Jinja template
(`templates/prov_timeline.html`) — the Python side builds a JSON
payload of nodes and edges and passes it in.

### `routes/prov_columns.py` (450 lines)

`GET /dossiers/{id}/prov/graph/columns` — the column-layout graph.
Three bands: top (client + scheduled activities), middle (side
effects + systemAction), bottom (entities in per-type rows with
derivation arrows). Computes the layout server-side and hands off to
`templates/prov_columns.html` for rendering.

### `routes/reference.py` (213 lines)

Workflow-scoped utility endpoints:

- `GET /{workflow}/reference/{list_name}` — static reference lists
  served straight from the plugin's `reference_data` dict. Public
  (no auth).
- `POST /{workflow}/validate/{validator_name}` — authenticated
  field-validator endpoints. Each validator registered in
  `plugin.field_validators` gets its own URL segment.

### `routes/templates/`

Jinja HTML templates used by `prov.py` and `prov_columns.py`:

- `prov_timeline.html` — the interactive timeline visualization.
- `prov_columns.html` — the column-layout graph.

Both include inline D3/Cytoscape.js and CSS; the Python side passes
in the node/edge JSON via template context.

### `routes/_helpers/` — private route-layer utilities

```
_helpers/
├── __init__.py            — package marker
├── activity_visibility.py — parse_activity_view, is_activity_visible
├── errors.py              — activity_error_to_http
├── models.py              — Pydantic request/response models
├── serializers.py         — entity_version_dict
└── typed_doc.py           — build_activity_description
```

#### `_helpers/activity_visibility.py` (158 lines)

Activity-visibility filtering shared by `dossiers.py`, `prov.py`,
and `prov_columns.py`.

- `parse_activity_view(raw_value)` — accepts the four shapes
  (`"all"`, `"own"`, `list[str]`, `dict(mode, include)`) and returns
  a normalized internal representation. Deny-safes legacy
  `"related"` values from older entries.
- `is_activity_visible(parsed_mode, activity_row, user)` — the
  per-activity filter predicate.

#### `_helpers/errors.py` (30 lines)

`activity_error_to_http(error: ActivityError) -> HTTPException` —
merges `error.payload` into `error.detail` so clients see one flat
JSON body with both the human message and machine-readable
diagnostic fields.

#### `_helpers/models.py` (204 lines)

Pydantic request and response models for the activity API.

- Item models: `UsedItem`, `GeneratedItem`, `RelationItem`.
- Request models: `ActivityRequest`, `BatchActivityRequest`,
  `BatchActivityItem`.
- Response models: `ActivityResponse`, `UsedResponse`,
  `GeneratedResponse`, `RelationResponse`, `DossierResponse`,
  `FullResponse` (the canonical activity-response envelope),
  `DossierDetailResponse` (the `GET /dossiers/{id}` shape).

#### `_helpers/serializers.py` (72 lines)

`entity_version_dict(row, include_entity_id=True)` — row-to-dict
serializer for version-listing endpoints. Always includes
`versionId`, `content`, `generatedBy`, `derivedFrom`,
`attributedTo`, `createdAt`; conditionally includes `entityId`
(dropped when the caller is already rendering inside an
entity_id-keyed dict), `schemaVersion` (dropped for legacy NULL),
and for tombstoned rows: `tombstonedBy` and `redirectTo`.

#### `_helpers/typed_doc.py` (196 lines)

Markdown documentation generator for typed activity endpoints.

- `build_activity_description(act_def, entity_schemas)` — the
  top-level renderer. Produces the OpenAPI description shown in
  Swagger UI for each typed endpoint. Walks the activity's YAML
  definition and emits sections for description, authorization,
  requirements, used entities, generated entities.
- `format_entity_schemas_for_doc(entity_type, act_def,
  entity_schemas)` — renders the JSON schema block(s) for a
  content-bearing entity type. For version-disciplined activities,
  one block per version; otherwise one unlabeled block.

### `routes/activities/` — activity execution endpoints

```
activities/
├── __init__.py        — re-exports register()
├── register.py        — the register() entry point + closures
├── typed.py           — _register_typed_route + _register_workflow_scoped_generic
└── run.py             — _run_activity + _emit_activity_success + _resolve_plugin_and_def
```

#### `activities/__init__.py`

Re-exports `register`.

#### `activities/register.py` (259 lines)

The `register(app, *, registry, get_user, global_access)` entry
point. Registers the activity execution endpoints at both URL
families (workflow-agnostic `/dossiers/...` and workflow-scoped
`/{workflow}/dossiers/...`) plus per-workflow typed wrappers via
`typed.py`.

Contains the shared handler closures `_handle_single` and
`_handle_batch` that close over FastAPI-level state (registry,
get_user) and are reused by every endpoint registration.

#### `activities/typed.py` (176 lines)

Per-workflow typed-route registrars.

- `_register_typed_route(...)` — one endpoint per (workflow,
  activity-type) with typed request/response schemas and an
  activity-specific OpenAPI description built via
  `_helpers/typed_doc.build_activity_description`.
- `_register_workflow_scoped_generic(...)` — one generic endpoint
  per workflow that accepts any activity type in the body.

#### `activities/run.py` (208 lines)

Pure helper functions (no closures over FastAPI state) used by the
register/typed modules.

- `_resolve_plugin_and_def(registry, activity_type, workflow)` —
  resolves `(plugin, activity_def)` from the request. Qualifies the
  type via `prov.activity_names.qualify` before lookup.
- `_run_activity(...)` — calls `engine.execute_activity` and shapes
  the response. Catches `ActivityError` and re-raises as
  `HTTPException` via `activity_error_to_http`.
- `_emit_activity_success(user, dossier_id, act_def, activity_id)`
  — post-commit audit emission. Called from the handler closures
  after `run_with_deadlock_retry` has committed successfully.

---

## `search/` — Elasticsearch integration

```
search/
├── __init__.py        — client, settings, ACL conventions
└── common_index.py    — common (cross-workflow) index operations
```

### `search/__init__.py` (209 lines)

ES client setup + shared ACL conventions:

- Connection config from env (`ES_URL`, `ES_API_KEY`).
- Client factory: `get_client()`.
- ACL helpers: `build_acl_for_dossier(access_entries)` — produces
  the flat list of role names and agent UUIDs that go into a
  document's `__acl__` field.
- Admin endpoint registration hooks used by plugins.

### `search/common_index.py` (211 lines)

The common dossier index — one doc per dossier, shared across
workflows. Fields: `dossier_id`, `workflow`, `onderwerp`, `__acl__`.

- `recreate_index()` — drop and recreate with mapping. Destructive.
- `reindex_all(registry)` — iterate every dossier in Postgres, build
  a common doc via its plugin's `common_doc_builder`, bulk index.
- `update_for_dossier(registry, dossier_id, dossier_row, session)` —
  call from `post_activity_hook` to update one dossier's doc after
  an activity.

---

## `worker/` — task worker process

```
worker/
├── __init__.py        — re-exports
├── cli.py             — main() argparse entry
├── loop.py            — worker_loop + _worker_loop_body
├── execution.py       — process_task + _execute_claimed_task + _refetch_task
├── polling.py         — find due tasks + claim one
├── failure.py         — retry + dead-letter + requeue
└── task_kinds.py      — per-kind handlers + complete_task + _resolve_triggering_user
```

`python -m dossier_engine.worker` boots the worker. `main` in
`cli.py` dispatches to either `worker_loop` (normal operation) or
`requeue_dead_letters` (admin command).

### `worker/__init__.py` (77 lines)

Re-exports the public API (`main`, `worker_loop`,
`requeue_dead_letters`, `complete_task`, `process_task`,
`find_due_tasks`) plus private helpers used by tests.

### `worker/cli.py` (120 lines)

`main()` — argparse entry point. Flags: `--config`, `--interval`,
`--once`, `--requeue-dead-letters`. Dispatches to `worker_loop` or
`requeue_dead_letters` with `asyncio.run`.

### `worker/loop.py` (250 lines)

- `worker_loop(config_path, poll_interval, once)` — the top-level
  coroutine. Wires up DB init, Sentry, signal handlers (SIGTERM,
  SIGINT), delegates to `_worker_loop_body`.
- `_worker_loop_body(session_factory, registry, shutdown,
  poll_interval, once)` — the polling loop. Claims one task at a
  time via `polling._claim_one_due_task`, dispatches to
  `execution.process_task`, sleeps, repeats until the shutdown flag
  is set. Failure path routes through `failure._record_failure` in a
  fresh transaction.

### `worker/polling.py` (234 lines)

Due-task discovery and claiming.

- `_parse_scheduled_for(value)` — ISO datetime parser with tolerance
  for Z-suffix and `+00:00` forms. (Separate from
  `engine.scheduling.parse_scheduled_for` which also handles
  relative offsets — the worker only sees absolute forms in DB
  rows.)
- `_build_scheduled_task_query(for_update=False)` — the base SELECT
  for scheduled tasks. Optionally `FOR UPDATE SKIP LOCKED` for
  claiming.
- `_is_task_due(task, now)` — point-in-time due check; also returns
  the due timestamp.
- `find_due_tasks(session)` — read-only snapshot of due tasks.
- `_claim_one_due_task(session)` — the atomic claim. Uses
  `FOR UPDATE SKIP LOCKED` so multiple workers don't fight for the
  same task.

### `worker/failure.py` (437 lines)

Retry scheduling, dead-lettering, and manual requeue.

- `_compute_next_attempt_at(attempt_count, base_delay, now)` —
  exponential backoff with jitter.
- `_record_failure(repo, task, exc, plugin, dossier_id, attempt,
  max_attempts)` — writes a failure outcome to the task. Decides
  retry vs dead-letter based on attempt count; emits Sentry via
  `capture_task_retry` or `capture_task_dead_letter`. Writes the new
  task version via `complete_task`.
- `_is_missing_schema_error(exc)` — classifies exceptions that mean
  "schema is out of date" (don't retry indefinitely).
- `_select_dead_lettered_tasks(session)` — the query used by
  `requeue_dead_letters`.
- `requeue_dead_letters(config_path)` — admin command. Walks
  dead-lettered tasks and creates fresh scheduled versions of them.
  Called from CLI.

### `worker/task_kinds.py` (370 lines)

Per-kind task dispatch + the shared finalizer.

- `complete_task(repo, plugin, dossier_id, task, *, status,
  result=None)` — writes a new task version with the given status
  (`completed`, `cancelled`, `failed`) plus the `completeTask`
  activity that authored it. Shared by all kinds.
- `_process_recorded(repo, plugin, dossier_id, task)` — kind 2:
  invoke the plugin function, record completion.
- `_process_scheduled_activity(repo, plugin, dossier_id, task,
  registry)` — kind 3: execute the target activity in the same
  dossier.
- `_process_cross_dossier(repo, plugin, dossier_id, task,
  registry)` — kind 4: invoke plugin function to determine target
  dossier, execute activity there, record completion back here.
- `_resolve_triggering_user(repo, activity_id)` — walks back from
  the task's `informed_by` to find the user that queued it.
  Preserves attribution across the async gap between queue time and
  execute time.

### `worker/execution.py` (172 lines)

Per-task execution wrapper.

- `process_task(task, registry, config)` — the high-level entry
  called by `loop._worker_loop_body`. Opens a session, delegates to
  `_execute_claimed_task`, routes exceptions to
  `failure._record_failure`.
- `_execute_claimed_task(session, task, registry)` — mid-level:
  opens the transaction, looks up the plugin + task kind, dispatches
  to the right `task_kinds._process_*` function, rolls back on
  error.
- `_refetch_task(session, task_id)` — re-read the task row inside
  the transaction. Handles the race where a task gets cancelled or
  superseded between claim and execute.

---

## Largest files (by design, after splits)

- `db/models/repository.py` (555) — one class, methods
  cross-reference heavily.
- `routes/prov.py` (510) — one registration function with nested
  route closures.
- `plugin/validators.py` (476) — five independent validators.
- `routes/prov_columns.py` (450) — one registration function with
  nested layout code.
- `worker/failure.py` (437) — five retry/dead-letter functions.
- `archive/orchestrator.py` (417) — one async function with many
  sequential steps.

Average file: ~230 lines. No file over 600 lines.

---

## Naming conventions

- **Leading-underscore packages** (`_helpers/`) are engine-private.
  Plugin code must not import from them.
- **Leading-underscore functions** (`_resolve_plugin_and_def`,
  `_import_dotted`, `_handle_domain_add`) are module-private by
  Python convention. Tests may still import them for fine-grained
  assertions; that's accepted.
- **`FieldValidator` vs `plugin/validators.py`** are different
  things. The former is a dataclass describing one per-field
  endpoint validator (client-facing API); the latter is the
  load-time contract checker.
- **`used.py` vs `lookups.py` vs `refs.py`** — `used.py` is the
  pipeline phase that resolves `used[]` references into rows;
  `lookups.py` is the set of engine-wide entity-lookup helpers
  (`lookup_singleton`, `resolve_from_trigger`); `refs.py` is the
  canonical ref-string parsing. Each has one job.
