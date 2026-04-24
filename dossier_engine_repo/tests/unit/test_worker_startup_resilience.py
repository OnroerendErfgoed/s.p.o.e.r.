"""Worker startup resilience — Bug 75.

The worker is sometimes launched concurrently with the app (on CI,
in docker-compose setups, in Kubernetes where init ordering is
best-effort). The app runs Alembic migrations in its startup event
handler, so between "worker process started" and "app finished
migrating" there's a window where the worker's first poll hits a
``relation "entities" does not exist`` error.

Before the fix, that error crashed the top-level worker loop and
an external supervisor had to restart the process. After the fix,
the first poll that sees a missing schema logs a warning, sleeps
one poll interval, and retries. Once any poll succeeds, the
``schema_ready`` flag flips and future UndefinedTableError is
treated as the real bug it would be (tables don't vanish
mid-operation).

This file tests only the detector function and the flag logic —
the full loop is covered by `test_requests.sh` running through
``scripts/ci_run_shell_spec.sh``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy.exc import DBAPIError

from dossier_engine.worker import _is_missing_schema_error


def _db_error_with_sqlstate(sqlstate: str) -> DBAPIError:
    """Construct a DBAPIError whose wrapped driver error has the
    given SQLSTATE. Direct constructor (not ``.instance()``) so
    the result is a real DBAPIError, not a StatementError."""
    orig = MagicMock()
    orig.sqlstate = sqlstate
    return DBAPIError("SELECT 1", None, orig)


class TestIsMissingSchemaError:

    def test_undefined_table_true(self):
        """42P01 (undefined_table) is the startup-race shape —
        must be recognized so the worker can log-and-retry."""
        assert _is_missing_schema_error(_db_error_with_sqlstate("42P01")) is True

    def test_deadlock_false(self):
        """40P01 (deadlock_detected) is a real-operation error,
        not a schema-not-ready error. The worker handles deadlock
        elsewhere (route-level retry wrapper); this detector must
        not false-positive on it."""
        assert _is_missing_schema_error(_db_error_with_sqlstate("40P01")) is False

    def test_unique_violation_false(self):
        """23505 (unique_violation) is a data-integrity error,
        not a schema error. Retrying would mask bugs."""
        assert _is_missing_schema_error(_db_error_with_sqlstate("23505")) is False

    def test_non_db_exception_false(self):
        """Plain exceptions can't be schema errors. The detector
        is typed against DBAPIError; anything else must pass
        through as not-a-schema-issue."""
        assert _is_missing_schema_error(ValueError("oops")) is False
        assert _is_missing_schema_error(RuntimeError("nope")) is False
        assert _is_missing_schema_error(None) is False  # defensive

    def test_detects_via_cause_chain(self):
        """Some driver/wrapper combinations surface the sqlstate
        via ``__cause__`` rather than ``.orig``. Mirror the same
        double-check the deadlock detector uses so both behave
        consistently across SQLAlchemy versions."""
        orig_without_sqlstate = MagicMock(spec=[])  # no attr
        err = DBAPIError("SELECT 1", None, orig_without_sqlstate)

        class FakeDriverError(Exception):
            sqlstate = "42P01"

        err.__cause__ = FakeDriverError("relation does not exist")
        assert _is_missing_schema_error(err) is True
