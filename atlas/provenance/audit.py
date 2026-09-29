"""Append-only audit trail (the table rejects UPDATE/DELETE at the database level)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from atlas.db.models import AuditLog


def audit(session: Session, action: str, object_type: str, object_id: uuid.UUID | None,
          run_id: uuid.UUID | None = None, details: dict | None = None, actor: str = "pipeline") -> None:
    session.add(AuditLog(actor=actor, action=action, object_type=object_type, object_id=object_id,
                         ingestion_run_id=run_id, details=details or {}))
