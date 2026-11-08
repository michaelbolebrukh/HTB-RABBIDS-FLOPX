"""Dialect-portable column types.

These let the same ORM models run on PostgreSQL (the production / serious-project
backend) and on SQLite (used for lightweight, low-traffic projects hosted on the
shared gateway instance) without changing any application behaviour.

Why this is safe:
- The application reads and writes these columns as whole JSON values (a dict or
  list goes in, the same structure comes back). It never uses JSONB-only SQL
  operators (``@>``, ``->>``, ``.astext`` etc.), so the physical storage type is
  irrelevant to application logic.
- SQLAlchemy serialises Python -> JSON on write and deserialises JSON -> Python on
  read identically for both ``JSONB`` and the generic ``JSON`` type. JSONB's only
  observable differences (key-order/whitespace normalisation, duplicate-key
  collapsing) do not affect code that round-trips whole values, so behaviour is
  the same on every backend.

On PostgreSQL ``JSONColumn`` still emits ``JSONB`` (identical to the previous
schema), so existing deployments and migrations are unaffected.
"""

from sqlalchemy import JSON, Uuid
from sqlalchemy.dialects.postgresql import JSONB

# JSONB on PostgreSQL, generic JSON (stored as TEXT) on every other backend:
JSONColumn = JSON().with_variant(JSONB(), "postgresql")

# Re-export SQLAlchemy's dialect-agnostic UUID type so models import portable
# types from one place. Renders UUID on PostgreSQL, CHAR(32) on SQLite.
__all__ = ["JSONColumn", "Uuid"]
