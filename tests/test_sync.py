import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backlog_pilot.app import create_app
from backlog_pilot.sync import (
    AzureDevOpsTracker,
    ExternalRef,
    JiraTracker,
    SyncError,
    SyncItem,
    _types,
)
from tests.conftest import payload_from, source_from

FEATURE = SyncItem(key="BP-120", item_type="feature", title="Same-day BOM to SAP",
                   description="Transfer BOMs on release.",
                   wsjf={"business_value": 13, "time_criticality": 8, "risk_reduction": 5,
                         "job_size": 8, "rationale": "r"}, product="PLM-SAP Integration")
STORY = SyncItem(key="BP-121", item_type="story", title="Transfer on release",
                 description="As a PLM administrator, I want X, so that Y.",
                 acceptance_criteria=["Given A When B Then C", "Given D When E Then F"])
EPIC_REF = ExternalRef("jira", "10001", "PRJ-1", "https://jira.example/browse/PRJ-1")


def recorder(response_json: dict, status: int = 201):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=response_json)

    return seen, httpx.MockTransport(handler)


# ---------- Jira ----------

def test_jira_cloud_request_shape():
    seen, transport = recorder({"id": "10002", "key": "PRJ-2", "self": "x"})
    jira = JiraTracker("https://jira.example/", "PRJ", "tok", email="a@b.com",
                       transport=transport)
    ref = jira.create(FEATURE, EPIC_REF)
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/rest/api/2/issue"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"a@b.com:tok").decode()
    fields = json.loads(req.content)["fields"]
    assert fields["project"] == {"key": "PRJ"} and fields["issuetype"] == {"name": "Feature"}
    assert fields["parent"] == {"key": "PRJ-1"}
    assert fields["labels"] == ["backlog-pilot", "PLM-SAP Integration"]
    assert "h3. WSJF" in fields["description"] and "Job size: 8" in fields["description"]
    assert "BP-120" in fields["description"]
    assert ref == ExternalRef("jira", "10002", "PRJ-2", "https://jira.example/browse/PRJ-2")


def test_jira_data_center_uses_bearer_token_and_epic_link_field():
    seen, transport = recorder({"id": "3", "key": "PRJ-3"})
    jira = JiraTracker("https://jira.example", "PRJ", "pat", epic_link_field="customfield_10014",
                       transport=transport)
    jira.create(STORY, EPIC_REF)
    req = seen[0]
    assert req.headers["authorization"] == "Bearer pat"
    fields = json.loads(req.content)["fields"]
    assert fields["customfield_10014"] == "PRJ-1" and "parent" not in fields
    assert "* Given A When B Then C" in fields["description"]


def test_jira_error_is_a_sync_error():
    _, transport = recorder({"errors": {"issuetype": "invalid"}}, status=400)
    jira = JiraTracker("https://jira.example", "PRJ", "t", email="e", transport=transport)
    with pytest.raises(SyncError, match="HTTP 400"):
        jira.create(STORY, None)


def test_type_overrides():
    assert _types("story=Product Backlog Item, bogus=X", {"epic": "Epic", "story": "User Story"}) \
        == {"epic": "Epic", "story": "Product Backlog Item"}


# ---------- Azure DevOps ----------

def test_ado_request_shape():
    seen, transport = recorder({"id": 42, "_links": {"html": {"href": "https://dev/42"}}}, 200)
    ado = AzureDevOpsTracker("https://dev.azure.com/org/", "Eng", "pat", transport=transport)
    ref = ado.create(FEATURE, ExternalRef("ado", "41", "#41", "u"))
    (req,) = seen
    assert req.url.path == "/org/Eng/_apis/wit/workitems/$Feature"
    assert req.url.params["api-version"] == "7.1"
    assert req.headers["content-type"] == "application/json-patch+json"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b":pat").decode()
    ops = {o["path"]: o["value"] for o in json.loads(req.content)}
    assert ops["/fields/System.Title"] == "Same-day BOM to SAP"
    assert ops["/fields/Microsoft.VSTS.Common.BusinessValue"] == 13
    assert ops["/fields/Microsoft.VSTS.Scheduling.Effort"] == 8
    assert ops["/relations/-"] == {"rel": "System.LinkTypes.Hierarchy-Reverse",
                                   "url": "https://dev.azure.com/org/Eng/_apis/wit/workItems/41"}
    assert ref == ExternalRef("ado", "42", "#42", "https://dev/42")


def test_ado_story_gets_acceptance_criteria_html_escaped():
    seen, transport = recorder({"id": 7}, 200)
    ado = AzureDevOpsTracker("https://dev.azure.com/org", "Eng", "pat", transport=transport)
    story = SyncItem(key="BP-1", item_type="story", title="t", description="<b>x</b>",
                     acceptance_criteria=["Given a < b When c Then d"])
    ado.create(story, None)
    ops = {o["path"]: o["value"] for o in json.loads(seen[0].content)}
    assert seen[0].url.path.endswith("/$User Story")
    assert "&lt;b&gt;x&lt;/b&gt;" in ops["/fields/System.Description"]
    assert ops["/fields/Microsoft.VSTS.Common.AcceptanceCriteria"] == \
        "<ul><li>Given a &lt; b When c Then d</li></ul>"


# ---------- push from the app ----------

class FakeTracker:
    system, label = "fake", "Fake tracker"

    def __init__(self, fail_types=()):
        self.created: list[tuple[SyncItem, ExternalRef | None]] = []
        self.fail_types = set(fail_types)

    def create(self, item, parent):
        if item.item_type in self.fail_types:
            raise SyncError("tracker unavailable")
        self.created.append((item, parent))
        n = len(self.created)
        return ExternalRef("fake", str(n), f"FAKE-{n}", f"https://fake/{n}")


def approve_scenario(client, scenarios):
    review = client.post("/decompose", data={"text": scenarios[0]["text"]}).text
    return client.post("/approve", data={"drop_reason": "Trimmed for this test",
                                         "payload": payload_from(review),
                                         "source_text": source_from(review),
                                         "keep:0.0.0": "on"}).text


def test_approval_pushes_parents_first(settings, engine, scenarios, tracker):
    fake = FakeTracker()
    client = TestClient(create_app(settings, engine=engine, external=fake))
    result = approve_scenario(client, scenarios)
    assert "3 items created in Fake tracker" in result
    types = [i.item_type for i, _ in fake.created]
    assert types == ["epic", "feature", "story"]
    assert fake.created[0][1] is None and fake.created[1][1].key == "FAKE-1"
    assert fake.created[2][1].key == "FAKE-2"
    assert "acceptance" not in fake.created[0][0].description.lower()
    assert fake.created[2][0].acceptance_criteria
    assert tracker.pending_sync() == []
    assert "FAKE-3" in client.get("/backlog").text


def test_failed_push_keeps_approval_and_can_be_retried(settings, engine, scenarios, tracker):
    fake = FakeTracker(fail_types={"feature"})
    client = TestClient(create_app(settings, engine=engine, external=fake))
    result = approve_scenario(client, scenarios)
    assert "2 pending" in result  # the feature failed and its story waits for it
    errors = {i.item_type: i.details["sync_error"] for i in tracker.pending_sync()}
    assert errors == {"feature": "tracker unavailable",
                      "story": "Waiting for its parent to sync."}
    assert "Retry sync to Fake tracker (2)" in client.get("/backlog").text
    fake.fail_types.clear()
    client.post("/sync/retry")
    assert tracker.pending_sync() == []
    assert "synced_to_tracker" in client.get("/audit").text
