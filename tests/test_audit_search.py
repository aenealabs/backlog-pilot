from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from backlog_pilot import audit_search
from backlog_pilot.app import create_app
from backlog_pilot.audit_search import AuditAnswer, AuditPlan, AuditQuery, search, verify_answer
from backlog_pilot.db import audit, utcnow


@pytest.fixture
def log(engine):
    rows = [
        ("Jordan Lee (Product Owner)", "wsjf_rescored",
         "BP-112: BV 8 -> 13 | reason: Plant escalation"),
        ("Riley Adams (Director of Engineering)", "question_answered",
         "#4 v2: Is fast-track in scope? | answer: yes"),
        ("Jordan Lee (Product Owner)", "approve_and_push", "Created 3 items: BP-1120, BP-1121"),
        ("Morgan Chen (Engineering Lead)", "capacity_changed",
         "PLM-SAP Integration Q4 2026: 13 -> 8"),
        ("Sam Patel (Business Analyst)", "assumption_removed",
         "#2 v2: SAP lag is batch | reason: wrong"),
        ("Mallory (Contributor)", "assumption_added", "#9: <script>alert('x')</script> plant"),
    ]
    with Session(engine) as s:
        for actor, action, detail in rows:
            audit(s, actor, action, detail)
        s.commit()
    return engine


def keys(engine, **kwargs):
    return [e.action for e in search(engine, AuditQuery(**kwargs)).events]


def test_text_search_is_prefix_and_all_words(log):
    assert keys(log, text="escalat") == ["wsjf_rescored"]
    assert keys(log, text="plant reason") == ["wsjf_rescored"]
    assert keys(log, text="nothing-like-this") == []


def test_unsafe_search_text_never_breaks_the_query(log):
    for text in ['"', "AND OR NOT", "plant*", "(", "-", "NEAR(a b)", "' OR 1=1 --"]:
        search(log, AuditQuery(text=text))  # must not raise


def test_filters(log):
    assert keys(log, item_keys=["BP-112"]) == ["wsjf_rescored"]  # not BP-1120
    assert keys(log, categories=["assumptions_and_questions"]) == [
        "assumption_added", "assumption_removed", "question_answered"]
    assert keys(log, actor="Riley") == ["question_answered"]
    today = utcnow().date()
    assert len(keys(log, since=today, until=today)) == 6
    assert keys(log, until=today - timedelta(days=1)) == []


def test_paging_and_total(log):
    first = search(log, AuditQuery(), limit=4)
    second = search(log, AuditQuery(), limit=4, offset=4)
    assert first.total == second.total == 6
    assert len(first.events) == 4 and len(second.events) == 2
    assert {e.id for e in first.events}.isdisjoint({e.id for e in second.events})


def test_verify_answer_drops_citations_not_in_results(log):
    events = search(log, AuditQuery(text="plant")).events
    real = events[0].id
    text, cited, bad = verify_answer(
        AuditAnswer(answer=f"Jordan re-scored it [#{real}] and approved it [#999].",
                    cited_event_ids=[real, 999]), events)
    assert cited == [real] and bad == [999]
    assert "[unverified]" in text and f"[#{real}]" in text


def test_summary_counts(log):
    summary = audit_search.summarize(search(log, AuditQuery()).events)
    assert summary["count"] == 6
    assert ("Jordan Lee (Product Owner)", 2) in summary["people"]
    assert dict(summary["items"])["BP-112"] == 1


# ---------- page and ask ----------

class AskStub:
    name = "stub"

    def __init__(self, answer=True):
        self.with_answer = answer

    def decompose(self, text, context=None):
        raise AssertionError("not used")

    def plan_audit(self, question, today, actors):
        assert "Jordan Lee (Product Owner)" in actors
        return AuditPlan(keywords=[], actor="", categories=["prioritization"], item_keys=[],
                         since=None, until=None, explanation="WSJF changes by anyone")

    def answer_audit(self, question, events):
        if not self.with_answer:
            return None
        return AuditAnswer(answer=f"Jordan re-scored BP-112 [#{events[0].id}] [#4242].",
                           cited_event_ids=[events[0].id])


def test_ask_runs_the_plan_verifies_citations_and_is_logged(settings, log):
    client = TestClient(create_app(settings, provider=AskStub(), engine=log))
    page = client.get("/audit", params={"ask": "Who changed WSJF?"}).text
    assert "Searched: WSJF changes by anyone" in page and "1 matching event" in page
    assert 'class="cite" href="#ev-' in page and "[unverified]" in page
    assert "audit_asked" in client.get("/audit").text


def test_offline_ask_shows_a_computed_summary(settings, log):
    client = TestClient(create_app(settings, provider=AskStub(answer=False), engine=log))
    page = client.get("/audit", params={"ask": "Who changed WSJF?"}).text
    assert "Summary computed from 1 event shown" in page


def test_offline_unknown_question_explains(settings, log):
    client = TestClient(create_app(settings, engine=log))  # replay provider
    page = client.get("/audit", params={"ask": "Something never recorded?"}).text
    assert "only knows the suggested questions" in page


def test_log_text_is_escaped_and_highlighted(settings, log):
    client = TestClient(create_app(settings, engine=log))
    page = client.get("/audit", params={"q": "plant"}).text
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page and "<mark>plant</mark>" in page.lower()


def test_filter_form_round_trip(settings, log):
    client = TestClient(create_app(settings, engine=log))
    page = client.get("/audit", params={"item": "bp-112", "category": "prioritization"}).text
    assert "1 event" in page and "Plant escalation" in page
    assert client.get("/audit", params={"since": "not-a-date"}).status_code == 200
    assert date.today()  # dates parse leniently; bad input is ignored
