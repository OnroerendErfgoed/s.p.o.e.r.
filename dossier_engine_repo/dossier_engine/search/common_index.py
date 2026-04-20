"""
Common dossier index — one doc per dossier, shared across workflows.

Fields:
    dossier_id   (keyword)  — UUID string
    workflow     (keyword)  — exact match on workflow name
    onderwerp    (text)     — fuzzy search (pulled from oe:aanvraag or
                              equivalent; plugins decide what maps here)
    __acl__      (keyword)  — flat list of role names + agent UUIDs

Operations:

* ``recreate_index`` — drop and recreate with mapping. Destructive.
* ``reindex_all`` — iterate every dossier in Postgres, build a common
  doc via its plugin, bulk-index. Use after recreate or after a
  mapping change.
* ``get_common_doc`` — build the per-dossier doc. Called from each
  plugin's post_activity_hook.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from . import build_acl, get_client, get_global_access


logger = logging.getLogger(__name__)


INDEX_NAME = "dossiers-common"


MAPPING = {
    "mappings": {
        "properties": {
            "dossier_id": {"type": "keyword"},
            "workflow": {"type": "keyword"},
            "onderwerp": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "__acl__": {"type": "keyword"},
        }
    }
}


def build_common_doc(
    dossier_id: UUID,
    workflow: str,
    onderwerp: str | None,
    access_entity_content: dict | None,
) -> dict:
    """Build the per-dossier common-index document.

    Pulls ``global_access`` from the module-level registration so
    global roles land in ``__acl__`` alongside per-dossier roles.
    Callers don't have to plumb it through.
    """
    return {
        "dossier_id": str(dossier_id),
        "workflow": workflow,
        "onderwerp": onderwerp or "",
        "__acl__": build_acl(access_entity_content, get_global_access()),
    }


async def index_one(doc: dict) -> None:
    """Index or update a single common doc. No-op if ES is not
    configured."""
    client = get_client()
    if client is None:
        logger.debug(
            "[search] index_one(common) skipped — ES not configured. "
            "doc=%s", doc,
        )
        return
    await client.index(
        index=INDEX_NAME,
        id=doc["dossier_id"],
        document=doc,
    )


async def recreate_index() -> dict:
    """Drop and recreate the common index. Destructive.

    Returns a summary dict for the admin endpoint response.
    """
    client = get_client()
    if client is None:
        return {
            "index": INDEX_NAME,
            "recreated": False,
            "reason": "ES not configured (DOSSIER_ES_URL is empty)",
        }

    existed = await client.indices.exists(index=INDEX_NAME)
    if existed:
        await client.indices.delete(index=INDEX_NAME)
    await client.indices.create(index=INDEX_NAME, body=MAPPING)
    return {
        "index": INDEX_NAME,
        "recreated": True,
        "previously_existed": bool(existed),
    }


async def reindex_all(repo, registry) -> dict:
    """Re-index every dossier in Postgres into the common index.

    Walks all dossiers, resolves each one's plugin, and asks the
    plugin for the latest common doc via ``plugin.build_common_doc``
    if available — otherwise falls back to a minimal doc (no
    onderwerp, no ACL). Plugins that want full coverage must
    implement the optional method.

    Returns a summary with indexed / skipped counts.
    """
    client = get_client()
    if client is None:
        return {
            "index": INDEX_NAME,
            "reindexed": 0,
            "skipped": 0,
            "reason": "ES not configured",
        }

    from sqlalchemy import select
    from ..db.models import DossierRow

    result = await repo.session.execute(select(DossierRow))
    dossiers = list(result.scalars().all())

    indexed = 0
    skipped = 0
    for dossier in dossiers:
        plugin = registry.get(dossier.workflow)
        # Prefer the plugin-supplied builder so docs carry real
        # per-dossier data (onderwerp, access). Falls back to a
        # minimal doc only if the plugin didn't register one — this
        # keeps the engine-level reindex working for plugins that
        # haven't opted in yet, at the cost of an ACL-less doc.
        builder = getattr(plugin, "build_common_doc_for_dossier", None) if plugin else None
        if builder is not None:
            doc = await builder(repo, dossier.id)
        else:
            # Fallback: minimal doc. Will still be filterable by
            # workflow but has no onderwerp and no per-dossier ACL
            # (only global-access roles end up in __acl__).
            doc = build_common_doc(dossier.id, dossier.workflow, None, None)
        if doc is None:
            skipped += 1
            continue
        await client.index(index=INDEX_NAME, id=str(dossier.id), document=doc)
        indexed += 1

    await client.indices.refresh(index=INDEX_NAME)

    return {
        "index": INDEX_NAME,
        "reindexed": indexed,
        "skipped": skipped,
    }


async def search_common(
    *, user, workflow: str | None = None, onderwerp: str | None = None,
    limit: int = 50,
) -> dict:
    """Search the common index. Always applies ACL filter."""
    client = get_client()
    if client is None:
        return {
            "hits": [],
            "total": 0,
            "reason": "ES not configured (DOSSIER_ES_URL is empty)",
        }

    from . import build_acl_filter
    must: list[dict] = []
    if onderwerp:
        # Fuzzy on onderwerp — edit-distance tolerant.
        must.append({
            "match": {"onderwerp": {"query": onderwerp, "fuzziness": "AUTO"}}
        })
    filter_clauses: list[dict] = [build_acl_filter(user)]
    if workflow:
        # Exact match on workflow name.
        filter_clauses.append({"term": {"workflow": workflow}})

    body = {
        "query": {
            "bool": {
                "must": must or [{"match_all": {}}],
                "filter": filter_clauses,
            }
        },
        "size": limit,
    }

    result = await client.search(index=INDEX_NAME, body=body)
    hits = result.get("hits", {}).get("hits", [])
    total = result.get("hits", {}).get("total", {}).get("value", 0)
    return {
        "hits": [hit["_source"] for hit in hits],
        "total": total,
    }
