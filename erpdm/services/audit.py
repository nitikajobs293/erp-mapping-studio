"""Audit trail and metadata version snapshots."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from erpdm.db.models import AuditLog, MetadataVersion
from erpdm.logging_setup import get_logger

log = get_logger(__name__)


def audit(session: Session, username: str | None, action: str, entity_type: str | None = None,
          entity_id: Any = None, details: dict | None = None) -> None:
    session.add(AuditLog(username=username, action=action, entity_type=entity_type,
                         entity_id=None if entity_id is None else str(entity_id), details=details))
    log.info("audit action=%s entity=%s:%s user=%s", action, entity_type, entity_id, username)


def snapshot(obj: Any) -> dict:
    """Serialisable snapshot of an ORM row's column attributes."""
    out = {}
    for attr in inspect(obj).mapper.column_attrs:
        val = getattr(obj, attr.key)
        out[attr.key] = val.isoformat() if hasattr(val, "isoformat") else val
    return out


def record_version(session: Session, entity_type: str, obj: Any, change_type: str,
                   username: str | None) -> None:
    session.flush()
    current = session.scalar(
        select(func.max(MetadataVersion.version)).where(
            MetadataVersion.entity_type == entity_type, MetadataVersion.entity_id == obj.id)
    ) or 0
    session.add(MetadataVersion(entity_type=entity_type, entity_id=obj.id, version=current + 1,
                                change_type=change_type, snapshot=snapshot(obj), changed_by=username))
    if hasattr(obj, "version"):
        obj.version = current + 1


def recent_audit(session: Session, limit: int = 200, username: str | None = None) -> list[AuditLog]:
    q = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
    if username:
        q = q.where(AuditLog.username == username)
    return list(session.scalars(q))


def versions_for(session: Session, entity_type: str, entity_id: int) -> list[MetadataVersion]:
    return list(session.scalars(
        select(MetadataVersion).where(MetadataVersion.entity_type == entity_type,
                                      MetadataVersion.entity_id == entity_id)
        .order_by(MetadataVersion.version.desc())))
