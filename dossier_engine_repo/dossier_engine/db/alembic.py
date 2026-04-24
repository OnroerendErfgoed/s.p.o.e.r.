"""
Alembic migration runner — invoked during app startup.

Factored out of ``dossier_engine.app`` in Round 34 so ``app.py`` can
stay focused on FastAPI wiring. The module only exports
``_run_alembic_migrations``; ``app.py`` imports it and calls it from
the lifespan startup block, and ``app.py`` itself re-exports the
function for any existing call site that imported it from there.

Subprocess-based by necessity: Alembic's ``env.py`` calls
``asyncio.run()`` internally, which can't nest inside uvicorn's
already-running event loop when startup runs in-process.

Fail-fast policy: any Alembic failure aborts startup. The previous
behaviour — falling back to ``create_tables()`` on migration failure —
risked silently accepting a partially migrated schema. See the
docstring on ``_run_alembic_migrations`` for the full rationale.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

_log = logging.getLogger("dossier.app")


def _run_alembic_migrations(db_url: str) -> None:
    """Run ``alembic upgrade head`` in a subprocess. Raise RuntimeError
    on any failure.

    Subprocess is needed because alembic's env.py calls
    ``asyncio.run()`` internally, which can't nest inside uvicorn's
    already-running event loop when startup runs in-process.

    Fail-fast policy: any Alembic failure aborts startup. The previous
    behaviour — falling back to ``create_tables()`` on migration
    failure — risked silently accepting a partially migrated schema,
    where the upgrade had applied some DDL before erroring.
    ``create_tables`` (``Base.metadata.create_all``) no-ops on
    existing tables, so the half-applied state would survive, the app
    would come up, and requests would land on a schema that matched
    neither the model nor any Alembic revision. Data corruption,
    invisible until someone reads the startup WARNING they didn't
    notice. Refusing to start is the safe posture: the operator sees
    the failure, fixes the migration or the DB, and retries.

    Missing ``alembic.ini`` is also a hard error. Every real
    deployment of this service ships migration infrastructure; an
    install that doesn't is a broken deployment, not a "dev
    convenience" case. Tests set up the schema via
    ``tests/conftest.py::create_tables()`` directly, not via
    ``create_app()``, so this check doesn't hurt the test path.

    Raises:
        RuntimeError: if ``alembic.ini`` is not found at the expected
            path, or if ``alembic upgrade head`` returns a non-zero
            exit code.
    """
    alembic_ini = Path(__file__).parent.parent.parent / "alembic.ini"
    if not alembic_ini.exists():
        raise RuntimeError(
            f"alembic.ini not found at {alembic_ini} — the "
            "deployment is missing migration infrastructure. "
            "Install the engine from a source checkout (which "
            "includes alembic.ini + the alembic/ directory at "
            "the repo root), not from a wheel that ships only "
            "the Python package."
        )

    env = {**os.environ, "DOSSIER_DB_URL": db_url}
    result = subprocess.run(
        ["python3", "-m", "alembic", "upgrade", "head"],
        cwd=str(alembic_ini.parent),
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        # Log the full stderr before raising so operators have the
        # Alembic traceback in the app log regardless of how the
        # RuntimeError is handled upstream.
        _log.error(
            "Alembic migration failed (rc=%s). Aborting startup to "
            "avoid a partially migrated schema. Alembic stderr:\n%s",
            result.returncode, result.stderr,
        )
        raise RuntimeError(
            f"Alembic 'upgrade head' failed with rc={result.returncode}. "
            "Refusing to start with a possibly partial schema; see "
            "app log for Alembic stderr."
        )
    _log.info("Alembic migrations applied successfully")
