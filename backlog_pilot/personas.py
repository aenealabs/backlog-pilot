"""Persona library: the approved personas every draft must use, with version history.

Drafts are prompted with the approved personas; personas the model needs but cannot find
are proposed, and become part of the library only when a reviewer accepts them.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backlog_pilot.db import audit, utcnow
from backlog_pilot.models import PersonaProposal

APPROVED, PROPOSED, RETIRED = "approved", "proposed", "retired"


class Persona(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)  # lower case; matched against story "as a"
    description: str
    goals: str = "[]"  # JSON list
    pain_points: str = "[]"  # JSON list
    status: str = APPROVED
    version: int = 1
    updated_by: str
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def goal_list(self) -> list[str]:
        return json.loads(self.goals)

    @property
    def pain_list(self) -> list[str]:
        return json.loads(self.pain_points)


class PersonaVersion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    persona_id: int = Field(foreign_key="persona.id", index=True)
    version: int
    name: str
    description: str
    goals: str
    pain_points: str
    status: str
    change: str  # created | proposed | approved | edited | retired | restored
    reason: str = ""
    changed_by: str
    changed_at: datetime = Field(default_factory=utcnow)

    @property
    def goal_list(self) -> list[str]:
        return json.loads(self.goals)

    @property
    def pain_list(self) -> list[str]:
        return json.loads(self.pain_points)


class PersonaError(Exception):
    """A persona change was rejected; the message is user-facing."""


@dataclass(frozen=True)
class PersonaBrief:
    """What a draft is told about a persona."""
    name: str
    description: str


def normalize(name: str) -> str:
    return " ".join(name.split()).lower()


def _lines(items: list[str] | str) -> list[str]:
    if isinstance(items, str):
        items = items.splitlines()
    return [i.strip(" -•\t") for i in items if i.strip(" -•\t")]


class PersonaStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def seed_if_empty(self, path: Path) -> None:
        with Session(self.engine) as s:
            if s.exec(select(func.count()).select_from(Persona)).one():
                return
            for p in json.loads(path.read_text())["personas"]:
                self._create(s, p["name"], p["description"], p["goals"], p["pain_points"],
                             "Persona library (seed)", "created")
            s.commit()

    # ---------- reads ----------

    def briefs(self) -> list[PersonaBrief]:
        return [PersonaBrief(p.name, p.description)
                for p in self.all_personas() if p.status == APPROVED]

    def all_personas(self) -> list[Persona]:
        with Session(self.engine) as s:
            return list(s.exec(select(Persona).order_by(Persona.status, Persona.name)))

    def approved_names(self) -> set[str]:
        return {p.name for p in self.all_personas() if p.status == APPROVED}

    def history(self, persona_id: int) -> tuple[Persona, list[PersonaVersion]]:
        with Session(self.engine) as s:
            persona = self._get(s, persona_id)
            versions = list(s.exec(select(PersonaVersion)
                                   .where(PersonaVersion.persona_id == persona_id)
                                   .order_by(PersonaVersion.version.desc())))
            return persona, versions

    def get(self, persona_id: int) -> Persona:
        with Session(self.engine) as s:
            return self._get(s, persona_id)

    # ---------- writes ----------

    def accept_proposal(self, s: Session, proposal: PersonaProposal, actor: str,
                        ai_label: str) -> Persona | None:
        """Add an AI-proposed persona the reviewer accepted (caller commits)."""
        if s.exec(select(Persona).where(Persona.name == normalize(proposal.name))).first():
            return None
        persona = self._create(s, proposal.name, proposal.description, proposal.goals,
                               proposal.pain_points, ai_label, "proposed")
        self._change(s, persona, actor, "approved", reason="Accepted during draft review")
        return persona

    def propose(self, source: str, name: str, description: str, goals: list[str],
                pain_points: list[str]) -> Persona:
        """A persona proposed from outside (Leadership Pilot); it joins the library only
        when an approver accepts it."""
        if not name.strip() or not description.strip():
            raise PersonaError("A persona needs a name and a description.")
        with Session(self.engine) as s:
            if s.exec(select(Persona).where(Persona.name == normalize(name))).first():
                raise PersonaError(f"A persona named '{normalize(name)}' already exists.")
            persona = self._create(s, name, description, goals, pain_points, source,
                                   "proposed", status=PROPOSED)
            s.commit()
            s.refresh(persona)
            return persona

    def add(self, actor: str, name: str, description: str, goals: str, pain_points: str) -> Persona:
        if not name.strip() or not description.strip():
            raise PersonaError("A persona needs a name and a description.")
        with Session(self.engine) as s:
            if s.exec(select(Persona).where(Persona.name == normalize(name))).first():
                raise PersonaError(f"A persona named '{normalize(name)}' already exists.")
            persona = self._create(s, name, description, _lines(goals), _lines(pain_points),
                                   actor, "created")
            s.commit()
            s.refresh(persona)
            return persona

    def edit(self, persona_id: int, actor: str, name: str, description: str, goals: str,
             pain_points: str, reason: str = "") -> Persona:
        if not name.strip() or not description.strip():
            raise PersonaError("A persona needs a name and a description.")
        with Session(self.engine) as s:
            persona = self._get(s, persona_id)
            new_name = normalize(name)
            clash = s.exec(select(Persona).where(Persona.name == new_name,
                                                 Persona.id != persona_id)).first()
            if clash:
                raise PersonaError(f"A persona named '{new_name}' already exists.")
            fields = {"name": new_name, "description": description.strip(),
                      "goals": json.dumps(_lines(goals)),
                      "pain_points": json.dumps(_lines(pain_points))}
            if all(getattr(persona, k) == v for k, v in fields.items()):
                return persona
            for k, v in fields.items():
                setattr(persona, k, v)
            self._change(s, persona, actor, "edited", reason=reason)
            s.commit()
            s.refresh(persona)
            return persona

    def set_status(self, persona_id: int, actor: str, status: str, reason: str = "") -> Persona:
        with Session(self.engine) as s:
            persona = self._get(s, persona_id)
            if persona.status == PROPOSED and status != APPROVED and not reason.strip():
                raise PersonaError(f"Say why the proposed persona '{persona.name}' is rejected; "
                                   "it was proposed from what stakeholders said.")
            if persona.status != status:
                was = persona.status
                persona.status = status
                if was == PROPOSED:
                    change = "approved" if status == APPROVED else "rejected"
                else:
                    change = "retired" if status == RETIRED else "restored"
                self._change(s, persona, actor, change, reason=reason)
                s.commit()
                s.refresh(persona)
            return persona

    # ---------- internals ----------

    def _get(self, s: Session, persona_id: int) -> Persona:
        persona = s.get(Persona, persona_id)
        if persona is None:
            raise PersonaError("That persona no longer exists.")
        return persona

    def _create(self, s: Session, name: str, description: str, goals: list[str],
                pain_points: list[str], actor: str, change: str,
                status: str = APPROVED) -> Persona:
        persona = Persona(name=normalize(name), description=description.strip(),
                          goals=json.dumps(_lines(goals)),
                          pain_points=json.dumps(_lines(pain_points)), updated_by=actor,
                          status=status)
        s.add(persona)
        s.flush()
        s.add(self._snapshot(persona, change, actor))
        audit(s, actor, f"persona_{change}", f"#{persona.id} {persona.name}")
        return persona

    def _change(self, s: Session, persona: Persona, actor: str, change: str,
                reason: str = "") -> None:
        persona.version += 1
        persona.updated_by = actor
        persona.updated_at = utcnow()
        s.add(persona)
        s.add(self._snapshot(persona, change, actor, reason))
        audit(s, actor, f"persona_{change}", f"#{persona.id} {persona.name} v{persona.version}"
              + (f" | reason: {reason.strip()}" if reason.strip() else ""))

    @staticmethod
    def _snapshot(p: Persona, change: str, actor: str, reason: str = "") -> PersonaVersion:
        return PersonaVersion(persona_id=p.id, version=p.version, name=p.name,
                              description=p.description, goals=p.goals,
                              pain_points=p.pain_points, status=p.status, change=change,
                              reason=reason.strip(), changed_by=actor)
