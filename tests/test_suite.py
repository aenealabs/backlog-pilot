import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backlog_pilot.app import create_app
from backlog_pilot.db import AuditEvent
from backlog_pilot.personas import PersonaStore
from backlog_pilot.suite import SuiteSettings
from tests.conftest import payload_from, source_from

SUITE = SuiteSettings(token="suite-secret", leadership_url="http://leadership.example")
AUTH = {"Authorization": "Bearer suite-secret"}


@pytest.fixture
def client(settings, engine):
    return TestClient(create_app(settings, engine=engine, suite=SUITE))


def problem(scenarios, **overrides):
    return {"title": "ECO approvals are too slow", "text": scenarios[0]["text"],
            "source_ref": "PR-3", "source_url": "http://leadership.example/problems/3",
            "stakeholders": ["Priya Raman", "Kenji Watanabe"],
            "submitted_by": "Jordan Lee"} | overrides


def test_api_is_off_without_a_token_and_rejects_wrong_tokens(settings, engine, scenarios):
    off = TestClient(create_app(settings, engine=engine, suite=SuiteSettings()))
    assert off.post("/api/inbox", json=problem(scenarios)).status_code == 401
    on = TestClient(create_app(settings, engine=engine, suite=SUITE))
    assert on.post("/api/inbox", json=problem(scenarios),
                   headers={"Authorization": "Bearer nope"}).status_code == 401
    assert on.post("/api/inbox", json=problem(scenarios)).status_code == 401
    with Session(engine) as s:  # refused calls are audited
        denied = s.exec(select(AuditEvent).where(AuditEvent.action == "access_denied")).all()
    assert len(denied) == 3 and all(e.actor == "suite API" for e in denied)


def test_inbox_item_becomes_a_linked_intake(client, scenarios):
    created = client.post("/api/inbox", json=problem(scenarios), headers=AUTH)
    assert created.status_code == 201
    item_id = created.json()["id"]
    page = client.get("/").text
    assert "Inbox" in page and "PR-3" in page and "Priya Raman, Kenji Watanabe" in page
    assert "Mission Control" in page and 'href="http://leadership.example"' in page

    review = client.post("/decompose", data={"text": scenarios[0]["text"],
                                             "inbox_id": str(item_id)}).text
    assert f'name="inbox_id" value="{item_id}"' in review
    client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                  "payload": payload_from(review),
                                  "source_text": source_from(review),
                                  "inbox_id": str(item_id), "keep:0.0.0": "on"})
    status = client.get(f"/api/inbox/{item_id}", headers=AUTH).json()
    assert status["status"] == "drafted" and status["intake_key"] == "IN-1"
    assert "PR-3" not in client.get("/").text  # no longer waiting in the inbox
    intake = client.get(status["intake_path"]).text
    assert "From Leadership Pilot" in intake and "problems/3" in intake
    audit = client.get("/audit").text
    assert "inbox_received" in audit and "inbox_drafted" in audit


def test_inbox_validation_and_dismissal(client, scenarios):
    bad = client.post("/api/inbox", json=problem(scenarios, text="too short"), headers=AUTH)
    assert bad.status_code == 422
    item_id = client.post("/api/inbox", json=problem(scenarios), headers=AUTH).json()["id"]
    client.post(f"/inbox/{item_id}/dismiss", headers={"HX-Prompt": "Covered by IN-1"})
    assert client.get(f"/api/inbox/{item_id}", headers=AUTH).json()["status"] == "dismissed"
    assert "Covered by IN-1" in client.get("/audit").text


def test_proposed_personas_wait_for_approval(client, engine):
    body = {"name": "Plant Quality Lead", "description": "Owns quality at a plant.",
            "goals": ["Fewer escapes"], "pain_points": ["Late BOMs"], "submitted_by": "Jordan Lee"}
    created = client.post("/api/personas/proposals", json=body, headers=AUTH)
    assert created.status_code == 201 and created.json()["status"] == "proposed"
    store = PersonaStore(engine)
    assert "plant quality lead" not in store.approved_names()
    assert client.post("/api/personas/proposals", json=body, headers=AUTH).status_code == 409
    page = client.get("/personas").text
    assert "Proposed" in page and ">Approve</button>" in page

    pid = created.json()["id"]
    client.post(f"/personas/{pid}/status", data={"status": "approved"})
    assert "plant quality lead" in store.approved_names()
    _, versions = store.history(pid)
    assert [v.change for v in versions] == ["approved", "proposed"]
    assert versions[1].changed_by == "Jordan Lee via Leadership Pilot"

    other = client.post("/api/personas/proposals", headers=AUTH,
                        json=body | {"name": "Supplier Portal Admin"}).json()["id"]
    refused = client.post(f"/personas/{other}/status", data={"status": "rejected"})
    assert "Say why" in refused.text and len(store.history(other)[1]) == 1
    client.post(f"/personas/{other}/status",
                data={"status": "rejected", "reason": "Same people as Design engineer"})
    assert store.history(other)[1][0].change == "rejected"
    assert store.history(other)[1][0].reason == "Same people as Design engineer"
    assert "supplier portal admin" not in store.approved_names()


def test_portfolio_api_is_read_only_roadmap_data(client):
    assert client.get("/api/portfolio").status_code == 401
    data = client.get("/api/portfolio", headers=AUTH).json()
    assert len(data["quarters"]) == 4 and data["source"] == "Backlog Pilot"
    by_key = {p["key"]: p for p in data["products"]}
    assert {"plm", "plm-sap", "data-gov"} <= set(by_key)
    sap = by_key["plm-sap"]
    assert set(sap["capacity"]) == set(data["quarters"])
    assert any(f["status"] == "In Progress" and f["quarter"] == data["quarters"][0]
               for f in sap["features"])
    assert sap["done"] and isinstance(sap["changes"], list)


def test_inbox_names_the_sending_app(client, scenarios):
    client.post("/api/inbox", headers=AUTH, json=problem(
        scenarios, source_ref="PRB-1", source_app="Support Pilot", stakeholders=[],
        source_url="http://support.example/problems/PRB-1"))
    page = client.get("/").text
    assert "Open in Support Pilot" in page and "From Support Pilot" in page
    assert "raised by" not in page.split("PRB-1")[1].split("</li>")[0]  # no stakeholders named
