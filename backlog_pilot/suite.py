"""Mission Control suite: the connection between Backlog Pilot and Leadership Pilot.

Leadership Pilot sends ranked problems here (the intake inbox) and proposes personas to the
persona library. Nothing it sends acts on its own: a product owner drafts from an inbox item,
and an approver accepts or rejects each proposed persona.

The API is server-to-server and authenticated with a shared bearer token (BP_SUITE_TOKEN);
it is switched off when no token is set. In production, replace the shared token with
Microsoft Entra ID app-to-app tokens.
"""

import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backlog_pilot.db import audit, utcnow

NEW, DRAFTED, DISMISSED = "new", "drafted", "dismissed"


@dataclass(frozen=True)
class SuiteSettings:
    token: str = ""  # shared secret for the suite API; empty disables it
    leadership_url: str = ""  # where Leadership Pilot runs, for the app switcher and links

    @property
    def api_enabled(self) -> bool:
        return bool(self.token)


def load_suite_settings() -> SuiteSettings:
    return SuiteSettings(os.getenv("BP_SUITE_TOKEN", ""),
                         os.getenv("BP_LEADERSHIP_URL", "").rstrip("/"))


def authorized(settings: SuiteSettings, header: str) -> bool:
    if not settings.api_enabled or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header.removeprefix("Bearer ").encode(), settings.token.encode())


class InboxItem(SQLModel, table=True):
    """A problem sent from another Mission Control app, waiting for a product owner to draft
    from it."""
    id: int | None = Field(default=None, primary_key=True)
    title: str
    text: str
    source_app: str = "Leadership Pilot"
    source_ref: str = ""  # e.g. the problem's key in Leadership Pilot
    source_url: str = ""
    stakeholders: str = "[]"  # JSON list of names who raised it
    submitted_by: str = ""
    status: str = NEW
    intake_id: int | None = Field(default=None, foreign_key="intake.id")
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def stakeholder_list(self) -> list[str]:
        return json.loads(self.stakeholders)


class SuiteStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def receive(self, title: str, text: str, source_ref: str, source_url: str,
                stakeholders: list[str], submitted_by: str,
                source_app: str = "Leadership Pilot") -> InboxItem:
        if not title.strip() or len(text.strip()) < 40:
            raise ValueError("A problem needs a title and at least a few sentences of text.")
        with Session(self.engine) as s:
            item = InboxItem(title=title.strip()[:200], text=text.strip(),
                             source_ref=source_ref[:50], source_url=source_url[:500],
                             stakeholders=json.dumps(stakeholders[:50]),
                             submitted_by=submitted_by[:200], source_app=source_app[:60])
            s.add(item)
            s.flush()
            audit(s, f"{submitted_by} via {item.source_app}", "inbox_received",
                  f"#{item.id} {source_ref}: {item.title}")
            s.commit()
            s.refresh(item)
            return item

    def inbox(self, status: str = NEW) -> list[InboxItem]:
        with Session(self.engine) as s:
            return list(s.exec(select(InboxItem).where(InboxItem.status == status)
                               .order_by(InboxItem.id.desc())))

    def get(self, item_id: int) -> InboxItem | None:
        with Session(self.engine) as s:
            return s.get(InboxItem, item_id)

    def for_intake(self, intake_id: int) -> InboxItem | None:
        with Session(self.engine) as s:
            return s.exec(select(InboxItem).where(InboxItem.intake_id == intake_id)).first()

    def mark_drafted(self, s: Session, item_id: int, intake_id: int, actor: str) -> None:
        """Link an inbox item to the intake approved from it (caller commits)."""
        item = s.get(InboxItem, item_id)
        if item is None:
            return
        item.status, item.intake_id = DRAFTED, intake_id
        s.add(item)
        audit(s, actor, "inbox_drafted", f"#{item.id} {item.source_ref} -> intake {intake_id}")

    def dismiss(self, item_id: int, actor: str, reason: str = "") -> None:
        with Session(self.engine) as s:
            item = s.get(InboxItem, item_id)
            if item is None:
                return
            item.status = DISMISSED
            s.add(item)
            audit(s, actor, "inbox_dismissed", f"#{item.id} {item.source_ref}"
                  + (f" | reason: {reason.strip()}" if reason.strip() else ""))
            s.commit()

    def counts(self) -> dict[str, int]:
        with Session(self.engine) as s:
            rows = s.exec(select(InboxItem.status, func.count()).group_by(InboxItem.status))
            return dict(rows.all())
