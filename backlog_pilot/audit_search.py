"""Search the audit log: exact full-text search with filters, and plain-English questions.

Layer 1 is deterministic: SQLite FTS5 over every event plus filters for person, category,
item key, and dates. It stays fast on very large logs and never invents anything.

Layer 2 ("Ask the audit log") lets the model turn a question into a layer-1 query (a plan),
runs that query, and, in live mode, has the model answer from the matching events only,
citing event numbers. Citations are checked against the events actually retrieved; anything
not backed by an event is dropped and flagged. The model never reads the whole log.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session, col, func, or_, select

from backlog_pilot.db import AuditEvent

CATEGORIES: dict[str, tuple[str, ...]] = {
    "drafts_and_approvals": ("draft_generated", "redraft_generated", "intake_created",
                             "draft_items_dropped", "draft_title_edited",
                             "approve_and_push", "supersede", "linked_related"),
    "assumptions_and_questions": ("assumption_%", "question_%", "changes_acknowledged"),
    "prioritization": ("wsjf_%",),
    "roadmap": ("capacity_changed", "roadmap_%", "product_updated"),
    "personas": ("persona_%",),
    "tracker_sync": ("synced_to_tracker", "sync_failed"),
    "access": ("signed_in", "sign_in_failed", "access_denied"),
}
CATEGORY_LABELS = {
    "drafts_and_approvals": "Drafts & approvals", "assumptions_and_questions":
    "Assumptions & questions", "prioritization": "Prioritization (WSJF)",
    "roadmap": "Roadmap & capacity", "personas": "Personas", "tracker_sync": "Tracker sync",
    "access": "Sign-in & access",
}
Category = Literal["drafts_and_approvals", "assumptions_and_questions", "prioritization",
                   "roadmap", "personas", "tracker_sync", "access"]

_ITEM_KEY = re.compile(r"\b(?:BP|IN)-\d+\b", re.IGNORECASE)


# ---------- full-text index ----------

def install_fts(conn: Connection) -> None:
    """Create the FTS5 index over audit events, keep it in sync, and backfill it."""
    conn.execute(text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS audit_fts USING fts5("
        "actor, action, detail, content='auditevent', content_rowid='id')"))
    conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS audit_fts_insert AFTER INSERT ON auditevent BEGIN "
        "INSERT INTO audit_fts(rowid, actor, action, detail) "
        "VALUES (new.id, new.actor, new.action, new.detail); END"))
    conn.execute(text("INSERT INTO audit_fts(audit_fts) VALUES ('rebuild')"))


def fts_query(words: str) -> str:
    """Turn free text into a safe FTS5 query: every word must match (prefix match)."""
    tokens = re.findall(r"[\w]+", words.lower())
    return " AND ".join(f'"{t}"*' for t in tokens)


# ---------- queries ----------

@dataclass
class AuditQuery:
    text: str = ""
    actor: str = ""
    categories: list[str] = field(default_factory=list)
    item_keys: list[str] = field(default_factory=list)
    since: date | None = None
    until: date | None = None

    def is_empty(self) -> bool:
        return not (self.text or self.actor or self.categories or self.item_keys
                    or self.since or self.until)


@dataclass
class AuditResults:
    events: list[AuditEvent]
    total: int


def search(engine: Engine, q: AuditQuery, limit: int = 100, offset: int = 0) -> AuditResults:
    stmt = select(AuditEvent)
    # Item keys are phrases in the index: "BP-112" -> "bp 112", which cannot match BP-1120.
    parts = [fts_query(q.text)] + [
        '"' + " ".join(re.findall(r"\w+", k.lower())) + '"' for k in q.item_keys]
    match = " AND ".join(p for p in parts if p)
    if match:
        ids = select(text("rowid")).select_from(text("audit_fts")).where(
            text("audit_fts MATCH :m")).params(m=match)
        stmt = stmt.where(col(AuditEvent.id).in_(ids))
    if q.actor:
        stmt = stmt.where(col(AuditEvent.actor).contains(q.actor))
    if q.categories:
        clauses = []
        for name in q.categories:
            for pattern in CATEGORIES.get(name, ()):
                clauses.append(col(AuditEvent.action).like(pattern) if "%" in pattern
                               else AuditEvent.action == pattern)
        if clauses:
            stmt = stmt.where(or_(*clauses))
    if q.since:  # dates are whole UTC days
        stmt = stmt.where(AuditEvent.at >= datetime.combine(q.since, time(), tzinfo=UTC))
    if q.until:
        stmt = stmt.where(AuditEvent.at < datetime.combine(q.until + timedelta(days=1), time(),
                                                           tzinfo=UTC))
    with Session(engine) as s:
        total = s.exec(select(func.count()).select_from(stmt.subquery())).one()
        events = s.exec(stmt.order_by(col(AuditEvent.id).desc()).offset(offset).limit(limit))
        return AuditResults(list(events), total)


def actors(engine: Engine) -> list[str]:
    with Session(engine) as s:
        return sorted(s.exec(select(AuditEvent.actor).distinct()).all())


def summarize(events: list[AuditEvent]) -> dict:
    """Deterministic overview of a result set: who, what, when, which items."""
    if not events:
        return {}
    by_category = Counter()
    for e in events:
        for name, patterns in CATEGORIES.items():
            if any(re.fullmatch(p.replace("%", ".*"), e.action) for p in patterns):
                by_category[CATEGORY_LABELS[name]] += 1
                break
    items = Counter(k.upper() for e in events for k in _ITEM_KEY.findall(e.detail))
    return {"count": len(events), "people": Counter(e.actor for e in events).most_common(5),
            "categories": by_category.most_common(), "items": items.most_common(8),
            "first": min(e.at for e in events), "last": max(e.at for e in events)}


# ---------- plain-English questions ----------

class AuditPlan(BaseModel):
    """How to search the log for a question; produced by the model, run by code."""
    keywords: list[str] = Field(description="Words that must appear in matching events, e.g. "
                                "a feature name or a reason. Empty when filters are enough.")
    actor: str = Field(description="Part of a person's name or role to filter on, or empty.")
    categories: list[Category] = Field(description="Event categories the question is about; "
                                       "empty means all.")
    item_keys: list[str] = Field(description="Backlog or intake keys named in the question, "
                                 "like BP-112 or IN-2.")
    since: date | None = Field(description="Earliest date (inclusive), resolved from phrases "
                               "like 'last week' against today's date; null if unbounded.")
    until: date | None = Field(description="Latest date (inclusive), or null.")
    explanation: str = Field(description="One sentence saying what will be searched.")

    def to_query(self) -> AuditQuery:
        return AuditQuery(" ".join(self.keywords), self.actor, list(self.categories),
                          [k.upper() for k in self.item_keys], self.since, self.until)


class AuditAnswer(BaseModel):
    answer: str = Field(description="A short answer drawn only from the events provided. "
                        "Cite every fact with its event number in square brackets, like [#42].")
    cited_event_ids: list[int] = Field(description="Every event number cited in the answer.")


_CITATION = re.compile(r"\[#(\d+)\]")


def verify_answer(answer: AuditAnswer, events: list[AuditEvent]) -> tuple[str, list[int],
                                                                          list[int]]:
    """Keep only citations to events that were actually retrieved.

    Returns the answer text with unsupported citations marked, the verified ids, and the
    ids the model cited that do not exist in the results.
    """
    retrieved = {e.id for e in events}
    cited = set(answer.cited_event_ids) | {int(n) for n in _CITATION.findall(answer.answer)}
    bad = sorted(cited - retrieved)

    def mark(m: re.Match) -> str:
        return m.group(0) if int(m.group(1)) in retrieved else "[unverified]"

    return _CITATION.sub(mark, answer.answer), sorted(cited & retrieved), bad
