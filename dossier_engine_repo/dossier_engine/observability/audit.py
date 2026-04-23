"""
Audit log emission.

Separate from the PROV graph (which records successful state transitions)
and from Sentry (which captures exceptions), the audit log is an
append-only record of **who did what to what, when** — including reads,
denials, and exports. It exists to answer compliance questions like
"who looked up applicant X's dossier?" or "who exported data between
dates Y and Z?"

Design:

* Events are emitted as newline-delimited JSON (NDJSON) to a file on
  disk. One complete JSON object per line, `\n` terminated.
* A Wazuh agent on the same host tails the file (see the README
  "Audit Log" section for the agent configuration) and forwards
  events to the SIEM. Your application does not talk to Wazuh over
  the network — it writes a file, Wazuh reads the file.
* The file path is typically `/var/log/dossier/audit.json`. Rotation
  is handled here (the `RotatingFileHandler`), not by logrotate;
  the Wazuh agent picks up rotated files correctly because it tails
  on inode, not on filename.
* Writes are best-effort and never fail the caller. If the log path
  is unwritable, audit emission is a no-op and a single warning is
  logged on first failure. The user request must not fail because
  the audit sink is temporarily unavailable.
* `propagate = False` keeps audit events out of the root logger's
  chain — otherwise they would also reach Sentry and stderr, which
  is the wrong retention and the wrong trust boundary.

Usage from route handlers and pipeline code::

    from dossier_engine.observability.audit import emit_dossier_audit

    emit_dossier_audit(
        action="dossier.read",
        user=user,
        dossier_id=dossier_id,
        outcome="allowed",
    )

Use the lower-level ``emit_audit`` for events that aren't
dossier-scoped (entity-scoped events, worker-level events with
non-Dossier targets). For dossier-scoped events — the common
case — ``emit_dossier_audit`` saves the repeated
``actor_id=user.id``/``actor_name=user.name`` and the dossier-id
double-pass.

In dev and test environments the default path is unwritable, so
`configure_audit_logging()` is a no-op unless explicitly called.
Tests that need to inspect emitted events can capture the
``dossier.audit`` logger directly.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

_log = logging.getLogger("dossier.audit")
_log.setLevel(logging.INFO)
_log.propagate = False  # don't let audit events leak to stderr/Sentry/root logger

_configured = False
_configuration_warned = False


class _NDJSONFormatter(logging.Formatter):
    """Render each record as a single-line JSON object, `\n` terminated
    by the logging framework. Wazuh's ``json`` log_format expects this
    exact shape: one complete JSON object per physical line, no pretty
    printing, no wrapping."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = getattr(record, "audit_payload", None) or {
            # Fallback so a stray direct `logger.info()` call doesn't
            # produce un-parseable output. Should not happen in normal
            # operation — every caller goes through emit_audit().
            "action": "unknown",
            "message": record.getMessage(),
        }
        # ISO-8601 with microseconds and explicit UTC offset. Wazuh's
        # JSON decoder will recognize `@timestamp` as the event time;
        # adding it here rather than relying on the agent's ingest time
        # means a delayed write doesn't get misattributed to a later
        # time bucket.
        payload["@timestamp"] = datetime.now(timezone.utc).isoformat()
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_audit_logging(
    path: str | None = None,
    max_bytes: int = 100 * 1024 * 1024,   # 100 MB
    backup_count: int = 10,               # 10 × 100 MB = 1 GB ceiling
) -> bool:
    """Wire the ``dossier.audit`` logger to write NDJSON to disk.

    Returns ``True`` if configuration succeeded, ``False`` if the path
    is unwritable (in which case emit_audit() becomes a no-op). Safe
    to call multiple times — subsequent calls are ignored after the
    first successful configuration.

    Reads ``DOSSIER_AUDIT_LOG_PATH`` from the environment if ``path``
    is ``None``. If that's unset, defaults to ``/var/log/dossier/audit.json``.

    The rotation is `inode-stable` in the sense that the Wazuh agent
    follows the file by name+inode, and when the RotatingFileHandler
    renames the file on rotation, the agent keeps reading until EOF
    on the old inode and then picks up the new file. No lost events
    at rotation boundaries.
    """
    global _configured, _configuration_warned

    if _configured:
        return True

    effective_path = path or os.environ.get(
        "DOSSIER_AUDIT_LOG_PATH", "/var/log/dossier/audit.json",
    )

    try:
        directory = os.path.dirname(effective_path)
        if directory and not os.path.isdir(directory):
            # Don't try to create /var/log/... from here; if the
            # directory doesn't exist, the host wasn't provisioned
            # for audit and we shouldn't silently write somewhere
            # the Wazuh agent isn't watching.
            raise FileNotFoundError(
                f"audit log directory does not exist: {directory}"
            )
        handler = RotatingFileHandler(
            effective_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(_NDJSONFormatter())
        _log.addHandler(handler)
        _configured = True
        logging.getLogger("dossier").info(
            "Audit log configured: %s (rotation: %d × %d bytes)",
            effective_path, backup_count, max_bytes,
        )
        return True
    except (OSError, PermissionError) as exc:
        if not _configuration_warned:
            logging.getLogger("dossier").warning(
                "Audit log path %s is not writable (%s); "
                "emit_audit() will be a no-op until configured",
                effective_path, exc,
            )
            _configuration_warned = True
        return False


def emit_audit(
    *,
    action: str,
    actor_id: str,
    actor_name: str,
    target_type: str,
    target_id: str,
    outcome: str,              # "allowed" | "denied" | "error"
    dossier_id: str | None = None,
    reason: str | None = None,
    **extra: Any,
) -> None:
    """Emit a single audit event.

    Non-blocking. If the audit logger has no handlers attached (e.g.
    because ``configure_audit_logging`` wasn't called or failed), the
    call is a silent no-op — audit emission is never permitted to
    fail the caller.

    Parameters:

    * ``action`` — a namespaced verb like ``dossier.read`` or
      ``worker.task_executed``. Keep the vocabulary small and stable;
      SIEM alert rules key on the exact string.
    * ``actor_id`` / ``actor_name`` — who did it. ``actor_id`` is
      the stable identifier (usually a username or user UUID);
      ``actor_name`` is for human-readable reports. For worker-driven
      activities use ``"system"`` / ``"Systeem"``.
    * ``target_type`` / ``target_id`` — what was acted on. Usually
      ``"Dossier"`` + the dossier UUID, but could be ``"Entity"`` +
      entity UUID, etc.
    * ``outcome`` — ``"allowed"`` for successful operations,
      ``"denied"`` for authorization refusals, ``"error"`` for
      validation or pipeline failures. Keep to exactly these three.
    * ``dossier_id`` — always the containing dossier's UUID when the
      event is dossier-scoped. Denormalized so SIEM queries can
      filter without having to join on target.
    * ``reason`` — free-text explanation for ``denied`` / ``error``.
    * ``**extra`` — extra structured fields that flow through to the
      event's ``extra`` object in the NDJSON payload. Use for action-
      specific context (export format, query text, file id, etc).
    """
    if not _log.handlers:
        # No handler attached → not configured → silent no-op.
        # This makes the module safe to import and call from dev
        # and test environments where the audit path isn't
        # provisioned.
        return

    payload: dict[str, Any] = {
        # NOTE: the JSON key is `event_action`, not `action`. Wazuh
        # reserves 13 static-field names (user, srcip, dstip, srcport,
        # dstport, protocol, action, id, url, data, extra_data, status,
        # system_name) — rules can't match dynamic fields with those
        # names. Renaming at the producer avoids the footgun at every
        # rule-writing site. The Python keyword argument stays `action`
        # for readability; only the wire-level JSON key is remapped.
        "event_action": action,
        "actor": {"id": actor_id, "name": actor_name},
        "target": {"type": target_type, "id": target_id},
        "outcome": outcome,
    }
    if dossier_id is not None:
        payload["dossier_id"] = dossier_id
    if reason is not None:
        payload["reason"] = reason
    if extra:
        payload["extra"] = extra

    try:
        _log.info(action, extra={"audit_payload": payload})
    except Exception:
        # Audit emission must never fail the caller. Swallow and move
        # on; the underlying handler will have logged its own failure.
        # This path is hit e.g. if the disk fills up mid-request.
        pass


def emit_dossier_audit(
    *,
    action: str,
    user: Any,
    dossier_id: Any,
    outcome: str,
    reason: str | None = None,
    **extra: Any,
) -> None:
    """Convenience wrapper over ``emit_audit`` for dossier-scoped events.

    Nearly every audit event in the platform is dossier-scoped: the
    ``target`` is the dossier, ``target_id`` equals ``dossier_id``,
    and actor identity comes straight off the ``User`` object from
    the auth middleware. Seven call sites across four route modules
    repeated the same 8-keyword ``emit_audit`` block, each with the
    same ``target_type="Dossier"``, ``target_id=str(dossier_id)``,
    ``dossier_id=str(dossier_id)``, ``actor_id=user.id``,
    ``actor_name=user.name`` boilerplate. Five of the seven fields
    on every call were identical.

    This helper collapses that boilerplate. Use it for dossier-scoped
    events:

        emit_dossier_audit(
            action="dossier.read",
            user=user,
            dossier_id=dossier_id,
            outcome="allowed",
            workflow=dossier.workflow,
        )

    Keep using the lower-level ``emit_audit`` for events where the
    target isn't a dossier (entity-scoped events, system-level
    worker events, or anything with a non-Dossier ``target_type``).

    Parameters:
    * ``user`` — any object with ``.id`` and ``.name`` string
      attributes. The auth middleware's ``User`` dataclass is the
      canonical shape, but duck-typed so test fixtures can pass in
      a simple namespace.
    * ``dossier_id`` — UUID or string. Stringified automatically —
      both ``target_id`` and the top-level ``dossier_id`` field on
      the emitted payload use the same value, which matches the
      pre-refactor behaviour across all seven call sites.
    * remaining parameters match ``emit_audit`` exactly. ``action``
      and ``outcome`` are required; ``reason`` and ``**extra`` flow
      through unchanged.
    """
    dossier_id_str = str(dossier_id)
    emit_audit(
        action=action,
        actor_id=user.id,
        actor_name=user.name,
        target_type="Dossier",
        target_id=dossier_id_str,
        outcome=outcome,
        dossier_id=dossier_id_str,
        reason=reason,
        **extra,
    )
