"""
Toelatingen-specific search index.

One doc per toelatingen dossier, fields:

    dossier_id   (keyword)  — UUID string
    onderwerp    (text)     — fuzzy on oe:aanvraag.content.onderwerp
    gemeente     (keyword)  — exact on oe:aanvraag.content.gemeente
    beslissing   (keyword)  — exact on oe:beslissing.content.beslissing
    __acl__      (keyword)  — flat list of role names + agent UUIDs

Every document is written after each activity completes via the
plugin's post_activity_hook (see ``indexing.py``). This module owns
the index mapping, doc builder, recreate/reindex helpers, and the
search function.
"""

from __future__ import annotations

import logging
from uuid import UUID

from dossier_engine.search import build_acl, get_client, get_global_access


logger = logging.getLogger(__name__)


INDEX_NAME = "dossiers-toelatingen"


MAPPING = {
    "mappings": {
        "properties": {
            "dossier_id": {"type": "keyword"},
            "onderwerp": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "gemeente": {"type": "keyword"},
            "beslissing": {"type": "keyword"},
            "__acl__": {"type": "keyword"},
        }
    }
}


def build_toelatingen_doc(
    dossier_id: UUID,
    aanvraag_content: dict | None,
    beslissing_content: dict | None,
    access_entity_content: dict | None,
) -> dict:
    """Assemble the per-dossier toelatingen-index document.

    Includes global-access roles in ``__acl__`` so users matching a
    global role see this dossier in search results.
    """
    aanvraag_content = aanvraag_content or {}
    beslissing_content = beslissing_content or {}
    return {
        "dossier_id": str(dossier_id),
        "onderwerp": aanvraag_content.get("onderwerp") or "",
        "gemeente": aanvraag_content.get("gemeente") or "",
        "beslissing": beslissing_content.get("beslissing") or "",
        "__acl__": build_acl(access_entity_content, get_global_access()),
    }


async def index_one(doc: dict) -> None:
    """Upsert a single toelatingen doc. No-op without ES."""
    client = get_client()
    if client is None:
        logger.debug(
            "[search] index_one(toelatingen) skipped — ES not configured. "
            "doc=%s", doc,
        )
        return
    await client.index(
        index=INDEX_NAME,
        id=doc["dossier_id"],
        document=doc,
    )


async def recreate_index() -> dict:
    """Drop and recreate the toelatingen index."""
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


async def reindex_all(repo) -> dict:
    """Re-index every toelatingen dossier. Walks the Postgres state,
    pulls each dossier's latest aanvraag / beslissing / access entity,
    and upserts the resulting doc."""
    client = get_client()
    if client is None:
        return {
            "index": INDEX_NAME,
            "reindexed": 0,
            "reason": "ES not configured",
        }

    from sqlalchemy import select
    from dossier_engine.db.models import DossierRow

    result = await repo.session.execute(
        select(DossierRow).where(DossierRow.workflow == "toelatingen")
    )
    dossiers = list(result.scalars().all())

    indexed = 0
    for dossier in dossiers:
        aanvraag = await repo.get_singleton_entity(dossier.id, "oe:aanvraag")
        beslissing = await repo.get_singleton_entity(dossier.id, "oe:beslissing")
        access = await repo.get_singleton_entity(dossier.id, "oe:dossier_access")
        doc = build_toelatingen_doc(
            dossier.id,
            aanvraag.content if aanvraag else None,
            beslissing.content if beslissing else None,
            access.content if access else None,
        )
        await client.index(index=INDEX_NAME, id=str(dossier.id), document=doc)
        indexed += 1

    await client.indices.refresh(index=INDEX_NAME)
    return {"index": INDEX_NAME, "reindexed": indexed}


async def reindex_common_too(repo, registry) -> dict:
    """Reindex both the toelatingen index AND rebuild the common-index
    entries for every toelatingen dossier. Convenience wrapper — when
    the toelatingen mapping changes we often want to refresh common
    alongside because both feeds come from the same entity changes.
    """
    from dossier_engine.search.common_index import (
        build_common_doc, INDEX_NAME as COMMON_INDEX,
    )

    client = get_client()
    if client is None:
        return {
            "toelatingen": {"reindexed": 0, "reason": "ES not configured"},
            "common": {"reindexed": 0, "reason": "ES not configured"},
        }

    from sqlalchemy import select
    from dossier_engine.db.models import DossierRow

    result = await repo.session.execute(
        select(DossierRow).where(DossierRow.workflow == "toelatingen")
    )
    dossiers = list(result.scalars().all())

    toelatingen_count = 0
    common_count = 0
    for dossier in dossiers:
        aanvraag = await repo.get_singleton_entity(dossier.id, "oe:aanvraag")
        beslissing = await repo.get_singleton_entity(dossier.id, "oe:beslissing")
        access = await repo.get_singleton_entity(dossier.id, "oe:dossier_access")

        # Toelatingen doc
        specific = build_toelatingen_doc(
            dossier.id,
            aanvraag.content if aanvraag else None,
            beslissing.content if beslissing else None,
            access.content if access else None,
        )
        await client.index(index=INDEX_NAME, id=str(dossier.id), document=specific)
        toelatingen_count += 1

        # Common doc — uses onderwerp from aanvraag
        onderwerp = (aanvraag.content or {}).get("onderwerp") if aanvraag else None
        common = build_common_doc(
            dossier.id, dossier.workflow, onderwerp,
            access.content if access else None,
        )
        await client.index(index=COMMON_INDEX, id=str(dossier.id), document=common)
        common_count += 1

    await client.indices.refresh(index=INDEX_NAME)
    await client.indices.refresh(index=COMMON_INDEX)
    return {
        "toelatingen": {"index": INDEX_NAME, "reindexed": toelatingen_count},
        "common": {"index": COMMON_INDEX, "reindexed": common_count},
    }


async def search_toelatingen(
    *, user, q: str | None = None,
    gemeente: str | None = None, beslissing: str | None = None,
    limit: int = 50,
) -> dict:
    """Search the toelatingen index. ACL filter always applied.

    * ``q`` → fuzzy on onderwerp.
    * ``gemeente`` → exact filter.
    * ``beslissing`` → exact filter.
    """
    client = get_client()
    if client is None:
        return {"hits": [], "total": 0, "reason": "ES not configured"}

    from dossier_engine.search import build_acl_filter

    must: list[dict] = []
    if q:
        must.append({
            "match": {"onderwerp": {"query": q, "fuzziness": "AUTO"}}
        })

    filter_clauses: list[dict] = [build_acl_filter(user)]
    if gemeente:
        filter_clauses.append({"term": {"gemeente": gemeente}})
    if beslissing:
        filter_clauses.append({"term": {"beslissing": beslissing}})

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


async def build_common_doc_for_dossier(repo, dossier_id):
    """Build the engine-level common-index document for a toelatingen
    dossier.

    Called by ``dossier_engine.search.common_index.reindex_all`` when
    the engine walks every dossier. Without this, the engine would
    fall back to a minimal doc (no onderwerp, empty ``access`` → only
    global-access roles in ``__acl__``) — which makes every
    non-global-access user invisible from search results after a
    ``/admin/search/common/reindex`` call. Exactly the bug we hit.

    Pulls onderwerp from the latest ``oe:aanvraag`` and ACL from the
    latest ``oe:dossier_access``. Returns None only if the dossier
    has neither (which shouldn't happen for a real toelatingen
    dossier but keeps the caller's skipped-counter honest).
    """
    from dossier_engine.search.common_index import build_common_doc

    aanvraag = await repo.get_singleton_entity(dossier_id, "oe:aanvraag")
    access = await repo.get_singleton_entity(dossier_id, "oe:dossier_access")

    if aanvraag is None and access is None:
        return None

    onderwerp = (aanvraag.content or {}).get("onderwerp") if aanvraag else None
    access_content = access.content if access else None

    return build_common_doc(
        dossier_id=dossier_id,
        workflow="toelatingen",
        onderwerp=onderwerp,
        access_entity_content=access_content,
    )
