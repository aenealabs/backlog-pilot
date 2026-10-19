"""SQLite-backed stand-in for Jira, so the full flow works without tracker access."""

import json
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backlog_pilot.db import audit, utcnow
from backlog_pilot.models import WSJF_PARTS, WSJF_SCALE, Decomposition, Feature, WsjfEstimate
from backlog_pilot.pipeline.similarity import Candidate
from backlog_pilot.roadmap import FeatureInput
from backlog_pilot.sync import ExternalRef, SyncError, SyncItem, Tracker

SUPERSEDED = "Superseded"


class BacklogItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    item_type: str  # epic | feature | story
    parent_id: int | None = Field(default=None, foreign_key="backlogitem.id")
    intake_id: int | None = Field(default=None, foreign_key="intake.id", index=True)
    product_id: int | None = Field(default=None, foreign_key="product.id", index=True)
    title: str
    body: str = "{}"  # JSON: type-specific fields
    status: str = "New"
    source: str = "sample"  # sample | backlog-pilot
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def details(self) -> dict:
        return json.loads(self.body)


class MockTracker:
    name = "Mock tracker"
    key_prefix = "BP"

    def __init__(self, engine: Engine):
        self.engine = engine

    def seed_if_empty(self, sample_path: Path, product_ids: dict[str, int]) -> None:
        with Session(self.engine) as s:
            if s.exec(select(func.count()).select_from(BacklogItem)).one():
                return
            sample = json.loads(sample_path.read_text())
            for epic in sample["epics"]:
                product_id = product_ids.get(epic.get("product", ""))
                e = self._add(s, "epic", None, None, epic["title"], epic, "sample",
                              epic["status"], product_id)
                for feature in epic["features"]:
                    if "wsjf" in feature:
                        feature = feature | {"wsjf_history": [_wsjf_entry(
                            feature["wsjf"], "Sample backlog (seed)", "drafted")]}
                    self._add(s, "feature", e.id, None, feature["title"], feature, "sample",
                              feature["status"], product_id)
            s.commit()

    def _next_key(self, s: Session) -> str:
        count = s.exec(select(func.count()).select_from(BacklogItem)).one()
        return f"{self.key_prefix}-{101 + count}"

    def _add(self, s: Session, item_type: str, parent_id: int | None, intake_id: int | None,
             title: str, details: dict, source: str, status: str = "New",
             product_id: int | None = None) -> BacklogItem:
        body = {k: v for k, v in details.items()
                if k not in {"title", "features", "stories", "product"}}
        item = BacklogItem(key=self._next_key(s), item_type=item_type, parent_id=parent_id,
                           intake_id=intake_id, product_id=product_id, title=title,
                           body=json.dumps(body), status=status, source=source)
        s.add(item)
        s.flush()
        return item

    def create_from(self, s: Session, decomposition: Decomposition, actor: str,
                    intake_id: int | None = None, ai_label: str = "AI draft",
                    product_ids: dict[str, int] | None = None) -> list[str]:
        """Add an approved decomposition as epics -> features -> stories (caller commits)."""
        keys: list[str] = []
        product_ids = product_ids or {}
        for epic in decomposition.epics:
            pid = product_ids.get(epic._product or "")
            e = self._add(s, "epic", None, intake_id, epic.title,
                          epic.model_dump() | self._ai_wording(s, actor, epic),
                          "backlog-pilot", product_id=pid)
            keys.append(e.key)
            for feature in epic.features:
                details = feature.model_dump()
                details["wsjf_history"] = self._initial_wsjf_history(feature, actor, ai_label)
                details["related"] = feature._related
                details |= self._ai_wording(s, actor, feature)
                f = self._add(s, "feature", e.id, intake_id, feature.title, details,
                              "backlog-pilot", product_id=pid)
                keys.append(f.key)
                self._audit_related(s, actor, f.key, feature._related)
                if len(details["wsjf_history"]) > 1:
                    audit(s, actor, "wsjf_adjusted",
                          f"{f.key}: {_fmt(feature._ai_wsjf)} -> {_fmt(feature.wsjf)} "
                          f"(during draft review) | reason: {feature._wsjf_reason}")
                for story in feature.stories:
                    st = self._add(s, "story", f.id, intake_id, story.title,
                                   story.model_dump() | {"related": story._related}
                                   | self._ai_wording(s, actor, story),
                                   "backlog-pilot", product_id=pid)
                    keys.append(st.key)
                    self._audit_related(s, actor, st.key, story._related)
        audit(s, actor, "approve_and_push", f"Created {len(keys)} items: {', '.join(keys)}")
        return keys

    @staticmethod
    def _ai_wording(s: Session, actor: str, item) -> dict:
        """Keep the AI's title beside a reviewer's rewording, and audit the change."""
        if not item._ai_title:
            return {}
        audit(s, actor, "draft_title_edited", f"'{item._ai_title}' -> '{item.title}'")
        return {"ai_title": item._ai_title}

    @staticmethod
    def _audit_related(s: Session, actor: str, key: str, related: list[str]) -> None:
        if related:
            audit(s, actor, "linked_related", f"{key} related to {', '.join(related)}")

    def candidates(self, exclude_intake: int | None = None) -> list[Candidate]:
        """Live features and stories a new draft could duplicate."""
        with Session(self.engine) as s:
            items = s.exec(select(BacklogItem).where(
                BacklogItem.status != SUPERSEDED,
                BacklogItem.item_type.in_(["feature", "story"]))).all()
        out = []
        for item in items:
            if exclude_intake is not None and item.intake_id == exclude_intake:
                continue
            d = item.details
            extra = d.get("description", "") if item.item_type == "feature" else d.get("i_want", "")
            out.append(Candidate(item.key, item.item_type, item.title,
                                 f"{item.title} {extra}", item.status))
        return out

    @staticmethod
    def _initial_wsjf_history(feature: Feature, actor: str, ai_label: str) -> list[dict]:
        history = []
        ai, final = feature._ai_wsjf, feature.wsjf
        if ai is not None:
            history.append(_wsjf_entry(ai.model_dump(), ai_label, "drafted"))
        if final is not None and (ai is None or _parts(final) != _parts(ai)):
            history.append(_wsjf_entry(final.model_dump(), actor, "adjusted",
                                       feature._wsjf_reason or "Adjusted during draft review"))
        return history

    def set_wsjf(self, key: str, values: dict[str, int], actor: str, reason: str = "") -> None:
        """Re-score a feature; every change is appended to its WSJF history and audited."""
        if set(values) != set(WSJF_PARTS) or any(v not in WSJF_SCALE for v in values.values()):
            raise ValueError("Each WSJF part needs a value from the scale 1-20.")
        with Session(self.engine) as s:
            item = s.exec(select(BacklogItem).where(BacklogItem.key == key,
                                                    BacklogItem.item_type == "feature")).first()
            if item is None:
                raise ValueError(f"No feature {key}.")
            details = item.details
            before = details.get("wsjf")
            if before and _parts_dict(before) == values:
                return
            if before and not reason.strip():
                raise ValueError("Say why you are changing the score, so the change can be "
                                 "understood later.")
            rationale = (before or {}).get("rationale", "")
            details["wsjf"] = {**values, "rationale": rationale}
            details.setdefault("wsjf_history", []).append(
                _wsjf_entry(details["wsjf"], actor, "rescored" if before else "scored", reason))
            item.body = json.dumps(details)
            s.add(item)
            audit(s, actor, "wsjf_rescored" if before else "wsjf_scored",
                  f"{key}: {_fmt_dict(before)} -> {_fmt_dict(values)}"
                  + (f" | reason: {reason.strip()}" if reason.strip() else ""))
            s.commit()

    def feature_inputs(self, product_id: int) -> list[FeatureInput]:
        """Live features of a product, shaped for roadmap planning."""
        with Session(self.engine) as s:
            items = s.exec(select(BacklogItem).where(
                BacklogItem.product_id == product_id, BacklogItem.status != SUPERSEDED,
                BacklogItem.item_type.in_(["epic", "feature"])).order_by(BacklogItem.id)).all()
        epics = {i.id: i for i in items if i.item_type == "epic"}
        out = []
        for f in items:
            if f.item_type != "feature":
                continue
            epic = epics.get(f.parent_id)
            wsjf, pin = f.details.get("wsjf"), f.details.get("pin") or {}
            out.append(FeatureInput(
                key=f.key, title=f.title, epic_key=epic.key if epic else "",
                epic_title=epic.title if epic else "", status=f.status,
                size=wsjf["job_size"] if wsjf else None, score=_score(wsjf),
                pin=pin.get("quarter"), pin_reason=pin.get("reason", "")))
        return out

    def set_pin(self, key: str, quarter: str | None, reason: str, actor: str) -> int | None:
        """Pin a feature to a quarter (or unpin with None). Returns its product id.

        A pin overrides where the roadmap rules would place the feature, so it needs a reason.
        """
        if quarter and not reason.strip():
            raise ValueError("Say why this feature is pinned. A pin overrides where the "
                             "roadmap rules would place it.")
        with Session(self.engine) as s:
            item = s.exec(select(BacklogItem).where(BacklogItem.key == key,
                                                    BacklogItem.item_type == "feature")).one()
            details = item.details
            if quarter:
                details["pin"] = {"quarter": quarter, "reason": reason.strip(), "by": actor,
                                  "at": utcnow().isoformat(timespec="seconds")}
                audit(s, actor, "roadmap_pinned", f"{key} pinned to {quarter}"
                      + (f" | reason: {reason.strip()}" if reason.strip() else ""))
            else:
                details.pop("pin", None)
                audit(s, actor, "roadmap_unpinned", f"{key} unpinned")
            item.body = json.dumps(details)
            s.add(item)
            s.commit()
            return item.product_id

    def product_of(self, key: str) -> int | None:
        with Session(self.engine) as s:
            item = s.exec(select(BacklogItem).where(BacklogItem.key == key)).first()
            return item.product_id if item else None

    # ---------- push to an external tracker ----------

    def pending_sync(self) -> list[BacklogItem]:
        """Approved items not yet in the external tracker, parents first."""
        with Session(self.engine) as s:
            items = s.exec(select(BacklogItem).where(
                BacklogItem.source == "backlog-pilot", BacklogItem.status != SUPERSEDED)
                .order_by(BacklogItem.id)).all()
        return [i for i in items if "external" not in i.details]

    def push_pending(self, external: Tracker, actor: str,
                     product_names: dict[int, str]) -> tuple[int, int]:
        """Push every pending item; returns (pushed, failed). Each result is saved at once,
        so a failure part-way keeps everything already pushed."""
        pushed = failed = 0
        for item in self.pending_sync():
            with Session(self.engine) as s:
                item = s.get(BacklogItem, item.id)
                parent = s.get(BacklogItem, item.parent_id) if item.parent_id else None
                parent_ref = None
                if parent is not None:
                    ext = parent.details.get("external")
                    if ext is None and parent.source == "backlog-pilot":
                        self._sync_failed(s, item, actor, "Waiting for its parent to sync.")
                        failed += 1
                        continue
                    if ext:
                        parent_ref = ExternalRef(**ext)
                try:
                    ref = external.create(self._sync_item(item, product_names), parent_ref)
                except (SyncError, httpx.HTTPError) as e:
                    self._sync_failed(s, item, actor, str(e))
                    failed += 1
                    continue
                details = item.details
                details.pop("sync_error", None)
                details["external"] = {"system": ref.system, "id": ref.id, "key": ref.key,
                                       "url": ref.url}
                item.body = json.dumps(details)
                s.add(item)
                audit(s, actor, "synced_to_tracker", f"{item.key} -> {external.label} {ref.key}")
                s.commit()
                pushed += 1
        return pushed, failed

    @staticmethod
    def _sync_failed(s: Session, item: BacklogItem, actor: str, message: str) -> None:
        details = item.details
        details["sync_error"] = message
        item.body = json.dumps(details)
        s.add(item)
        audit(s, actor, "sync_failed", f"{item.key}: {message}")
        s.commit()

    @staticmethod
    def _sync_item(item: BacklogItem, product_names: dict[int, str]) -> SyncItem:
        d = item.details
        if item.item_type == "story":
            description = (f"As a {d.get('as_a', '')}, I want {d.get('i_want', '')}, "
                           f"so that {d.get('so_that', '')}.")
        elif item.item_type == "epic":
            description = d.get("description", "") + (
                f"\n\nBusiness outcome: {d['business_outcome']}"
                if d.get("business_outcome") else "")
        else:
            description = d.get("description", "")
        return SyncItem(key=item.key, item_type=item.item_type, title=item.title,
                        description=description,
                        acceptance_criteria=d.get("acceptance_criteria", []),
                        wsjf=d.get("wsjf"),
                        product=product_names.get(item.product_id, ""))

    def ranked_features(self) -> list[dict]:
        """Live features by WSJF, highest first; unscored features last."""
        with Session(self.engine) as s:
            items = s.exec(select(BacklogItem).where(BacklogItem.status != SUPERSEDED)).all()
        by_id = {i.id: i for i in items}
        rows = []
        for item in items:
            if item.item_type != "feature":
                continue
            wsjf = item.details.get("wsjf")
            history = item.details.get("wsjf_history", [])
            rows.append({
                "item": item,
                "epic": by_id.get(item.parent_id),
                "wsjf": wsjf,
                "score": _score(wsjf),
                "last": history[-1] if history else None,
                "ai": next((h for h in history if h["change"] == "drafted"), None),
            })
        rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["item"].id))
        return rows

    def supersede_intake_items(self, s: Session, intake_id: int, actor: str) -> list[str]:
        """Mark every live item drafted from an intake as Superseded (caller commits)."""
        items = s.exec(select(BacklogItem).where(BacklogItem.intake_id == intake_id,
                                                 BacklogItem.status != SUPERSEDED)).all()
        for item in items:
            item.status = SUPERSEDED
            s.add(item)
        keys = [i.key for i in items]
        if keys:
            audit(s, actor, "supersede", f"Superseded {len(keys)} items: {', '.join(keys)}")
        return keys

    def tree(self, include_superseded: bool = False) -> list[dict]:
        """Epics with nested features and stories, newest epics first."""
        with Session(self.engine) as s:
            query = select(BacklogItem).order_by(BacklogItem.id)
            if not include_superseded:
                query = query.where(BacklogItem.status != SUPERSEDED)
            items = s.exec(query).all()
        children: dict[int | None, list[BacklogItem]] = {}
        for item in items:
            children.setdefault(item.parent_id, []).append(item)

        def node(item: BacklogItem) -> dict:
            return {"item": item, "children": [node(c) for c in children.get(item.id, [])]}

        return [node(e) for e in reversed(children.get(None, []))]

    def epics_for_intake(self, intake_id: int) -> list[BacklogItem]:
        with Session(self.engine) as s:
            return list(s.exec(select(BacklogItem).where(
                BacklogItem.intake_id == intake_id, BacklogItem.item_type == "epic")
                .order_by(BacklogItem.id.desc())))

    def stories_for_intake(self, intake_id: int) -> list[BacklogItem]:
        """Live (not superseded) stories drafted from an intake."""
        with Session(self.engine) as s:
            return list(s.exec(select(BacklogItem).where(
                BacklogItem.intake_id == intake_id, BacklogItem.item_type == "story",
                BacklogItem.status != SUPERSEDED).order_by(BacklogItem.id)))

    def persona_usage(self) -> dict[str, int]:
        """How many live stories each persona appears in, keyed by normalized name."""
        with Session(self.engine) as s:
            stories = s.exec(select(BacklogItem).where(
                BacklogItem.item_type == "story", BacklogItem.status != SUPERSEDED)).all()
        usage: dict[str, int] = {}
        for story in stories:
            name = " ".join(story.details.get("as_a", "").split()).lower()
            if name:
                usage[name] = usage.get(name, 0) + 1
        return usage

    def counts(self) -> dict[str, int]:
        with Session(self.engine) as s:
            rows = s.exec(select(BacklogItem.item_type, func.count())
                          .where(BacklogItem.status != SUPERSEDED)
                          .group_by(BacklogItem.item_type)).all()
            superseded = s.exec(select(func.count()).select_from(BacklogItem)
                                .where(BacklogItem.status == SUPERSEDED)).one()
        return {**{t: n for t, n in rows}, "superseded": superseded}


def _parts(estimate: WsjfEstimate) -> dict[str, int]:
    return {p: getattr(estimate, p) for p in WSJF_PARTS}


def _parts_dict(values: dict) -> dict[str, int]:
    return {p: values[p] for p in WSJF_PARTS}


def _score(values: dict | None) -> float | None:
    if not values:
        return None
    cost_of_delay = values["business_value"] + values["time_criticality"] + values["risk_reduction"]
    return round(cost_of_delay / values["job_size"], 1)


def _fmt_dict(values: dict | None) -> str:
    if not values:
        return "unscored"
    return ("BV {business_value} / TC {time_criticality} / RR {risk_reduction} / "
            "JS {job_size}").format(**values) + f" = {_score(values)}"


def _fmt(estimate: WsjfEstimate | None) -> str:
    return _fmt_dict(estimate.model_dump() if estimate else None)


def _wsjf_entry(values: dict, by: str, change: str, reason: str = "") -> dict:
    return {"values": _parts_dict(values), "score": _score(values), "by": by, "change": change,
            "reason": reason.strip(), "at": utcnow().isoformat(timespec="seconds")}
