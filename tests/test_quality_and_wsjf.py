import pytest
from fastapi.testclient import TestClient

from backlog_pilot.app import create_app
from backlog_pilot.models import Decomposition, UserStory, WsjfEstimate
from backlog_pilot.pipeline.decompose import apply_edits
from backlog_pilot.pipeline.quality import check_story, is_ready, nfr_coverage
from backlog_pilot.pipeline.trace import trace_story
from tests.conftest import payload_from, source_from

PERSONAS = {"design engineer"}
SOURCE = "Suppliers build to old revisions. Some drawings are ITAR controlled."


def story(**overrides) -> UserStory:
    fields = dict(title="See my ECO", as_a="design engineer", i_want="to see where my ECO is",
                  so_that="I can follow up",
                  acceptance_criteria=["Given an ECO When I open it Then I see its step",
                                       "Given a stuck ECO When it ages Then it is flagged"],
                  source_quotes=["Suppliers build to old revisions"])
    return UserStory(**(fields | overrides))


def codes(s: UserStory, source: str = SOURCE) -> set[str]:
    return {i.code for i in check_story(s, trace_story(source, s.source_quotes), PERSONAS)}


# ---------- story checks ----------

def test_clean_story_is_ready():
    s = story()
    assert codes(s) == set()
    assert is_ready(check_story(s, trace_story(SOURCE, s.source_quotes), PERSONAS))


@pytest.mark.parametrize("overrides, code", [
    ({"acceptance_criteria": ["Given a When b Then c"]}, "few_criteria"),
    ({"acceptance_criteria": ["Given a When b Then c"] * 6}, "many_criteria"),
    ({"acceptance_criteria": ["Given a When b Then c", "The page loads"]}, "not_gherkin"),
    ({"acceptance_criteria": ["Given a When b Then it is fast", "Given a When b Then c"]},
     "untestable"),
    ({"i_want": "search and filter and export parts"}, "compound"),
    ({"so_that": " "}, "no_outcome"),
    ({"as_a": "supplier quality engineer"}, "persona"),
    ({"source_quotes": ["suppliers are thrilled"]}, "misquote"),
    ({"source_quotes": []}, "inferred"),
])
def test_each_rule_fires(overrides, code):
    assert code in codes(story(**overrides))


def test_info_findings_do_not_block_ready():
    s = story(as_a="supplier quality engineer", source_quotes=[])
    issues = check_story(s, trace_story(SOURCE, []), PERSONAS)
    assert {i.code for i in issues} == {"persona", "inferred"} and is_ready(issues)


# ---------- non-functional coverage ----------

def draft_with(categories: list[str]) -> Decomposition:
    return Decomposition.model_validate({
        "summary": "s", "assumptions": [], "open_questions": [],
        "epics": [{"title": "e", "description": "d", "business_outcome": "o", "features": [
            {"title": "f", "description": "d",
             "stories": [story(nfr_categories=categories).model_dump()]}]}]})


def test_coverage_flags_a_need_the_input_implies_but_no_story_covers():
    rows = {c.category: c for c in nfr_coverage(SOURCE, draft_with([]))}
    assert rows["export_control"].gap
    assert not rows["security"].mentioned
    rows = {c.category: c for c in nfr_coverage(SOURCE, draft_with(["export_control"]))}
    assert not rows["export_control"].gap and rows["export_control"].covered


def test_coverage_counts_only_kept_stories():
    rows = {c.category: c for c in nfr_coverage(SOURCE, draft_with(["export_control"]),
                                                kept=set())}
    assert rows["export_control"].gap


# ---------- WSJF ----------

def test_wsjf_score():
    w = WsjfEstimate(business_value=8, time_criticality=5, risk_reduction=3, job_size=5,
                     rationale="r")
    assert w.cost_of_delay == 16 and w.score == 3.2


def scored_draft() -> Decomposition:
    d = draft_with([])
    d.epics[0].features[0].wsjf = WsjfEstimate(business_value=8, time_criticality=5,
                                               risk_reduction=3, job_size=5, rationale="AI")
    return d


def test_apply_edits_adjusts_wsjf_and_keeps_the_ai_original():
    edited = apply_edits(scored_draft(), {"keep:0.0.0": "on", "wsjf:0.0:job_size": "13",
                                          "wsjf:0.0:business_value": "7"})  # 7 is off-scale
    feature = edited.epics[0].features[0]
    assert feature.wsjf.job_size == 13 and feature.wsjf.business_value == 8
    assert feature._ai_wsjf.job_size == 5


# ---------- end to end ----------

class StubProvider:
    name = "stub"

    def __init__(self):
        self.context = None

    def decompose(self, text, context=None):
        self.context = context
        return scored_draft()


@pytest.fixture
def stub():
    return StubProvider()


@pytest.fixture
def client(settings, engine, stub):
    return TestClient(create_app(settings, provider=stub, engine=engine))


def approve(client, wsjf_overrides=None):
    review = client.post("/decompose", data={"text": SOURCE}).text
    data = {"payload": payload_from(review), "source_text": source_from(review),
            "keep:0.0.0": "on", "drop_reason": "Trimmed for this test"} | (wsjf_overrides or {})
    client.post("/approve", data=data)
    return review


def test_review_shows_quality_and_wsjf(client):
    review = approve(client)
    assert "1 of 1 stories ready" in review
    assert "Export control (ITAR/EAR) · gap" in review
    assert 'name="wsjf:0.0:job_size"' in review and ">3.2<" in review


def test_adjusted_wsjf_is_ranked_with_history_and_audited(client, tracker):
    approve(client, {"wsjf:0.0:job_size": "2"})  # changed the AI's score without a reason
    assert "f" not in [r["item"].title for r in tracker.ranked_features()]  # refused
    approve(client, {"wsjf:0.0:job_size": "2", "wsjf_reason:0.0": "Reuses the SAP adapter"})
    top = tracker.ranked_features()[0]
    assert top["score"] == 8.0 and top["item"].title == "f"
    history = top["item"].details["wsjf_history"]
    assert [h["change"] for h in history] == ["drafted", "adjusted"]
    assert history[1]["reason"] == "Reuses the SAP adapter"
    assert history[0]["score"] == 3.2 and history[0]["by"].startswith("AI draft")
    assert "wsjf_adjusted" in client.get("/audit").text
    page = client.get("/priorities").text
    assert "AI: 3.2" in page and "Not scored" in page  # one sample feature stays unscored


def test_rescoring_on_priorities_page(client, tracker):
    approve(client)
    key = tracker.ranked_features()[0]["item"].key
    assert "Save" in client.get(f"/priorities?edit={key}").text
    client.post(f"/features/{key}/wsjf", data={"business_value": "20", "time_criticality": "13",
                                               "risk_reduction": "8", "job_size": "3",
                                               "reason": "Plant escalation"})
    row = tracker.ranked_features()[0]
    assert row["score"] == 13.7 and row["last"]["reason"] == "Plant escalation"
    assert "wsjf_rescored" in client.get("/audit").text
    no_reason = client.post(f"/features/{key}/wsjf", data={
        "business_value": "8", "time_criticality": "13", "risk_reduction": "8", "job_size": "3"})
    assert "Say why" in no_reason.text and tracker.ranked_features()[0]["score"] == 13.7
    bad = client.post(f"/features/{key}/wsjf", data={"business_value": "4",
                                                     "time_criticality": "1",
                                                     "risk_reduction": "1", "job_size": "1"})
    assert "scale" in bad.text


def test_scored_features_calibrate_the_next_draft(client, stub, tracker):
    approve(client)
    client.post("/decompose", data={"text": SOURCE})
    titles = [f.title for f in stub.context.scored_features]
    assert "f" in titles and "SAP material and BOM upsert service" in titles
    assert "design engineer" in [p.name for p in stub.context.personas]
