"""Intakes: the stakeholder request behind a set of backlog items, plus its assumptions and
open questions ("notes"), each with a full version history.

Every note change is written as a new NoteVersion row; the Note row only caches the latest
state. Changes that could invalidate the drafted stories are flagged `material`, and an
intake lists its unreviewed material changes until someone re-drafts or acknowledges them.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backlog_pilot.db import audit, utcnow

ASSUMPTION = "assumption"
QUESTION = "question"
KINDS = (ASSUMPTION, QUESTION)

ACTIVE, OPEN, ANSWERED, REMOVED = "active", "open", "answered", "removed"


class Intake(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    summary: str
    source_text: str
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    draft_count: int = 1
    # Highest NoteVersion.id already reflected in the backlog (by a draft or an acknowledgement).
    reviewed_through: int = 0


class Note(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    intake_id: int = Field(foreign_key="intake.id", index=True)
    kind: str  # assumption | question
    text: str
    answer: str = ""
    status: str  # assumption: active|removed; question: open|answered|removed
    version: int = 1
    updated_by: str
    updated_at: datetime = Field(default_factory=utcnow)


class NoteVersion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    note_id: int = Field(foreign_key="note.id", index=True)
    version: int
    text: str
    answer: str
    status: str
    change: str  # created | edited | answered | removed | restored
    reason: str = ""
    material: bool = False
    changed_by: str
    changed_at: datetime = Field(default_factory=utcnow)


class NoteError(Exception):
    """A note change was rejected; the message is user-facing."""


def is_material(kind: str, change: str, by_ai: bool, answer_changed: bool = False) -> bool:
    """Could this change make the drafted stories wrong?

    Assumptions underpin the stories, so any human change to one is material. A question
    only matters once it is answered; adding, rewording, or removing it is just captured.
    """
    if by_ai:
        return False
    if kind == ASSUMPTION:
        return True
    return answer_changed


@dataclass
class ReviewedNotes:
    """One kind of note as it left the review screen.

    `originals` are the AI's suggestions; `kept` maps an original's index to its final text
    (originals missing from `kept` were removed); `added` are new notes typed by the reviewer.
    """
    kind: str
    originals: list[str]
    kept: dict[int, str] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)


@dataclass
class IntakeView:
    intake: Intake
    notes: dict[str, list[Note]]
    removed: dict[str, list[Note]]
    pending: list[tuple[NoteVersion, Note]]


class IntakeStore:
    key_prefix = "IN"

    def __init__(self, engine: Engine):
        self.engine = engine

    # ---------- writes that are part of approving a draft (caller commits) ----------

    def create(self, s: Session, source_text: str, summary: str, actor: str) -> Intake:
        count = s.exec(select(func.count()).select_from(Intake)).one()
        intake = Intake(key=f"{self.key_prefix}-{count + 1}", summary=summary,
                        source_text=source_text, created_by=actor)
        s.add(intake)
        s.flush()
        audit(s, actor, "intake_created", f"{intake.key}: {summary}")
        return intake

    def add_reviewed(self, s: Session, intake: Intake, reviewed: ReviewedNotes, actor: str,
                     ai_label: str, drop_reason: str = "") -> None:
        """Store the AI's suggestions as version 1 and the reviewer's edits on top of them.

        On a re-draft, suggestions that repeat an existing live note are skipped.
        """
        existing = {n.text.strip().lower() for n in self._notes(s, intake.id)
                    if n.kind == reviewed.kind and n.status != REMOVED}
        for i, original in enumerate(reviewed.originals):
            if original.strip().lower() in existing:
                continue
            note = self._create(s, intake.id, reviewed.kind, original, ai_label, by_ai=True)
            if i not in reviewed.kept:
                self._change(s, note, actor, "removed", status=REMOVED,
                             reason=drop_reason or "Removed during draft review")
            elif reviewed.kept[i].strip() != original.strip():
                self._change(s, note, actor, "edited", text=reviewed.kept[i].strip(),
                             reason="Edited during draft review")
        for text in reviewed.added:
            if text.strip():
                self._create(s, intake.id, reviewed.kind, text.strip(), actor, by_ai=False)

    def mark_redrafted(self, s: Session, intake: Intake) -> None:
        """Everything recorded so far is now reflected in a fresh draft."""
        intake.draft_count += 1
        intake.reviewed_through = self._max_version_id(s)
        s.add(intake)

    # ---------- edits after approval ----------

    def add_note(self, intake_id: int, kind: str, text: str, actor: str) -> Note:
        if kind not in KINDS:
            raise NoteError(f"Unknown note type: {kind}")
        if not text.strip():
            raise NoteError("Enter some text first.")
        with Session(self.engine) as s:
            intake = self._intake(s, intake_id)
            note = self._create(s, intake.id, kind, text.strip(), actor, by_ai=False)
            s.commit()
            s.refresh(note)
            return note

    def edit_note(self, note_id: int, actor: str, text: str | None = None,
                  answer: str | None = None, reason: str = "") -> Note:
        with Session(self.engine) as s:
            note = self._note(s, note_id)
            if note.status == REMOVED:
                raise NoteError("Restore this item before editing it.")
            new_text = note.text if text is None else text.strip()
            if not new_text:
                raise NoteError("The text can't be empty. Remove the item instead.")
            new_answer = note.answer if answer is None else answer.strip()
            if note.kind == ASSUMPTION:
                new_answer = ""
            if new_text == note.text and new_answer == note.answer:
                return note
            answer_changed = new_answer != note.answer
            if note.kind == QUESTION and answer_changed:
                change = "answered"
                status = ANSWERED if new_answer else OPEN
            else:
                change, status = "edited", note.status
            self._change(s, note, actor, change, text=new_text, answer=new_answer,
                         status=status, reason=reason, answer_changed=answer_changed)
            s.commit()
            s.refresh(note)
            return note

    def remove_note(self, note_id: int, actor: str, reason: str = "") -> Note:
        with Session(self.engine) as s:
            note = self._note(s, note_id)
            if note.status != REMOVED:
                self._change(s, note, actor, "removed", status=REMOVED, reason=reason)
                s.commit()
                s.refresh(note)
            return note

    def restore_note(self, note_id: int, actor: str, reason: str = "") -> Note:
        with Session(self.engine) as s:
            note = self._note(s, note_id)
            if note.status == REMOVED:
                status = ACTIVE if note.kind == ASSUMPTION else (ANSWERED if note.answer else OPEN)
                self._change(s, note, actor, "restored", status=status, reason=reason)
                s.commit()
                s.refresh(note)
            return note

    def acknowledge(self, intake_id: int, actor: str, reason: str = "") -> None:
        """Record a decision that the pending changes need no backlog change."""
        with Session(self.engine) as s:
            intake = self._intake(s, intake_id)
            pending = len(self._pending(s, intake))
            intake.reviewed_through = self._max_version_id(s)
            s.add(intake)
            detail = f"{intake.key}: {pending} change(s) reviewed, no backlog change needed"
            audit(s, actor, "changes_acknowledged", detail + (f" ({reason})" if reason else ""))
            s.commit()

    # ---------- reads ----------

    def view(self, intake_id: int) -> IntakeView:
        with Session(self.engine) as s:
            intake = self._intake(s, intake_id)
            notes = self._notes(s, intake.id)
            return IntakeView(
                intake=intake,
                notes={k: [n for n in notes if n.kind == k and n.status != REMOVED]
                       for k in KINDS},
                removed={k: [n for n in notes if n.kind == k and n.status == REMOVED]
                         for k in KINDS},
                pending=self._pending(s, intake),
            )

    def history(self, note_id: int) -> tuple[Note, list[NoteVersion]]:
        with Session(self.engine) as s:
            note = self._note(s, note_id)
            versions = list(s.exec(select(NoteVersion).where(NoteVersion.note_id == note_id)
                                   .order_by(NoteVersion.version.desc())))
            return note, versions

    def all_intakes(self) -> list[dict]:
        with Session(self.engine) as s:
            intakes = s.exec(select(Intake).order_by(Intake.id.desc())).all()
            rows = []
            for intake in intakes:
                notes = self._notes(s, intake.id)
                rows.append({
                    "intake": intake,
                    "assumptions": sum(n.kind == ASSUMPTION and n.status != REMOVED
                                       for n in notes),
                    "open_questions": sum(n.status == OPEN for n in notes),
                    "pending": len(self._pending(s, intake)),
                })
            return rows

    def intake_for_note(self, note_id: int) -> int:
        with Session(self.engine) as s:
            return self._note(s, note_id).intake_id

    # ---------- internals ----------

    def _intake(self, s: Session, intake_id: int) -> Intake:
        intake = s.get(Intake, intake_id)
        if intake is None:
            raise NoteError("That intake no longer exists.")
        return intake

    def _note(self, s: Session, note_id: int) -> Note:
        note = s.get(Note, note_id)
        if note is None:
            raise NoteError("That item no longer exists.")
        return note

    def _notes(self, s: Session, intake_id: int) -> list[Note]:
        return list(s.exec(select(Note).where(Note.intake_id == intake_id).order_by(Note.id)))

    def _max_version_id(self, s: Session) -> int:
        return s.exec(select(func.max(NoteVersion.id))).one() or 0

    def _pending(self, s: Session, intake: Intake) -> list[tuple[NoteVersion, Note]]:
        rows = s.exec(select(NoteVersion, Note).join(Note, NoteVersion.note_id == Note.id)
                      .where(Note.intake_id == intake.id, NoteVersion.material,
                             NoteVersion.id > intake.reviewed_through)
                      .order_by(NoteVersion.id)).all()
        return [(v, n) for v, n in rows]

    def _create(self, s: Session, intake_id: int, kind: str, text: str, actor: str,
                by_ai: bool) -> Note:
        status = ACTIVE if kind == ASSUMPTION else OPEN
        note = Note(intake_id=intake_id, kind=kind, text=text, status=status, updated_by=actor)
        s.add(note)
        s.flush()
        s.add(NoteVersion(note_id=note.id, version=1, text=text, answer="", status=status,
                          change="created", changed_by=actor,
                          material=is_material(kind, "created", by_ai)))
        if not by_ai:
            audit(s, actor, f"{kind}_added", f"#{note.id}: {text}")
        return note

    def _change(self, s: Session, note: Note, actor: str, change: str, *,
                text: str | None = None, answer: str | None = None, status: str | None = None,
                reason: str = "", answer_changed: bool = False) -> None:
        note.text = note.text if text is None else text
        note.answer = note.answer if answer is None else answer
        note.status = note.status if status is None else status
        note.version += 1
        note.updated_by = actor
        note.updated_at = utcnow()
        s.add(note)
        s.add(NoteVersion(note_id=note.id, version=note.version, text=note.text,
                          answer=note.answer, status=note.status, change=change,
                          reason=reason.strip(), changed_by=actor,
                          material=is_material(note.kind, change, False, answer_changed)))
        audit(s, actor, f"{note.kind}_{change}",
              f"#{note.id} v{note.version}: {note.text}"
              + (f" | answer: {note.answer}" if note.answer and change == "answered" else "")
              + (f" | reason: {reason.strip()}" if reason.strip() else ""))
