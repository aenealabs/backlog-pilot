"""Roadmap: products, quarterly capacity, and a plan derived from WSJF.

The plan is computed, not drawn: features fill quarters in WSJF order until each quarter's
capacity (in WSJF job-size points) is used. In-progress work stays in the current quarter,
pinned features go where a person put them, and whatever does not fit is "Unplanned".
Every recalculation is diffed against the last one, so each move is logged with its cause.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backlog_pilot.db import audit, utcnow
from backlog_pilot.pipeline.similarity import Candidate, find_overlaps_text

HORIZON = 4  # quarters shown and planned
NEW, REMOVED = "new", "removed"  # change-log slots outside the timeline
DONE, IN_PROGRESS = "Done", "In Progress"


# ---------- quarters ----------

def quarter_of(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def quarters_from(start: str, n: int = HORIZON) -> list[str]:
    year, q = int(start[:4]), int(start[-1])
    out = []
    for _ in range(n):
        out.append(f"{year}-Q{q}")
        q += 1
        if q > 4:
            year, q = year + 1, 1
    return out


def quarter_label(quarter: str) -> str:
    return f"Q{quarter[-1]} {quarter[:4]}"


def slot_label(slot: str) -> str:
    """Label for a change-log position: a quarter, unplanned, new, or removed."""
    if slot == "":
        return "unplanned"
    if slot in (NEW, REMOVED):
        return slot
    return quarter_label(slot)


# ---------- tables ----------

class Product(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    name: str
    description: str
    default_capacity: int = 13


class Capacity(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    quarter: str
    points: int


class RoadmapSnapshot(SQLModel, table=True):
    """The last computed placement per product: {feature key: quarter or ''}."""
    product_id: int = Field(primary_key=True, foreign_key="product.id")
    placements: str = "{}"


class RoadmapChange(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    feature_key: str
    feature_title: str
    from_quarter: str  # a quarter, '' (unplanned), or 'new'
    to_quarter: str  # a quarter, '' (unplanned), or 'removed' (done or superseded)
    cause: str
    changed_by: str
    changed_at: datetime = Field(default_factory=utcnow)


# ---------- planning (pure) ----------

@dataclass
class FeatureInput:
    key: str
    title: str
    epic_key: str
    epic_title: str
    status: str
    size: int | None  # WSJF job size; None = unscored
    score: float | None
    pin: str | None = None  # quarter a person pinned it to
    pin_reason: str = ""


@dataclass
class Placement:
    feature: FeatureInput
    quarter: str | None
    reason: str


@dataclass
class Plan:
    quarters: list[str]
    capacity: dict[str, int]
    placed: dict[str, list[Placement]]  # quarter -> placements in plan order
    unplanned: list[Placement]
    unscored: list[FeatureInput]
    done: list[FeatureInput]
    used: dict[str, int] = field(default_factory=dict)

    def quarter_of(self, key: str) -> str | None:
        for q, items in self.placed.items():
            if any(p.feature.key == key for p in items):
                return q
        return None

    def placements(self) -> dict[str, str]:
        out = {p.feature.key: q for q, items in self.placed.items() for p in items}
        out |= {p.feature.key: "" for p in self.unplanned}
        return out

    def epic_spans(self) -> list[dict]:
        """Each epic with the first and last quarter its planned features occupy."""
        spans: dict[str, dict] = {}
        for q_index, q in enumerate(self.quarters):
            for p in self.placed[q]:
                f = p.feature
                span = spans.setdefault(f.epic_key, {"key": f.epic_key, "title": f.epic_title,
                                                     "start": q_index, "end": q_index,
                                                     "features": []})
                span["end"] = q_index
                span["features"].append(p)
        return sorted(spans.values(), key=lambda s: (s["start"], s["title"]))


def plan_product(features: list[FeatureInput], quarters: list[str],
                 capacity: dict[str, int]) -> Plan:
    placed: dict[str, list[Placement]] = {q: [] for q in quarters}
    used = dict.fromkeys(quarters, 0)
    unplanned: list[Placement] = []
    done = [f for f in features if f.status == DONE]
    live = [f for f in features if f.status != DONE]
    unscored = [f for f in live if f.size is None]
    scored = [f for f in live if f.size is not None]

    def place(f: FeatureInput, q: str, reason: str) -> None:
        placed[q].append(Placement(f, q, reason))
        used[q] += f.size or 0

    current = quarters[0]
    rest = []
    for f in scored:
        if f.pin in used:
            place(f, f.pin, f"Pinned: {f.pin_reason}" if f.pin_reason else "Pinned")
        elif f.status == IN_PROGRESS:
            place(f, current, "In progress")
        else:
            rest.append(f)
    for f in sorted(rest, key=lambda f: (-(f.score or 0), f.key)):
        for q in quarters:
            if used[q] + f.size <= capacity[q]:
                place(f, q, "Next by WSJF with room in this quarter")
                break
        else:
            unplanned.append(Placement(f, None, "Over capacity in every quarter shown"))
    return Plan(quarters, capacity, placed, unplanned, unscored, done, used)


# ---------- store ----------

class RoadmapStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def seed_if_empty(self, path: Path) -> None:
        with Session(self.engine) as s:
            if s.exec(select(Product)).first():
                return
            for p in json.loads(path.read_text())["products"]:
                s.add(Product(key=p["key"], name=p["name"], description=p["description"],
                              default_capacity=p["default_capacity"]))
            s.commit()

    def products(self) -> list[Product]:
        with Session(self.engine) as s:
            return list(s.exec(select(Product).order_by(Product.id)))

    def product(self, key: str) -> Product | None:
        with Session(self.engine) as s:
            return s.exec(select(Product).where(Product.key == key)).first()

    def product_by_id(self, product_id: int) -> Product | None:
        with Session(self.engine) as s:
            return s.get(Product, product_id)

    def suggest_product(self, text: str) -> Product | None:
        """Best product for an epic by text similarity to product descriptions."""
        products = self.products()
        candidates = [Candidate(p.key, "product", p.name, f"{p.name} {p.description}", "")
                      for p in products]
        best = find_overlaps_text(text, candidates, minimum=0.0)
        return next((p for p in products if best and p.key == best[0].key), None)

    def update_product(self, key: str, actor: str, name: str, description: str,
                       default_capacity: int) -> None:
        with Session(self.engine) as s:
            product = s.exec(select(Product).where(Product.key == key)).one()
            product.name, product.description = name.strip(), description.strip()
            product.default_capacity = max(0, default_capacity)
            s.add(product)
            audit(s, actor, "product_updated",
                  f"{key}: {product.name}, default capacity {product.default_capacity}")
            s.commit()

    def capacity(self, product: Product, quarters: list[str]) -> dict[str, int]:
        with Session(self.engine) as s:
            rows = s.exec(select(Capacity).where(Capacity.product_id == product.id)).all()
        overrides = {r.quarter: r.points for r in rows}
        return {q: overrides.get(q, product.default_capacity) for q in quarters}

    def set_capacity(self, product: Product, quarter: str, points: int, actor: str) -> None:
        with Session(self.engine) as s:
            row = s.exec(select(Capacity).where(Capacity.product_id == product.id,
                                                Capacity.quarter == quarter)).first()
            before = row.points if row else product.default_capacity
            if before == points:
                return
            row = row or Capacity(product_id=product.id, quarter=quarter, points=points)
            row.points = points
            s.add(row)
            audit(s, actor, "capacity_changed",
                  f"{product.name} {quarter_label(quarter)}: {before} -> {points} points")
            s.commit()

    def last_placements(self, product: Product) -> dict[str, str] | None:
        with Session(self.engine) as s:
            snap = s.get(RoadmapSnapshot, product.id)
            return json.loads(snap.placements) if snap else None

    def record(self, product: Product, plan: Plan, cause: str, actor: str) -> list[str]:
        """Diff the plan against the last snapshot, log every move, and save the new one."""
        titles = {p.feature.key: p.feature.title
                  for items in plan.placed.values() for p in items}
        titles |= {p.feature.key: p.feature.title for p in plan.unplanned}
        new = plan.placements()
        moved = []
        with Session(self.engine) as s:
            snap = s.get(RoadmapSnapshot, product.id)
            old = json.loads(snap.placements) if snap else None
            if old is not None:
                for key in sorted(set(old) | set(new)):
                    before = old.get(key, NEW)
                    after = new.get(key, REMOVED)
                    if before != after:
                        moved.append(key)
                        s.add(RoadmapChange(product_id=product.id, feature_key=key,
                                            feature_title=titles.get(key, key),
                                            from_quarter=before, to_quarter=after,
                                            cause=cause, changed_by=actor))
            snap = snap or RoadmapSnapshot(product_id=product.id)
            snap.placements = json.dumps(new)
            s.add(snap)
            s.commit()
        return moved

    def changes(self, product: Product | None = None, limit: int = 30) -> list[RoadmapChange]:
        with Session(self.engine) as s:
            query = select(RoadmapChange).order_by(RoadmapChange.id.desc()).limit(limit)
            if product is not None:
                query = query.where(RoadmapChange.product_id == product.id)
            return list(s.exec(query))
