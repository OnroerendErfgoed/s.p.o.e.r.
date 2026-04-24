"""
End-to-end concurrency demonstration.

Fires two `bewerkAanvraag` activities against the same dossier at the
same time, each declaring `derivedFrom` pointing at the same parent
version. Without the row-level lock in `ensure_dossier`, both would
read the parent before either committed and both would succeed,
producing a branching version graph where one edit silently wins.

With the lock, one request acquires the dossier row's FOR UPDATE
lock first, commits its new version, and releases. The second
request then acquires the lock, reads fresh state (now including
the first commit), and the derivation-chain validator rejects the
now-stale `derivedFrom` with HTTP 422.

Expected output:
    Request 1: HTTP 200  (the winner, committed a new version)
    Request 2: HTTP 422  (rejected: stale derivation chain)

Usage:
    python concurrency_test.py
    # Requires the engine running at localhost:8000
    # Requires a dossier D5 with an aanvraag entity in place
    # (run test_requests.sh first to set it up).
"""
import asyncio
import sys
import time

import aiohttp

BASE = "http://localhost:8000"
DOSSIER = "d5000000-0000-0000-0000-000000000001"
USER = "benjamma"  # behandelaar for D5


async def get_current_aanvraag(session: aiohttp.ClientSession) -> tuple[str, str]:
    """Fetch the current latest oe:aanvraag version for D5.

    Returns (entity_id, version_id).
    """
    url = f"{BASE}/dossiers/{DOSSIER}"
    async with session.get(url, headers={"X-POC-User": "claeyswo"}) as r:
        body = await r.json()
    for e in body.get("currentEntities", []):
        if e.get("type") == "oe:aanvraag":
            return e["entityId"], e["versionId"]
    raise RuntimeError("No oe:aanvraag found in D5 currentEntities")


async def submit_bewerking(
    session: aiohttp.ClientSession,
    activity_id: str,
    eid: str,
    parent_vid: str,
    new_vid: str,
    label: str,
) -> tuple[int, float, str, str]:
    """Submit a bewerkAanvraag deriving from a specific parent."""
    payload = {
        "used": [{"entity": "https://id.erfgoed.net/erfgoedobjecten/10005"}],
        "generated": [
            {
                "entity": f"oe:aanvraag/{eid}@{new_vid}",
                "derivedFrom": f"oe:aanvraag/{eid}@{parent_vid}",
                "content": {
                    "onderwerp": label,
                    "handeling": "renovatie",
                    "gemeente": "Brugge",
                    "aanvrager": {"rrn": "85010100123"},
                    "object": "https://id.erfgoed.net/erfgoedobjecten/10005",
                },
            }
        ],
    }
    url = f"{BASE}/toelatingen/dossiers/{DOSSIER}/activities/{activity_id}/oe:bewerkAanvraag"
    t0 = time.monotonic()
    async with session.put(url, json=payload, headers={"X-POC-User": USER}) as r:
        elapsed = time.monotonic() - t0
        body = await r.text()
        return r.status, elapsed, label, body


async def main() -> int:
    async with aiohttp.ClientSession() as session:
        # Discover the current latest aanvraag version.
        try:
            eid, parent_vid = await get_current_aanvraag(session)
        except Exception as exc:
            print(f"FAIL: could not read current aanvraag: {exc}")
            print("      Run test_requests.sh first to set up dossier D5.")
            return 1

        print(f"Current D5 aanvraag: {eid}@{parent_vid}")
        print("Firing two concurrent bewerkAanvraag requests, "
              "both deriving from the same parent...")
        print()

        # Fire both at once.
        t0 = time.monotonic()
        r1, r2 = await asyncio.gather(
            submit_bewerking(
                session,
                "aa0c0001-0000-0000-0000-000000000001",
                eid,
                parent_vid,
                "aa0c0001-0000-4000-8000-000000000001",
                "CONCURRENT EDIT A",
            ),
            submit_bewerking(
                session,
                "aa0c0002-0000-0000-0000-000000000001",
                eid,
                parent_vid,
                "aa0c0002-0000-4000-8000-000000000001",
                "CONCURRENT EDIT B",
            ),
        )
        total = time.monotonic() - t0

    statuses = []
    for status, elapsed, label, body in (r1, r2):
        print(f"{label}: HTTP {status}, {elapsed * 1000:.0f}ms")
        # Show a short reason excerpt
        if status == 422:
            # extract the "detail" field
            import json
            try:
                d = json.loads(body)
                print(f"  -> {str(d.get('detail', d))[:200]}")
            except Exception:
                print(f"  -> {body[:200]}")
        statuses.append(status)
    print(f"\nTotal wall time: {total * 1000:.0f}ms")

    # Contract: exactly one must win (200), the other must lose (422).
    if sorted(statuses) == [200, 422]:
        print("\nPASS: one request committed, the other was rejected")
        print("      due to a stale derivation chain. Row-level locking")
        print("      on the dossier row serialized the two writes and")
        print("      the derivation validator caught the conflict.")
        return 0

    print(f"\nFAIL: expected [200, 422], got {sorted(statuses)}")
    print("      Without locking, both requests see the same parent")
    print("      version and both succeed, producing a branching graph.")
    print("      Investigate: is ensure_dossier using get_dossier_for_update?")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
