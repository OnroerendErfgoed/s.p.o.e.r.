"""
Worker main loop.

Two functions:

* ``worker_loop`` — the top-level coroutine. Wires up DB init,
  sentry, signal handlers, and delegates to ``_worker_loop_body``.
* ``_worker_loop_body`` — the actual polling loop. Claims one
  task at a time, dispatches to ``process_task``, sleeps, repeats
  until the shutdown flag is set.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from ..app import load_config_and_registry
from ..db import init_db, get_session_factory
from ..observability.sentry import init_sentry_worker, capture_worker_loop_crash

from .polling import _claim_one_due_task
from .execution import process_task
from .failure import _record_failure

logger = logging.getLogger("dossier.worker")


async def worker_loop(config_path: str = "config.yaml", poll_interval: int = 10, once: bool = False):
    """Main worker loop.

    Two nested loops:

    * **Outer loop** — controls the poll cadence. Sleeps `poll_interval`
      seconds between drain passes, or until SIGTERM arrives. Exits
      on SIGTERM or after a single pass in `--once` mode.

    * **Inner drain loop** — repeatedly claims one task at a time
      via `_claim_one_due_task`, executes it inside the same
      transaction that holds the row lock, and commits. Breaks when
      `_claim_one_due_task` returns None (nothing claimable this
      cycle — either backlog drained or everything remaining is
      locked by other workers).

    The claim-lock-execute pattern gives us concurrency safety for
    multi-worker deployments: `SELECT ... FOR UPDATE OF entities
    SKIP LOCKED` means worker A's locked row is invisible to worker
    B's next claim attempt. When A commits (success) or rolls back
    (failure), the lock releases and B's subsequent claim either sees
    the new `completed` / `dead_letter` status (and skips the row)
    or sees `scheduled` with updated `next_attempt_at` (and respects
    the retry delay). No two workers ever execute the same task
    version concurrently.

    Signal handling: SIGTERM and SIGINT set an `asyncio.Event`. The
    outer loop's interruptible sleep (`asyncio.wait_for` on the
    event) returns immediately when the signal arrives. The inner
    drain loop also checks the event at the top of each iteration,
    so a SIGTERM mid-drain finishes the in-flight task cleanly (its
    transaction runs to completion) and then exits without starting
    the next one. We never interrupt a task mid-transaction —
    doing so would leak locks and potentially leave the dossier
    state in a half-written form.

    Failure handling: if `_execute_claimed_task` raises, the error
    is routed through `_record_failure` in a *fresh* transaction.
    The original transaction is rolled back (so the locked row's
    content state isn't touched), and the fresh transaction writes
    a new task version with the retry decision (retry with backoff,
    or dead_letter). The new task version goes through
    `complete_task → execute_activity` so it gets validated and the
    post-activity hook fires.
    """
    config, registry = load_config_and_registry(config_path)

    db_url = config.get("database", {}).get("url")
    if not db_url:
        raise RuntimeError(
            "database.url is required in config (Postgres connection string)"
        )
    await init_db(db_url)
    # Schema is managed by Alembic migrations (run via the API startup
    # or `alembic upgrade head`). The worker does not create or migrate
    # tables — it only needs the engine connection.

    # Initialize Sentry if SENTRY_DSN is set. No-op otherwise.
    # Placed after config load so deployments can override DSN via
    # config in the future if they want to, though env var wins for now.
    init_sentry_worker()

    shutdown = asyncio.Event()

    def _on_signal(signum, _frame):
        logger.info(f"Worker received signal {signum}, shutting down gracefully")
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)

    logger.info(f"Worker started. Poll interval: {poll_interval}s. Once: {once}")

    session_factory = get_session_factory()

    try:
        await _worker_loop_body(session_factory, registry, shutdown, poll_interval, once)
    except (KeyboardInterrupt, SystemExit):
        # Not a crash — these are expected ways to stop the worker.
        raise
    except Exception as exc:
        # Top-level worker-loop crash. This is the "the worker itself
        # died" signal — distinct from the per-task retry/dead_letter
        # events handled inside the loop. Single fingerprint so all
        # such crashes group into one Sentry issue.
        logger.exception("Worker loop crashed")
        capture_worker_loop_crash(exc)
        raise
    finally:
        logger.info("Worker stopped")


async def _worker_loop_body(session_factory, registry, shutdown, poll_interval: int, once: bool):
    """Extracted body of the poll/drain loop. See `worker_loop` for
    the top-level orchestration (config load, DB init, Sentry init,
    signal wiring, top-level try/except)."""

    # Track whether we've ever successfully polled. Before the first
    # successful poll, we tolerate "schema not ready" errors as a
    # startup-race condition (Bug 75): when the worker is launched
    # concurrently with the app, and the app is the process that
    # runs Alembic migrations at startup, there's a window where
    # the worker polls and the tables don't exist yet. Rather than
    # crashing and needing an external supervisor to restart us,
    # we just sleep and retry. After the first successful poll, any
    # subsequent UndefinedTableError is a real bug — tables don't
    # disappear — and we let it propagate to the top-level crash
    # handler.
    schema_ready = False

    while not shutdown.is_set():
        # Inner drain loop — keep claiming and executing one task at
        # a time until _claim_one_due_task returns None (nothing
        # claimable this cycle). Each iteration is its own session
        # and its own transaction; the lock held by the SELECT FOR
        # UPDATE persists for the lifetime of that transaction and
        # is released on commit or rollback.
        processed_this_cycle = 0
        while not shutdown.is_set():
            task_for_failure_path: EntityRow | None = None
            failure: Exception | None = None

            try:
                async with session_factory() as session:
                    async with session.begin():
                        task = await _claim_one_due_task(session)
                        if task is None:
                            schema_ready = True  # poll worked, even if empty
                            break  # nothing claimable — leave the drain loop

                        schema_ready = True  # claim worked; tables exist
                        try:
                            await _execute_claimed_task(session, task, registry)
                            processed_this_cycle += 1
                        except Exception as e:
                            # Capture the exception so we can handle it in a
                            # fresh transaction below. The `async with
                            # session.begin()` will roll back this transaction
                            # on the way out because we're re-raising — no,
                            # wait, we don't want to re-raise, we want the
                            # transaction to roll back cleanly and then handle
                            # failure separately. Do that by catching here
                            # and remembering the task + exception, then
                            # exiting the inner `begin()` block by falling
                            # through to the end of the `with`. That commits
                            # the (empty) transaction, which is fine because
                            # _claim_one_due_task only did a SELECT.
                            logger.error(
                                f"Task {task.id} execution failed: {e}",
                                exc_info=True,
                            )
                            task_for_failure_path = task
                            failure = e
            except Exception as claim_exc:
                # Separate handling for claim-phase failures (the
                # `task = await _claim_one_due_task(...)` call above).
                # The startup-race case is the important one: during
                # the window between worker launch and migrations
                # completing in the app process, ``entities`` doesn't
                # exist and the SELECT raises UndefinedTableError.
                # Sleep and try again rather than crashing the loop.
                if not schema_ready and _is_missing_schema_error(claim_exc):
                    logger.warning(
                        "Worker poll: schema not ready yet "
                        "(likely waiting for migrations); will retry",
                    )
                    # Break out of the drain loop and fall through to
                    # the normal poll-interval sleep. Another iteration
                    # of the outer loop will retry.
                    break
                # Not a startup race — real problem. Let the top-level
                # crash handler in worker_loop see it.
                raise

            # If execution failed, record the failure in a fresh
            # transaction. The original session/transaction from the
            # claim is already closed — its SELECT-only work committed
            # cleanly — and the failure write path needs its own
            # transaction to land the new task version through
            # execute_activity.
            if task_for_failure_path is not None and failure is not None:
                try:
                    async with session_factory() as fail_session:
                        async with fail_session.begin():
                            fail_repo = Repository(fail_session)
                            fail_dossier = await fail_repo.get_dossier(
                                task_for_failure_path.dossier_id,
                            )
                            fail_plugin = (
                                registry.get(fail_dossier.workflow)
                                if fail_dossier else None
                            )
                            if fail_plugin:
                                await _record_failure(
                                    fail_repo,
                                    fail_plugin,
                                    task_for_failure_path.dossier_id,
                                    task_for_failure_path,
                                    failure,
                                )
                except Exception as e2:
                    logger.error(
                        f"Task {task_for_failure_path.id}: failed to record "
                        f"failure (will be retried by next poll): {e2}",
                        exc_info=True,
                    )
                processed_this_cycle += 1  # count as drained to avoid spinning

        if processed_this_cycle:
            logger.info(f"Drain cycle: processed {processed_this_cycle} tasks")

        if once:
            break

        # Interruptible sleep between poll cycles.
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


