"""
POC authentication middleware.

Simulates auth by looking up X-POC-User header against config.
In production, replace with JWT/OAuth middleware.
"""

from __future__ import annotations

from dataclasses import dataclass
from fastapi import Request, HTTPException


@dataclass
class User:
    id: str
    type: str
    name: str
    roles: list[str]
    properties: dict[str, str]
    uri: str | None = None  # canonical external IRI for this agent


# Canonical system-user singleton. Used by the engine's side-effect
# executor, the worker's task runner, and the app's bootstrap path to
# attribute system-initiated work. Re-exported by ``dossier_engine.app``
# as ``SYSTEM_USER`` for back-compat with callers that imported it
# there before it moved to this module (still safe — ``app.SYSTEM_USER``
# is the same object identity).
SYSTEM_USER = User(
    id="system",
    type="systeem",
    name="Systeem",
    roles=["systeemgebruiker"],
    properties={},
    uri="https://id.erfgoed.net/agenten/system",
)


class POCAuthMiddleware:
    """Simulates auth by looking up X-POC-User header against config."""

    def __init__(self, users_config: list[dict]):
        self._users: dict[str, User] = {}
        for u in users_config:
            self._users[u["username"]] = User(
                id=str(u["id"]),
                type=u["type"],
                name=u["name"],
                roles=u.get("roles", []),
                properties=u.get("properties", {}),
                uri=u.get("uri"),
            )

    async def __call__(self, request: Request) -> User:
        username = request.headers.get("X-POC-User")
        if not username:
            raise HTTPException(401, detail="X-POC-User header required")
        user = self._users.get(username)
        if not user:
            raise HTTPException(401, detail=f"Unknown POC user: {username}")
        return user
