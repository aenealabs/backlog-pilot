from datetime import date

import pytest
from fastapi.testclient import TestClient

from backlog_pilot.app import create_app
from backlog_pilot.db import utcnow
from backlog_pilot.roadmap import (
    FeatureInput,
    RoadmapStore,
    plan_product,
    quarter_label,
    quarter_of,
    quarters_from,
)
from tests.conftest import payload_from, source_from

Q = ["2026-Q4", "2027-Q1", "2027-Q2", "2027-Q3"]


def test_quarter_math():
    assert quarter_of(date(2026, 10, 17)) == "2026-Q4"
    assert quarter_of(date(2027, 1, 1)) == "2027-Q1"
    assert quarters_from("2026-Q4") == Q
    assert quarter_label("2027-Q1") == "Q1 2027"


def feature(key, size, score, status="New", pin=None, epic="E1"):
    return FeatureInput(key=key, title=key, epic_key=epic, epic_title=epic, status=status,
                        size=size, score=score, pin=pin)


def test_fills_quarters_in_wsjf_order_up_to_capacity():
    plan = plan_product([feature("low", 5, 1.0), feature("high", 8, 9.0),
                         feature("mid", 8, 4.0)], Q, dict.fromkeys(Q, 13))
    assert [p.feature.key for p in plan.placed["2026-Q4"]] == ["high", "low"]
    assert [p.feature.key for p in plan.placed["2027-Q1"]] == ["mid"]
    assert plan.used["2026-Q4"] == 13


def test_in_progress_and_pins_place_first_even_over_capacity():
    plan = plan_product([feature("wip", 8, 1.0, status="In Progress"),
                         feature("pinned", 8, 0.5, pin="2026-Q4"),
                         feature("top", 5, 9.0)], Q, dict.fromkeys(Q, 13))
    q4 = {p.feature.key: p.reason for p in plan.placed["2026-Q4"]}
    assert q4["wip"] == "In progress" and q4["pinned"].startswith("Pinned")
    assert plan.used["2026-Q4"] == 16 > plan.capacity["2026-Q4"]  # shown as over capacity
    assert plan.quarter_of("top") == "2027-Q1"


def test_unplanned_done_and_unscored_are_kept_apart():
    plan = plan_product([feature("huge", 20, 5.0), feature("shipped", 5, 9.0, status="Done"),
                         feature("unscored", None, None)], Q, dict.fromkeys(Q, 13))
    assert [p.feature.key for p in plan.unplanned] == ["huge"]
    assert [f.key for f in plan.done] == ["shipped"]
    assert [f.key for f in plan.unscored] == ["unscored"]
    assert plan.placements() == {"huge": ""}


def test_epic_spans_cover_first_to_last_quarter():
    plan = plan_product([feature("a", 13, 9.0), feature("b", 13, 8.0)], Q,
                        dict.fromkeys(Q, 13))
    (span,) = plan.epic_spans()
    assert (span["start"], span["end"]) == (0, 1)


# ---------- store and app ----------

@pytest.fixture
def roadmap(engine, settings):
    store = RoadmapStore(engine)
    store.seed_if_empty(settings.products_path)
    return store


@pytest.mark.parametrize("text, key", [
    ("Event-driven EBOM transfer to SAP on release, integration monitoring", "plm-sap"),
    ("Supplier portal acknowledgement of drawing changes and PPAP", "supplier"),
    ("Unified part search and duplicate part merge for part master data", "data-gov"),
    ("Migrate the legacy CAD vault files, drawings, and revisions", "cad"),
])
def test_product_suggestion(roadmap, text, key):
    assert roadmap.suggest_product(text).key == key


@pytest.fixture
def client(settings, engine):
    return TestClient(create_app(settings, engine=engine))


def current_quarters():
    return quarters_from(quarter_of(utcnow().date()))


def test_roadmap_pages_render_the_seeded_plan(client):
    page = client.get("/roadmap?product=plm-sap").text
    assert "SAP material and BOM upsert service" in page and "Capacity (points)" in page
    assert "BOM release event publisher in PLM" in page  # listed under Done
    assert client.get("/roadmap/portfolio").status_code == 200
    assert "Not scored" in client.get("/roadmap?product=plm").text


def test_capacity_change_moves_work_and_logs_it(client, tracker, roadmap):
    q = current_quarters()
    # plm-sap: in-progress upsert (8) sits in the current quarter; the dashboard (3) fits too.
    client.post("/roadmap/plm-sap/capacity", data={f"cap:{q[0]}": "8"})
    page = client.get("/roadmap?product=plm-sap").text
    assert "Capacity change for PLM-SAP Integration" in page
    change = roadmap.changes(roadmap.product("plm-sap"))[0]
    assert change.feature_title == "Integration error dashboard"
    assert (change.from_quarter, change.to_quarter) == (q[0], q[1])
    assert "capacity_changed" in client.get("/audit").text


def test_pin_and_rescore_are_logged_moves(client, roadmap, tracker):
    q = current_quarters()
    key = next(f.key for f in tracker.feature_inputs(roadmap.product("plm-sap").id)
               if f.title == "Integration error dashboard")
    refused = client.post(f"/features/{key}/pin", data={"quarter": q[2]})
    assert "Say why this feature is pinned" in refused.text
    client.post(f"/features/{key}/pin", data={"quarter": q[2], "reason": "Waiting on middleware"})
    change = roadmap.changes(roadmap.product("plm-sap"))[0]
    assert change.to_quarter == q[2] and "pinned" in change.cause
    assert "Waiting on middleware" in client.get("/audit").text
    client.post(f"/features/{key}/pin", data={"quarter": ""})
    assert roadmap.changes(roadmap.product("plm-sap"))[0].to_quarter == q[0]


class Stub:
    name = "stub"

    def __init__(self, draft):
        self.draft = draft

    def decompose(self, text, context=None):
        return self.draft


def test_review_suggests_a_product_and_approval_puts_work_on_its_roadmap(settings, engine,
                                                                       roadmap, tracker):
    from tests.test_quality_and_wsjf import scored_draft

    draft = scored_draft()
    draft.epics[0].title = "Supplier portal for drawing change acknowledgement"
    client = TestClient(create_app(settings, provider=Stub(draft), engine=engine))
    text = "Suppliers need drawing changes the day they are released."
    page = client.post("/decompose", data={"text": text}).text
    assert '<option value="supplier" selected>' in page
    client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                  "payload": payload_from(page), "source_text": source_from(page),
                                  "keep:0.0.0": "on", "product:0": "supplier"})
    supplier = roadmap.product("supplier")
    assert [f.title for f in tracker.feature_inputs(supplier.id)] == ["f"]
    assert "IN-1 approved" in client.get("/roadmap?product=supplier").text  # logged as new


def test_backlog_filters_by_product(client):
    page = client.get("/backlog?product=data-gov").text
    assert "Engineering data governance foundation" in page
    assert "PLM platform security hardening" not in page
