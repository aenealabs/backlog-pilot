import re

import pytest
from fastapi.testclient import TestClient

from backlog_pilot.app import create_app
from backlog_pilot.models import Decomposition
from backlog_pilot.pipeline.similarity import Candidate, find_overlaps, tokens
from tests.conftest import payload_from, source_from


def test_tokens_drop_stopwords_and_stem():
    assert tokens("The transfers of BOMs to SAP") == ["transfer", "bom", "sap"]


def draft(feature_title: str, feature_desc: str, story_title: str = "Story",
          i_want: str = "something") -> Decomposition:
    return Decomposition.model_validate({
        "summary": "s", "assumptions": [], "open_questions": [],
        "epics": [{"title": "e", "description": "d", "business_outcome": "o", "features": [{
            "title": feature_title, "description": feature_desc, "stories": [{
                "title": story_title, "as_a": "design engineer", "i_want": i_want,
                "so_that": "y", "acceptance_criteria": ["Given a When b Then c"] * 2}]}]}]})


CANDIDATES = [
    Candidate("BP-103", "feature", "SAP material and BOM upsert service",
              "SAP material and BOM upsert service Consume release events and create or "
              "update materials and BOMs in SAP.", "In Progress"),
    Candidate("BP-106", "feature", "Part classification standard",
              "Part classification standard Common classification schema.", "Done"),
    Candidate("BP-200", "story", "Retry failed BOM transfers",
              "Retry failed BOM transfers to retry a failed BOM transfer to SAP", "New"),
]


def test_finds_overlapping_feature_and_ignores_unrelated():
    overlaps = find_overlaps(
        draft("Event-driven BOM transfer to SAP",
              "Create or update SAP materials and BOMs when a release event fires."),
        CANDIDATES)
    keys = [m.key for m in overlaps["0.0"]]
    assert keys[0] == "BP-103" and "BP-106" not in keys


def test_features_match_features_and_stories_match_stories():
    overlaps = find_overlaps(
        draft("Unrelated reporting", "Weekly dashboards", "Retry failed BOM transfers",
              "to retry a failed BOM transfer to SAP"), CANDIDATES)
    assert "0.0" not in overlaps
    assert [m.key for m in overlaps["0.0.0"]] == ["BP-200"]
    assert overlaps["0.0.0"][0].likely


def test_no_candidates_no_overlaps():
    assert find_overlaps(draft("x", "y"), []) == {}


# ---------- end to end against the seeded sample backlog ----------

DRAFT = draft("SAP BOM upsert on release",
              "Create or update materials and BOMs in SAP when a release event fires.")


class Stub:
    name = "stub"

    def decompose(self, text, context=None):
        return DRAFT


@pytest.fixture
def client(settings, engine):
    return TestClient(create_app(settings, provider=Stub(), engine=engine))


def review(client):
    return client.post("/decompose", data={"text": "BOMs reach SAP a day late after release."}).text


def test_review_flags_overlap_with_seeded_feature(client):
    page = review(client)
    assert "1 item may overlap existing work" in page
    key = re.search(r'name="related:0\.0:(BP-\d+)"', page).group(1)
    assert "SAP material and BOM upsert service" in page and key == "BP-103"


def test_ticked_overlap_is_stored_as_related_and_audited(client, tracker):
    page = review(client)
    client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                  "payload": payload_from(page), "source_text": source_from(page),
                                  "keep:0.0.0": "on", "related:0.0:BP-103": "on"})
    feature = next(r["item"] for r in tracker.ranked_features()
                   if r["item"].title == "SAP BOM upsert on release")
    assert feature.details["related"] == ["BP-103"]
    assert "linked_related" in client.get("/audit").text
    assert "BP-103" in client.get("/backlog").text


def test_unticked_overlap_is_not_stored(client, tracker):
    page = review(client)
    client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                  "payload": payload_from(page), "keep:0.0.0": "on"})
    feature = next(r["item"] for r in tracker.ranked_features()
                   if r["item"].title == "SAP BOM upsert on release")
    assert feature.details["related"] == []


def test_redraft_does_not_flag_its_own_items(client, tracker):
    page = review(client)
    client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                  "payload": payload_from(page), "source_text": source_from(page),
                                  "keep:0.0.0": "on"})
    new = "SAP BOM upsert on release"
    assert any(c.title == new for c in tracker.candidates())
    assert not any(c.title == new for c in tracker.candidates(exclude_intake=1))
