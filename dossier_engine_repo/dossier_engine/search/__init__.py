"""
Elasticsearch integration — client, settings, ACL.

Search-related plumbing lives here so both the engine (common index)
and plugins (workflow-specific indices) share the same connection
and ACL conventions.

Three concerns:

1. **Connection config** — URL and API key from env vars. Secrets via
   env only; committed config is fine for the URL.
2. **The common index** — one doc per dossier, fields: dossier_id,
   workflow, onderwerp, __acl__. Populated after each activity by
   any plugin's post_activity_hook (via ``get_common_doc``), and
   re-createable / re-indexable via engine-level admin endpoints.
3. **ACL filtering** — ``build_acl_filter(user)`` returns an ES
   query fragment every search must AND into its query. The filter
   checks ``__acl__`` against ``user.roles ∪ {user.id}``.

All index operations are no-ops when ``settings.es_url`` is empty —
the POC runs without ES, tests exercise the logic without a server,
and real deployments set the env vars to connect.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


# ---------- Settings ----------

class SearchSettings(BaseSettings):
    """Elasticsearch connection config.

    Both values come from env vars by default (prefix ``DOSSIER_ES_``)
    so the API key never has to touch committed YAML. If ``es_url``
    is empty, every index operation is a silent no-op — this is the
    expected state for development and test runs.
    """

    model_config = SettingsConfigDict(
        env_prefix="DOSSIER_ES_",
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )

    url: str = ""
    """Base URL for the ES cluster. Empty = no-op mode (POC/tests)."""

    api_key: str = ""
    """Encoded API key for Authorization: ApiKey <key>. Keep in env vars."""

    verify_certs: bool = True
    """Disable only for dev clusters with self-signed certs."""


# ---------- Client ----------

_client: Any = None
"""Lazily created AsyncElasticsearch. Module-global because the
client pools connections and we want one per process."""


_global_access: list[dict] | None = None
"""Global access entries from config.yaml. Populated at app startup
by ``configure_global_access``. Used at indexing time to include
global roles (e.g. ``beheerder``, ``systeemgebruiker``) in the
per-doc ``__acl__`` list — without this, global-access users
search and find nothing even though they can see dossiers via
``GET /dossiers/{id}``."""


_global_admin_access: list[str] | None = None
"""Role names allowed to run destructive/bulk admin operations on
search indices (recreate, reindex). Populated at app startup by
``configure_global_admin_access``. Separate tier from audit."""


def configure_global_access(entries: list[dict] | None) -> None:
    """Store the global_access entries for later use by indexers.
    Call once at app startup after reading config.yaml."""
    global _global_access
    _global_access = entries or []


def get_global_access() -> list[dict]:
    """Return the configured global_access entries (empty list if
    unset)."""
    return _global_access or []


def configure_global_admin_access(roles: list[str] | None) -> None:
    """Store the global_admin_access role list. Used by the engine's
    and plugins' admin endpoints to gate destructive operations."""
    global _global_admin_access
    _global_admin_access = roles or []


def get_global_admin_access() -> list[str]:
    """Return the configured global_admin_access roles (empty list
    if unset)."""
    return _global_admin_access or []


def get_client() -> Any | None:
    """Return a configured AsyncElasticsearch client, or None if ES
    is not configured (``es_url`` is empty)."""
    global _client
    settings = SearchSettings()
    if not settings.url:
        return None
    if _client is None:
        try:
            from elasticsearch import AsyncElasticsearch
        except ImportError:
            logger.warning(
                "elasticsearch package not installed — index operations "
                "will be skipped. Install with: pip install elasticsearch"
            )
            return None
        _client = AsyncElasticsearch(
            hosts=[settings.url],
            api_key=settings.api_key or None,
            verify_certs=settings.verify_certs,
        )
    return _client


async def close_client() -> None:
    """Close the shared client. Call at app shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


# ---------- ACL ----------

def build_acl(
    access_entity_content: dict | None,
    global_access: list[dict] | None = None,
) -> list[str]:
    """Flatten every source of access for a dossier into a single flat
    ACL list of role names + agent UUIDs.

    Three sources contribute:

    1. **Per-dossier roles and agents** from the ``oe:dossier_access``
       entity's ``access`` list — business-level grants managed by a
       handler.
    2. **Per-dossier audit roles** from the same entity's
       ``audit_access`` list — auditors for this specific dossier.
    3. **Global roles** from ``config.yaml``'s ``global_access`` list —
       roles that see every dossier regardless of per-dossier config
       (e.g. ``beheerder``, ``systeemgebruiker``). Without these in
       ``__acl__``, a global-access user would search, find nothing,
       and wonder why — even though ``GET /dossiers/{id}`` would
       have returned the data.

    The result is deduplicated but order-stable so doc diffs in
    Elasticsearch stay readable.
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)

    # 1 + 2: per-dossier sources
    if access_entity_content:
        for entry in access_entity_content.get("access", []):
            role = entry.get("role")
            if role:
                _add(role)
            for agent in entry.get("agents", []):
                _add(agent)
        for role in access_entity_content.get("audit_access", []):
            _add(role)

    # 3: global roles
    if global_access:
        for entry in global_access:
            role = entry.get("role")
            if role:
                _add(role)

    return tokens


def build_acl_filter(user) -> dict:
    """ES query fragment that restricts hits to docs whose ``__acl__``
    list intersects with ``user.roles ∪ {user.id}``.

    AND this into every search query. Callers should NOT skip this
    filter even for admin roles — if an admin role is in the index's
    __acl__, they'll match naturally; if not, they shouldn't see
    the doc.
    """
    tokens = list(user.roles) + [user.id]
    return {"terms": {"__acl__": tokens}}
