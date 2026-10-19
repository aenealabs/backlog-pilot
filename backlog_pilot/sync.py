"""Push approved backlog items to the team's tracker (Jira or Azure DevOps).

Backlog Pilot stays the record of intakes, decisions, and versions; the tracker is where
delivery teams work. Approved items are pushed parents-first (epic, feature, story). A push
that fails never undoes the approval: the item is marked "sync pending" and can be retried.

Both adapters follow the vendors' documented REST APIs (Jira REST v2, which Jira Cloud and
Data Center both serve; Azure DevOps Work Item Tracking 7.1). They are covered by tests with
simulated responses and still need a check against a real instance before production use.
"""

import html
import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

WSJF_LABELS = {"business_value": "Business value", "time_criticality": "Time criticality",
               "risk_reduction": "Risk reduction / opportunity enablement",
               "job_size": "Job size"}


class SyncError(Exception):
    """A push to the tracker failed; the message is shown on the backlog item."""


@dataclass
class SyncItem:
    key: str  # Backlog Pilot key, e.g. BP-112
    item_type: str  # epic | feature | story
    title: str
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)
    wsjf: dict | None = None
    product: str = ""


@dataclass(frozen=True)
class ExternalRef:
    system: str
    id: str
    key: str
    url: str


class Tracker(Protocol):
    system: str
    label: str

    def create(self, item: SyncItem, parent: ExternalRef | None) -> ExternalRef: ...


def _types(raw: str, defaults: dict[str, str]) -> dict[str, str]:
    """Parse 'epic=Epic,feature=Feature,story=Story' overrides."""
    mapping = dict(defaults)
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        k, _, v = pair.partition("=")
        if k.strip() in mapping and v.strip():
            mapping[k.strip()] = v.strip()
    return mapping


def _wsjf_lines(wsjf: dict | None) -> list[str]:
    if not wsjf:
        return []
    return [f"{label}: {wsjf[part]}" for part, label in WSJF_LABELS.items()]


def _raise_for(response: httpx.Response, system: str) -> None:
    if response.is_success:
        return
    detail = response.text[:300].replace("\n", " ")
    raise SyncError(f"{system} returned HTTP {response.status_code}: {detail}")


# ---------- Jira ----------

class JiraTracker:
    """Jira REST API v2 (Cloud and Data Center).

    Cloud signs in with an account email plus API token; Data Center with a personal access
    token. Child issues link to their parent through the `parent` field, or, for older Data
    Center setups, through an Epic Link custom field (BP_JIRA_EPIC_LINK_FIELD).
    """

    system = "jira"

    def __init__(self, base_url: str, project: str, token: str, email: str = "",
                 types: dict[str, str] | None = None, epic_link_field: str = "",
                 transport: httpx.BaseTransport | None = None):
        auth = httpx.BasicAuth(email, token) if email else None
        headers = {} if email else {"Authorization": f"Bearer {token}"}
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.types = types or {"epic": "Epic", "feature": "Feature", "story": "Story"}
        self.epic_link_field = epic_link_field
        self.label = f"Jira {project}"
        self.http = httpx.Client(base_url=self.base_url, auth=auth, headers=headers,
                                 timeout=20.0, transport=transport)

    def description(self, item: SyncItem) -> str:
        parts = [item.description.strip()]
        if item.acceptance_criteria:
            parts.append("h3. Acceptance criteria\n"
                         + "\n".join(f"* {c}" for c in item.acceptance_criteria))
        if item.wsjf:
            parts.append("h3. WSJF\n" + "\n".join(f"* {line}" for line in _wsjf_lines(item.wsjf)))
        parts.append(f"_Created by Backlog Pilot from {item.key}._")
        return "\n\n".join(p for p in parts if p)

    def create(self, item: SyncItem, parent: ExternalRef | None) -> ExternalRef:
        fields: dict = {
            "project": {"key": self.project},
            "summary": item.title[:255],
            "issuetype": {"name": self.types[item.item_type]},
            "description": self.description(item),
            "labels": ["backlog-pilot"] + ([item.product] if item.product else []),
        }
        if parent is not None:
            if self.epic_link_field and item.item_type != "epic":
                fields[self.epic_link_field] = parent.key
            else:
                fields["parent"] = {"key": parent.key}
        response = self.http.post("/rest/api/2/issue", json={"fields": fields})
        _raise_for(response, "Jira")
        data = response.json()
        return ExternalRef("jira", str(data["id"]), data["key"],
                           f"{self.base_url}/browse/{data['key']}")


# ---------- Azure DevOps ----------

class AzureDevOpsTracker:
    """Azure DevOps Work Item Tracking REST API 7.1, signed in with a personal access token.

    Uses the Agile process types by default (Epic > Feature > User Story); set
    BP_ADO_TYPES for Scrum ("story=Product Backlog Item"). WSJF maps to ADO's own
    Business Value, Time Criticality, and Effort fields on features.
    """

    system = "ado"
    API = "7.1"

    def __init__(self, org_url: str, project: str, token: str,
                 types: dict[str, str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.org_url = org_url.rstrip("/")
        self.project = project
        self.types = types or {"epic": "Epic", "feature": "Feature", "story": "User Story"}
        self.label = f"Azure DevOps {project}"
        self.http = httpx.Client(base_url=self.org_url, auth=httpx.BasicAuth("", token),
                                 timeout=20.0, transport=transport)

    def operations(self, item: SyncItem, parent: ExternalRef | None) -> list[dict]:
        def add(path: str, value) -> dict:
            return {"op": "add", "path": path, "value": value}

        description = f"<p>{html.escape(item.description)}</p>"
        if item.wsjf:
            description += ("<p><b>WSJF</b><br>"
                            + "<br>".join(html.escape(x) for x in _wsjf_lines(item.wsjf))
                            + "</p>")
        description += f"<p><i>Created by Backlog Pilot from {html.escape(item.key)}.</i></p>"
        ops = [add("/fields/System.Title", item.title[:255]),
               add("/fields/System.Description", description),
               add("/fields/System.Tags",
                   "; ".join(["backlog-pilot"] + ([item.product] if item.product else [])))]
        if item.acceptance_criteria:
            ops.append(add("/fields/Microsoft.VSTS.Common.AcceptanceCriteria",
                           "<ul>" + "".join(f"<li>{html.escape(c)}</li>"
                                            for c in item.acceptance_criteria) + "</ul>"))
        if item.wsjf and item.item_type == "feature":
            ops += [add("/fields/Microsoft.VSTS.Common.BusinessValue",
                        item.wsjf["business_value"]),
                    add("/fields/Microsoft.VSTS.Common.TimeCriticality",
                        item.wsjf["time_criticality"]),
                    add("/fields/Microsoft.VSTS.Scheduling.Effort", item.wsjf["job_size"])]
        if parent is not None:
            ops.append(add("/relations/-", {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"{self.org_url}/{self.project}/_apis/wit/workItems/{parent.id}"}))
        return ops

    def create(self, item: SyncItem, parent: ExternalRef | None) -> ExternalRef:
        work_type = self.types[item.item_type]
        response = self.http.post(
            f"/{self.project}/_apis/wit/workitems/${work_type}",
            params={"api-version": self.API},
            json=self.operations(item, parent),
            headers={"Content-Type": "application/json-patch+json"})
        _raise_for(response, "Azure DevOps")
        data = response.json()
        url = data.get("_links", {}).get("html", {}).get("href") or \
            f"{self.org_url}/{self.project}/_workitems/edit/{data['id']}"
        return ExternalRef("ado", str(data["id"]), f"#{data['id']}", url)


def build_tracker() -> Tracker | None:
    """The configured tracker, or None to keep items in Backlog Pilot only (demo mode)."""
    kind = os.getenv("BP_TRACKER", "mock").strip().lower()
    if kind == "jira":
        return JiraTracker(os.environ["BP_JIRA_URL"], os.environ["BP_JIRA_PROJECT"],
                           os.environ["BP_JIRA_TOKEN"], os.getenv("BP_JIRA_EMAIL", ""),
                           _types(os.getenv("BP_JIRA_TYPES", ""),
                                  {"epic": "Epic", "feature": "Feature", "story": "Story"}),
                           os.getenv("BP_JIRA_EPIC_LINK_FIELD", ""))
    if kind == "ado":
        return AzureDevOpsTracker(os.environ["BP_ADO_ORG_URL"], os.environ["BP_ADO_PROJECT"],
                                  os.environ["BP_ADO_TOKEN"],
                                  _types(os.getenv("BP_ADO_TYPES", ""),
                                         {"epic": "Epic", "feature": "Feature",
                                          "story": "User Story"}))
    return None
