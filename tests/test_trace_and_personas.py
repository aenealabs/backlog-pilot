import pytest
from fastapi.testclient import TestClient

from backlog_pilot.app import create_app
from backlog_pilot.models import Decomposition
from backlog_pilot.personas import PersonaError, PersonaStore
from backlog_pilot.pipeline.trace import (
    INFERRED,
    TRACED,
    UNVERIFIED,
    coverage,
    find_spans,
    highlight,
    trace_story,
)
from tests.conftest import payload_from, source_from

SOURCE = ("Our ECOs take 19 business days.  Engineers have no idea where a change is stuck, "
          "and approvers get flooded with emails.")


# ---------- quote verification ----------

def test_find_spans_tolerates_case_whitespace_and_edge_punctuation():
    assert find_spans(SOURCE, '"engineers have no idea where a change is stuck,"')
    assert find_spans(SOURCE, "take 19 business days. Engineers")  # collapses double space


def test_find_spans_rejects_paraphrase_and_checks_ellipsis_parts_in_order():
    assert find_spans(SOURCE, "engineers cannot tell where changes are stuck") is None
    assert find_spans(SOURCE, "Our ECOs ... flooded with emails")
    assert find_spans(SOURCE, "flooded with emails ... Our ECOs") is None


def test_trace_story_statuses():
    assert trace_story(SOURCE, ["approvers get flooded with emails"]).status == TRACED
    assert trace_story(SOURCE, ["approvers love email"]).status == UNVERIFIED
    assert trace_story(SOURCE, []).status == INFERRED
    mixed = trace_story(SOURCE, ["approvers love email", "19 business days"])
    assert mixed.status == TRACED and mixed.quotes == [("approvers love email", False),
                                                       ("19 business days", True)]


def test_highlight_splits_source_and_tags_overlapping_citations():
    segments = highlight(SOURCE, {"0.0.0": ["19 business days"],
                                  "0.0.1": ["business days.  Engineers"]})
    assert "".join(s.text for s in segments) == SOURCE
    tagged = {s.text: s.paths for s in segments if s.paths}
    assert tagged["19 "] == ["0.0.0"]
    assert tagged["business days"] == ["0.0.0", "0.0.1"]
    assert tagged[".  Engineers"] == ["0.0.1"]


def test_coverage_counts():
    traces = {"a": trace_story(SOURCE, ["19 business days"]), "b": trace_story(SOURCE, [])}
    assert coverage(traces) == {TRACED: 1, UNVERIFIED: 0, INFERRED: 1}


# ---------- persona store ----------

@pytest.fixture
def personas(engine, settings):
    store = PersonaStore(engine)
    store.seed_if_empty(settings.personas_path)
    return store


def test_seeded_library_and_versioned_edits(personas):
    assert "eco approver" in personas.approved_names()
    eco = next(p for p in personas.all_personas() if p.name == "eco approver")
    personas.edit(eco.id, "Jordan Lee (Product Owner)", "ECO Approver", eco.description,
                  "Approve fast\n- See impact up front", "", reason="tightened")
    persona, versions = personas.history(eco.id)
    assert persona.version == 2 and persona.goal_list == ["Approve fast", "See impact up front"]
    assert [v.change for v in versions] == ["edited", "created"]
    assert versions[0].reason == "tightened"


def test_persona_name_clash_and_retire(personas):
    with pytest.raises(PersonaError, match="already exists"):
        personas.add("x", "Design Engineer", "dup", "", "")
    eco = next(p for p in personas.all_personas() if p.name == "eco approver")
    personas.set_status(eco.id, "x", "retired", "merged")
    assert "eco approver" not in personas.approved_names()
    assert "eco approver" not in [b.name for b in personas.briefs()]


# ---------- end to end with a stub model ----------

DRAFT = Decomposition.model_validate({
    "summary": "Cut ECO cycle time.",
    "epics": [{"title": "Faster ECOs", "description": "d", "business_outcome": "7 days",
               "features": [{"title": "Visibility", "description": "d", "stories": [
                   {"title": "See where my ECO is", "as_a": "design engineer", "i_want": "x",
                    "so_that": "y", "acceptance_criteria": ["Given a When b Then c"],
                    "source_quotes": ["no idea where a change is stuck"]},
                   {"title": "Audit approvals", "as_a": "ECO approver", "i_want": "x",
                    "so_that": "y", "acceptance_criteria": ["Given a When b Then c"],
                    "source_quotes": []},
                   {"title": "Supplier view", "as_a": "supplier quality engineer",
                    "i_want": "x", "so_that": "y", "acceptance_criteria": ["Given a"],
                    "source_quotes": ["suppliers are unhappy"]}]}]}],
    "assumptions": ["A"], "open_questions": ["Q"],
    "proposed_personas": [{"name": "Supplier Quality Engineer",
                           "description": "Owns supplier PPAP.",
                           "goals": ["Fewer escapes"], "pain_points": ["Late ECO notice"]}],
})


class StubProvider:
    name = "stub"

    def __init__(self):
        self.personas = None

    def decompose(self, text, context=None):
        self.personas = list(context.personas)
        return DRAFT


def test_review_shows_traces_and_personas_then_accepts_proposal(settings, engine):
    stub = StubProvider()
    client = TestClient(create_app(settings, provider=stub, engine=engine))
    review = client.post("/decompose", data={"text": SOURCE}).text

    assert "eco approver" in [p.name for p in stub.personas]  # library sent to the model
    assert "1 traced to input" in review
    assert "1 AI inference" in review and "1 quote not found" in review
    assert '<mark data-paths="0.0.0">no idea where a change is stuck</mark>' in review
    assert "supplier quality engineer · proposed" in review
    assert "New personas proposed" in review

    client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                  "payload": payload_from(review),
                                  "source_text": source_from(review),
                                  "keep:0.0.0": "on", "keep:0.0.1": "on", "keep:0.0.2": "on",
                                  "persona:0": "on"})
    page = client.get("/personas").text
    assert "supplier quality engineer" in page
    assert "Owns supplier PPAP." in page

    intake = client.get("/intakes/1").text
    assert "1 of 3 stories traced to this input" in intake
    backlog = client.get("/backlog").text
    assert "AI inference" in backlog and "Quote not found" in backlog


def test_unticked_proposal_is_not_added(settings, engine):
    client = TestClient(create_app(settings, provider=StubProvider(), engine=engine))
    review = client.post("/decompose", data={"text": SOURCE}).text
    client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                  "payload": payload_from(review), "keep:0.0.0": "on"})
    assert "Owns supplier PPAP." not in client.get("/personas").text


def test_persona_routes(settings, engine):
    client = TestClient(create_app(settings, engine=engine))
    assert client.post("/personas", data={"name": "Test Lead", "description": "Tests."},
                       follow_redirects=False).status_code == 303
    assert "already exists" in client.post("/personas", data={"name": "test lead",
                                                              "description": "x"}).text
    page = client.get("/personas").text
    assert "test lead" in page
