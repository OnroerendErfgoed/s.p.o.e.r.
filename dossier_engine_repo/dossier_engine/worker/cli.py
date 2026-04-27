"""
Worker command-line entry point.

Called via ``python -m dossier_engine.worker``. Parses argv, dispatches
to either ``worker_loop`` (normal operation) or
``requeue_dead_letters`` (admin command).
"""
from __future__ import annotations

import argparse
import asyncio

from .loop import worker_loop
from .failure import requeue_dead_letters


def main():
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="python -m dossier_engine.worker",
        description=(
            "Dossier task worker. Polls for due system:task entities and "
            "executes them (recorded functions, scheduled activities, "
            "cross-dossier activities). Runs against the same database as "
            "the dossier API."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to the deployment's config.yaml. If omitted, resolves "
            "the path via the installed dossier_app package."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Poll interval in seconds between scans for due tasks (default: 10).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain all currently-due tasks and exit instead of polling forever.",
    )
    parser.add_argument(
        "--requeue-dead-letters",
        action="store_true",
        help=(
            "Requeue dead-lettered tasks: write fresh revisions with "
            "status=scheduled, attempt_count=0, next_attempt_at cleared. "
            "Scope defaults to all dead-lettered tasks across all "
            "dossiers; narrow with --dossier and/or --task. The requeue "
            "runs once and exits (does not start a drain cycle). Use "
            "a separate `--once` invocation afterward if you want to "
            "immediately execute the requeued tasks."
        ),
    )
    parser.add_argument(
        "--dossier",
        default=None,
        help=(
            "Scope filter for --requeue-dead-letters: only requeue "
            "dead-lettered tasks belonging to the given dossier UUID."
        ),
    )
    parser.add_argument(
        "--task",
        default=None,
        help=(
            "Scope filter for --requeue-dead-letters: only requeue the "
            "task with the given logical entity UUID (system:task "
            "entity_id, not version_id)."
        ),
    )
    args = parser.parse_args()

    # Default config path via installed dossier_app package, same
    # pattern file_service uses. Lets the worker launch from any cwd.
    config_path = args.config
    if config_path is None:
        try:
            import dossier_app
            config_path = str(Path(dossier_app.__file__).parent / "config.yaml")
        except ImportError:
            config_path = "config.yaml"

    # --requeue-dead-letters is an admin one-shot action. It shares
    # the config/db bootstrap with the polling loop but runs a
    # different top-level coroutine and exits when done.
    if args.requeue_dead_letters:
        dossier_uuid = UUID(args.dossier) if args.dossier else None
        task_uuid = UUID(args.task) if args.task else None
        asyncio.run(requeue_dead_letters(
            config_path=config_path,
            dossier_id=dossier_uuid,
            task_entity_id=task_uuid,
        ))
        return

    if args.dossier or args.task:
        parser.error(
            "--dossier and --task are only valid with --requeue-dead-letters"
        )

    asyncio.run(worker_loop(
        config_path=config_path,
        poll_interval=args.interval,
        once=args.once,
    ))


if __name__ == "__main__":
    main()
