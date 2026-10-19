import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backlog_pilot.app import create_app
from backlog_pilot.intakes import ASSUMPTION, QUESTION
from backlog_pilot.models import Decomposition
from backlog_pilot.trackers.mock import BacklogItem
from tests.conftest import payload_from, source_from


def draft(client, text):
    response = client.post("/decompose", data={"text": text})
    assert response.status_code == 200
    return response.text


def test_pages_render(client):
    for path in ["/", "/intakes", "/backlog", "/audit"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "Backlog Pilot" in response.text
        assert "Styled with Brightlayer UI (open source)." in response.text
        assert "Eaton" not in response.text  # no company name on screen


def test_sample_backlog_is_seeded_once(client, tracker, settings):
    before = tracker.counts()
    assert before["epic"] == 3
    tracker.seed_if_empty(settings.sample_backlog_path, {})
    assert tracker.counts() == before


def test_end_to_end_draft_review_approve(client, tracker, intakes, scenarios):
    review = draft(client, scenarios[0]["text"])
    assert "Draft for review" in review
    response = client.post("/approve", data={
        "drop_reason": "Trimmed for this test", 
        "payload": payload_from(review),
        "source_text": source_from(review),
        "keep:0.0.0": "on",
        "keep:0.1.0": "on",
        "title:0.0.0": "Show ECO stage and owner",
    })
    assert response.status_code == 200
    assert "Pushed 5 items (2 stories)" in response.text
    assert "IN-1" in response.text

    titles = [s["item"].title for e in tracker.tree() for f in e["children"]
              for s in f["children"]]
    assert "Show ECO stage and owner" in titles

    view = intakes.view(1)
    assert view.intake.source_text == scenarios[0]["text"]
    # No assumption/question fields were posted, so every AI suggestion was removed in review.
    assert view.notes[ASSUMPTION] == [] and view.removed[ASSUMPTION]


def test_review_edits_to_notes_are_versioned_and_attributed(client, intakes, scenarios):
    review = draft(client, scenarios[0]["text"])
    decomposition = json.loads(payload_from(review))
    client.cookies.set("bp_user", "eng")
    client.post("/approve", data={
        "drop_reason": "Trimmed for this test", 
        "payload": payload_from(review),
        "source_text": source_from(review),
        "keep:0.0.0": "on",
        "assumption:0": decomposition["assumptions"][0],             # kept as-is
        "assumption:1": "Edited by the analyst",                     # edited
        # assumption:2.. omitted -> removed
        "assumption:new": "Plants run SAP S/4HANA",                  # added
        **{f"question:{i}": q for i, q in enumerate(decomposition["open_questions"])},
    })
    view = intakes.view(1)
    texts = [n.text for n in view.notes[ASSUMPTION]]
    assert texts == [decomposition["assumptions"][0], "Edited by the analyst",
                     "Plants run SAP S/4HANA"]
    assert len(view.notes[QUESTION]) == len(decomposition["open_questions"])

    edited = view.notes[ASSUMPTION][1]
    _, versions = intakes.history(edited.id)
    assert [(v.version, v.change) for v in versions] == [(2, "edited"), (1, "created")]
    assert versions[1].changed_by == "AI draft (recorded demo)"
    assert versions[1].text == decomposition["assumptions"][1]
    assert versions[0].changed_by == "Morgan Chen (Engineering Lead)"
    # Assumption edits made during review still flag the backlog for re-review.
    assert view.pending


def approved_intake(client, scenarios, index=0):
    review = draft(client, scenarios[index]["text"])
    decomposition = json.loads(payload_from(review))
    fields = {"payload": payload_from(review), "source_text": source_from(review),
              "keep:0.0.0": "on"}
    fields |= {f"assumption:{i}": a for i, a in enumerate(decomposition["assumptions"])}
    fields |= {f"question:{i}": q for i, q in enumerate(decomposition["open_questions"])}
    client.post("/approve", data=fields | {"drop_reason": "Trimmed for this test"})
    return 1


def test_note_lifecycle_through_the_ui(client, intakes, scenarios):
    intake_id = approved_intake(client, scenarios)
    page = client.get(f"/intakes/{intake_id}")
    assert page.status_code == 200 and "Stakeholder input" in page.text
    assert "may affect it" not in page.text  # untouched AI notes are not pending changes

    question = intakes.view(intake_id).notes[QUESTION][0]
    assert "Save version 2" in client.get(f"/notes/{question.id}/edit").text

    # Answering a question is material.
    response = client.post(f"/notes/{question.id}/edit",
                           data={"text": question.text, "answer": "Yes, all change classes",
                                 "reason": "Confirmed in steering meeting"})
    assert "may affect it" in response.text and "Yes, all change classes" in response.text

    # Rewording a question is captured but not material.
    other = intakes.view(intake_id).notes[QUESTION][1]
    client.post(f"/notes/{other.id}/edit", data={"text": other.text + " (by Friday)"})
    assert len(intakes.view(intake_id).pending) == 1

    # Remove with a reason (htmx sends it in HX-Prompt), then restore.
    assumption = intakes.view(intake_id).notes[ASSUMPTION][0]
    client.post(f"/notes/{assumption.id}/remove", headers={"HX-Prompt": "Out of scope"})
    assert assumption.id in [n.id for n in intakes.view(intake_id).removed[ASSUMPTION]]
    client.post(f"/notes/{assumption.id}/restore")
    history = client.get(f"/notes/{assumption.id}/history").text
    assert "restored" in history and "Out of scope" in history

    # Add a question; empty text is rejected with a message.
    client.post(f"/intakes/{intake_id}/notes", data={"kind": "question", "text": "Who funds it?"})
    assert "Who funds it?" in [n.text for n in intakes.view(intake_id).notes[QUESTION]]
    assert "Enter some text" in client.post(f"/intakes/{intake_id}/notes",
                                            data={"kind": "question", "text": " "}).text

    # Acknowledge: pending clears and the decision is logged.
    assert len(intakes.view(intake_id).pending) == 3  # answer, remove, restore
    client.post(f"/intakes/{intake_id}/acknowledge", headers={"HX-Prompt": "Stories hold"})
    assert intakes.view(intake_id).pending == []
    audit = client.get("/audit").text
    assert "changes_acknowledged" in audit and "Stories hold" in audit


def test_redraft_in_offline_mode_explains_live_mode_is_needed(client, intakes, scenarios):
    intake_id = approved_intake(client, scenarios)
    response = client.post(f"/intakes/{intake_id}/redraft")
    assert "needs live AI mode" in response.text


def test_redraft_approval_supersedes_previous_items(client, tracker, intakes, scenarios):
    intake_id = approved_intake(client, scenarios)
    # Simulate an approved re-draft (live mode would generate it) by reusing the draft.
    review = draft(client, scenarios[0]["text"])
    response = client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                             "payload": payload_from(review),
                                             "intake_id": str(intake_id), "keep:0.0.0": "on"})
    assert "marked Superseded" in response.text
    assert tracker.counts()["superseded"] == 3  # the first draft's epic, feature, and story
    assert intakes.view(intake_id).intake.draft_count == 2
    assert "Show superseded" in client.get("/backlog").text


def test_switching_user_sets_cookie_and_stays_on_site(client):
    response = client.post("/user", data={"user_id": "eng"},
                           headers={"referer": "http://evil.example//phish"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "bp_user=eng" in response.headers["set-cookie"]


def test_approve_with_nothing_selected_is_rejected(client, tracker, scenarios):
    review = draft(client, scenarios[0]["text"])
    before = tracker.counts()
    response = client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                             "payload": payload_from(review)})
    assert "Nothing selected" in response.text
    assert tracker.counts() == before


def test_approve_with_tampered_payload_is_rejected(client):
    response = client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                             "payload": '{"not": "a draft"}'})
    assert "could not be read" in response.text


def test_unknown_input_in_offline_mode_shows_error(client):
    response = client.post("/decompose", data={"text": "A brand new request " * 5})
    assert "Offline demo mode" in response.text


def test_only_approvers_can_make_decisions(client, tracker, scenarios):
    review = draft(client, scenarios[0]["text"])
    client.cookies.set("bp_user", "stake")  # Director of Engineering: not an approver
    before = tracker.counts()
    response = client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                             "payload": payload_from(review), "keep:0.0.0": "on"})
    assert response.status_code == 403 and "Only approvers" in response.text
    assert tracker.counts() == before
    key = tracker.ranked_features()[0]["item"].key
    for path, data in [(f"/features/{key}/wsjf", {"business_value": "1"}),
                       ("/roadmap/plm/capacity", {}), (f"/features/{key}/pin", {}),
                       ("/sync/retry", {})]:
        assert client.post(path, data=data).status_code == 403, path
    client.cookies.set("bp_user", "po")
    log = client.get("/audit?category=access").text
    assert log.count("access_denied") >= 5 and f"/features/{key}/pin" in log
    client.cookies.set("bp_user", "stake")
    # Non-approvers can still draft and work on assumptions and questions.
    assert "Draft for review" in draft(client, scenarios[0]["text"])


def test_security_headers(client):
    headers = client.get("/static/css/app.css").headers
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"


SUITE = ("Backlog Pilot=http://bp, Leadership Pilot=http://lp, PLM Insight=http://pi, "
         "Future App=http://sp")


def test_launcher_menu_renders(settings, engine, monkeypatch):
    monkeypatch.setenv("MISSION_CONTROL_APPS", SUITE)
    page = TestClient(create_app(settings, engine=engine)).get("/").text
    assert 'class="suite-menu"' in page and 'href="http://sp"' in page
    assert 'aria-current=page' in page



def test_dropping_drafted_items_needs_a_reason_and_rewording_keeps_the_ai_title(
        client, tracker, engine, scenarios):
    review = draft(client, scenarios[0]["text"])
    ai_title = Decomposition.model_validate_json(payload_from(review)).epics[0] \
        .features[0].stories[0].title
    form = {"payload": payload_from(review), "source_text": source_from(review),
            "keep:0.0.0": "on", "title:0.0.0": "Show BOM transfer errors to planners"}
    before = tracker.counts()
    refused = client.post("/approve", data=form).text
    assert "the AI drafted" in refused and "Say why" in refused
    assert tracker.counts() == before  # nothing was pushed
    client.post("/approve", data=form | {"drop_reason": "Only the planner story is in scope"})
    with Session(engine) as s:
        story = s.exec(select(BacklogItem).where(
            BacklogItem.title == "Show BOM transfer errors to planners")).one()
    assert story.details["ai_title"] == ai_title
    log = client.get("/audit").text
    assert "draft_items_dropped" in log and "Only the planner story is in scope" in log
    assert "draft_title_edited" in log
