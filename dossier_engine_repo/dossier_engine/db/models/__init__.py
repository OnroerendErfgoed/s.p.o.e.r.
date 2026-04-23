"""
Database models and repository.

Layout (Round 34 split):
    db/models/
    ├── __init__.py       — re-exports Base + 8 Row classes + Repository
    ├── rows.py           — the 8 SQLAlchemy row classes + Base + type shims
    └── repository.py     — the Repository class (session-bound data access)

All activity/entity tables are append-only. Status is stored as
``computed_status`` on each activity row. Content is stored as JSONB,
validated by Pydantic on write.

Postgres 16+ required (native ``UUID`` and ``JSONB``).
"""
from .rows import (
    Base,
    UUID_DB,
    JSON_DB,
    DossierRow,
    ActivityRow,
    AssociationRow,
    EntityRow,
    UsedRow,
    RelationRow,
    AgentRow,
    DomainRelationRow,
)
from .repository import Repository

__all__ = [
    "Base",
    "UUID_DB",
    "JSON_DB",
    "DossierRow",
    "ActivityRow",
    "AssociationRow",
    "EntityRow",
    "UsedRow",
    "RelationRow",
    "AgentRow",
    "DomainRelationRow",
    "Repository",
]
