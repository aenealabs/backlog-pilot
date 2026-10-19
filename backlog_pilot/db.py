"""Database engine and the shared audit log."""

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

# Bump when table shapes change. The local SQLite file holds demo data only, so an older
# file is rebuilt (and re-seeded) rather than migrated.
SCHEMA_VERSION = 6

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


class AuditEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow)
    actor: str
    action: str
    detail: str


def make_engine(db_path: str) -> Engine:
    # Import table modules so their models are registered before create_all.
    import backlog_pilot.intakes  # noqa: F401
    import backlog_pilot.personas  # noqa: F401
    import backlog_pilot.roadmap  # noqa: F401
    import backlog_pilot.suite  # noqa: F401
    import backlog_pilot.trackers.mock  # noqa: F401

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        version = conn.execute(text("PRAGMA user_version")).scalar()
        has_tables = conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE type='table'")).scalar()
        if has_tables and version != SCHEMA_VERSION:
            log.warning("Rebuilding demo database %s (schema %s -> %s)",
                        db_path, version, SCHEMA_VERSION)
            SQLModel.metadata.drop_all(conn)
            for (name,) in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")).all():
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        SQLModel.metadata.create_all(conn)
        from backlog_pilot.audit_search import install_fts

        install_fts(conn)
        conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
    return engine


def audit(s: Session, actor: str, action: str, detail: str) -> None:
    s.add(AuditEvent(actor=actor, action=action, detail=detail))


def audit_events(engine: Engine, limit: int = 100) -> list[AuditEvent]:
    with Session(engine) as s:
        return list(s.exec(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)))
