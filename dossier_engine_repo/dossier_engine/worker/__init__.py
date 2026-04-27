"""
Task worker.

Polls for due ``system:task`` entities and executes them. Runs as a
separate process: ``python -m dossier_engine.worker``.

Task types:
  - recorded (type 2): call function, completeTask with result
  - scheduled_activity (type 3): execute_activity in same dossier,
    completeTask
  - cross_dossier_activity (type 4): call function for target,
    execute_activity in target dossier, completeTask in source dossier

All operations run within a single DB transaction. If anything fails,
everything rolls back.

Layout (Round 34 split):
    worker/
    ├── __init__.py       — re-exports
    ├── polling.py        — due-task discovery + claim (5 functions)
    ├── failure.py        — retry / dead-letter / requeue (5 functions)
    ├── task_kinds.py     — per-kind handlers + complete_task (4 functions)
    ├── execution.py      — process_task + _execute_claimed_task (4 functions)
    ├── loop.py           — worker_loop + _worker_loop_body
    └── cli.py            — main() / argparse entry
"""
from .polling import (
    _parse_scheduled_for,
    _is_task_due,
    find_due_tasks,
    _claim_one_due_task,
)
from .failure import (
    _compute_next_attempt_at,
    _record_failure,
    _is_missing_schema_error,
    _select_dead_lettered_tasks,
    requeue_dead_letters,
)
from .task_kinds import (
    complete_task,
    _process_recorded,
    _process_scheduled_activity,
    _process_cross_dossier,
    _resolve_triggering_user,
)
from .execution import (
    process_task,
    _execute_claimed_task,
    _refetch_task,
)
from .loop import worker_loop
from .cli import main

__all__ = [
    # public API
    "main",
    "worker_loop",
    "requeue_dead_letters",
    "process_task",
    "complete_task",
    "find_due_tasks",
    # private helpers exported for test access
    "_parse_scheduled_for",
    "_is_task_due",
    "_claim_one_due_task",
    "_compute_next_attempt_at",
    "_record_failure",
    "_is_missing_schema_error",
    "_select_dead_lettered_tasks",
    "_process_recorded",
    "_process_scheduled_activity",
    "_process_cross_dossier",
    "_execute_claimed_task",
    "_refetch_task",
    "_resolve_triggering_user",
]
