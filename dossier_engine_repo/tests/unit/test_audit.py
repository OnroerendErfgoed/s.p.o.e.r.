"""
Unit tests for the audit module.

Exercises the three main behaviors:

1. emit_audit() is a silent no-op when configure_audit_logging() has
   not been called. This is the default state for dev/test and it
   MUST NOT raise — code calling emit_audit() has no way to know
   whether audit is configured.

2. After configuration, events are written as NDJSON (one complete
   JSON object per line). Each line must round-trip through
   json.loads() independently — Wazuh's json log_format depends on
   this shape.

3. Unwritable paths don't crash configuration; they return False.
   The app must still start when /var/log/dossier isn't provisioned.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _reload_audit_module():
    """Force a fresh import of audit so each test starts with a clean
    module-level state (no handlers attached, _configured=False).

    The module uses module-level globals for the configured flag and
    for handler attachment, which is fine in production (configure
    runs once at startup) but makes test isolation non-trivial.
    Reloading resets the module globals; we also clear the named
    logger's handler list because Python's `logging` caches logger
    objects by name across reloads and handlers would otherwise
    accumulate across tests."""
    import logging
    logging.getLogger("dossier.audit").handlers.clear()
    import dossier_engine.observability.audit as audit_mod
    return importlib.reload(audit_mod)


class TestEmitAuditUnconfigured:
    """Default state: no handler, emit is a no-op."""

    def test_emit_without_configure_is_silent_noop(self, tmp_path, capsys):
        """Calling emit_audit() without configuring first must not
        raise and must not write anywhere. This is the state every
        test run starts in, and every `import dossier_engine.audit`
        without an explicit configure() call."""
        audit = _reload_audit_module()

        # Must not raise.
        audit.emit_audit(
            action="dossier.read",
            actor_id="u1", actor_name="Test",
            target_type="Dossier", target_id="d1",
            outcome="allowed",
        )

        # Nothing should be in the test tmp_path.
        assert list(tmp_path.iterdir()) == []


class TestConfigure:
    """Path handling, writability, return value."""

    def test_writable_path_returns_true(self, tmp_path):
        """Good path → returns True, creates the file on first emit."""
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"

        ok = audit.configure_audit_logging(path=str(log_file))
        assert ok is True

    def test_unwritable_path_returns_false(self, tmp_path, caplog):
        """Directory doesn't exist → returns False, logs warning,
        doesn't crash. The module uses `dossier` logger for the
        warning (not `dossier.audit`, which has propagate=False)."""
        audit = _reload_audit_module()
        missing = tmp_path / "does" / "not" / "exist" / "audit.json"

        with caplog.at_level("WARNING", logger="dossier"):
            ok = audit.configure_audit_logging(path=str(missing))

        assert ok is False
        assert any("not writable" in rec.message for rec in caplog.records)

    def test_configure_is_idempotent(self, tmp_path):
        """Calling configure twice → second call is a no-op (returns
        True because config succeeded once; doesn't double-attach
        handlers, which would double-write every event)."""
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"

        assert audit.configure_audit_logging(path=str(log_file)) is True
        # Second call with a DIFFERENT path should be ignored.
        other = tmp_path / "other.json"
        assert audit.configure_audit_logging(path=str(other)) is True

        # Handler count should be 1 (not 2).
        import logging
        logger = logging.getLogger("dossier.audit")
        assert len(logger.handlers) == 1


class TestEmitAuditConfigured:
    """After configure(), events land in the file as NDJSON."""

    def test_single_event_is_one_line_of_json(self, tmp_path):
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"
        audit.configure_audit_logging(path=str(log_file))

        audit.emit_audit(
            action="dossier.exported",
            actor_id="claeyswo", actor_name="Claeys Wouter",
            target_type="Dossier", target_id="d1-uuid",
            outcome="allowed",
            dossier_id="d1-uuid",
            export_format="pdfa3",
            bytes_sent=12345,
        )
        # Flush the file handler.
        import logging
        for h in logging.getLogger("dossier.audit").handlers:
            h.flush()

        content = log_file.read_text()
        lines = content.splitlines()
        assert len(lines) == 1

        event = json.loads(lines[0])
        assert event["event_action"] == "dossier.exported"
        assert event["actor"] == {"id": "claeyswo", "name": "Claeys Wouter"}
        assert event["target"] == {"type": "Dossier", "id": "d1-uuid"}
        assert event["outcome"] == "allowed"
        assert event["dossier_id"] == "d1-uuid"
        assert event["extra"] == {"export_format": "pdfa3", "bytes_sent": 12345}
        assert "@timestamp" in event

    def test_multiple_events_each_on_own_line(self, tmp_path):
        """Wazuh's json log_format requires exactly one JSON object
        per line. Two emits → two lines, each independently parseable."""
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"
        audit.configure_audit_logging(path=str(log_file))

        for i in range(3):
            audit.emit_audit(
                action="dossier.read",
                actor_id=f"u{i}", actor_name=f"User {i}",
                target_type="Dossier", target_id=f"d{i}",
                outcome="allowed",
                dossier_id=f"d{i}",
            )
        import logging
        for h in logging.getLogger("dossier.audit").handlers:
            h.flush()

        lines = log_file.read_text().splitlines()
        assert len(lines) == 3
        # Each line MUST be parseable on its own.
        events = [json.loads(line) for line in lines]
        assert [e["target"]["id"] for e in events] == ["d0", "d1", "d2"]

    def test_denied_outcome_includes_reason(self, tmp_path):
        """denied/error outcomes carry a human-readable reason."""
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"
        audit.configure_audit_logging(path=str(log_file))

        audit.emit_audit(
            action="dossier.denied",
            actor_id="attacker", actor_name="Someone Else",
            target_type="Dossier", target_id="d-confidential",
            outcome="denied",
            dossier_id="d-confidential",
            reason="User has no role in this dossier",
        )
        import logging
        for h in logging.getLogger("dossier.audit").handlers:
            h.flush()

        event = json.loads(log_file.read_text().strip())
        assert event["outcome"] == "denied"
        assert event["reason"] == "User has no role in this dossier"

    def test_no_propagation_to_root_logger(self, tmp_path, caplog):
        """Audit events must NOT propagate to the root logger —
        otherwise they'd also end up in stderr/Sentry/etc with the
        wrong retention. The propagate=False is a critical contract."""
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"
        audit.configure_audit_logging(path=str(log_file))

        with caplog.at_level("INFO"):
            audit.emit_audit(
                action="dossier.read",
                actor_id="u1", actor_name="Test",
                target_type="Dossier", target_id="d1",
                outcome="allowed",
            )

        # No record from dossier.audit in the root capture.
        audit_records = [r for r in caplog.records if r.name == "dossier.audit"]
        assert audit_records == []


class TestEmitDossierAudit:
    """The dossier-scoped convenience wrapper — D4/D22 refactor.

    Seven route call sites repeated the same 8-keyword emit_audit
    boilerplate (target_type="Dossier", target_id=str(dossier_id),
    dossier_id=str(dossier_id), actor_id=user.id,
    actor_name=user.name). The wrapper encapsulates that so the
    callers don't have to. These tests pin down the wrapper's
    contract so a future refactor can't silently change the wire
    shape it produces — SIEM alert rules key on the exact payload
    structure.
    """

    def test_produces_same_payload_as_long_form(self, tmp_path):
        """Wire-level equivalence: a wrapper call must produce the
        same payload as the equivalent emit_audit call. If this
        ever diverges, existing SIEM rules break."""
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"
        audit.configure_audit_logging(path=str(log_file))

        class FakeUser:
            id = "alice"
            name = "Alice"

        # Emit one event via the wrapper, one via the long form.
        # The payloads should match modulo the timestamp.
        audit.emit_dossier_audit(
            action="dossier.read",
            user=FakeUser(),
            dossier_id="d1-uuid",
            outcome="allowed",
            workflow="toelatingen",
        )
        audit.emit_audit(
            action="dossier.read",
            actor_id="alice", actor_name="Alice",
            target_type="Dossier", target_id="d1-uuid",
            outcome="allowed",
            dossier_id="d1-uuid",
            workflow="toelatingen",
        )

        import logging
        for h in logging.getLogger("dossier.audit").handlers:
            h.flush()

        lines = log_file.read_text().splitlines()
        assert len(lines) == 2
        wrapper_event = json.loads(lines[0])
        long_event = json.loads(lines[1])
        # Strip timestamps — they differ by microseconds.
        wrapper_event.pop("@timestamp")
        long_event.pop("@timestamp")
        assert wrapper_event == long_event

    def test_stringifies_uuid_dossier_id(self, tmp_path):
        """Callers pass UUIDs directly; the wrapper must stringify
        once (for target_id) and propagate that same string as
        dossier_id on the payload. Skipping the str() would leak a
        UUID object into the JSON serializer, which works today
        by accident but is brittle — pin the contract down."""
        from uuid import UUID
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"
        audit.configure_audit_logging(path=str(log_file))

        class FakeUser:
            id = "alice"
            name = "Alice"

        did = UUID("12345678-1234-1234-1234-123456789abc")
        audit.emit_dossier_audit(
            action="dossier.read",
            user=FakeUser(),
            dossier_id=did,
            outcome="allowed",
        )
        import logging
        for h in logging.getLogger("dossier.audit").handlers:
            h.flush()

        event = json.loads(log_file.read_text().splitlines()[0])
        assert event["target"]["id"] == str(did)
        assert event["dossier_id"] == str(did)
        # Same value in both slots — this is the invariant every
        # route caller depended on when building the long-form
        # emit_audit call.
        assert event["target"]["id"] == event["dossier_id"]

    def test_reason_and_extra_flow_through(self, tmp_path):
        """denied events pass a `reason`; non-standard kwargs flow
        into the payload's `extra` object. Both must survive the
        wrapper's argument munging intact."""
        audit = _reload_audit_module()
        log_file = tmp_path / "audit.json"
        audit.configure_audit_logging(path=str(log_file))

        class FakeUser:
            id = "alice"
            name = "Alice"

        audit.emit_dossier_audit(
            action="dossier.denied",
            user=FakeUser(),
            dossier_id="d1-uuid",
            outcome="denied",
            reason="no role match",
            activity_type="bewerkAanvraag",
            activity_id="a1-uuid",
        )
        import logging
        for h in logging.getLogger("dossier.audit").handlers:
            h.flush()

        event = json.loads(log_file.read_text().splitlines()[0])
        assert event["reason"] == "no role match"
        assert event["extra"] == {
            "activity_type": "bewerkAanvraag",
            "activity_id": "a1-uuid",
        }

    def test_silent_when_unconfigured(self, tmp_path):
        """Unconfigured → no-op. Same contract as emit_audit; the
        wrapper mustn't add a failure mode the base function
        doesn't have."""
        audit = _reload_audit_module()

        class FakeUser:
            id = "alice"
            name = "Alice"

        # Must not raise.
        audit.emit_dossier_audit(
            action="dossier.read",
            user=FakeUser(),
            dossier_id="d1-uuid",
            outcome="allowed",
        )
        assert list(tmp_path.iterdir()) == []
