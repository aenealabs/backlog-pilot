"""The Help reference: rules, checks, and settings, built from the code that uses them.

Quality checks come from pipeline/quality.py, duplicate thresholds from similarity.py, WSJF
from models.py, and planning from roadmap.py, so the reference cannot say something the app
does not do. SETTINGS documents every environment setting; a test fails if the code reads one
that is not listed here.
"""

from mission_control import ReferenceSection

from backlog_pilot.models import WSJF_PARTS, WSJF_SCALE
from backlog_pilot.pipeline.decompose import MAX_INPUT_CHARS, MIN_INPUT_CHARS
from backlog_pilot.pipeline.quality import (
    MAX_CRITERIA,
    MIN_CRITERIA,
    NFR_LABELS,
    NFR_TRIGGERS,
    VAGUE_WORDS,
)
from backlog_pilot.pipeline.similarity import LIKELY, MAX_MATCHES, POSSIBLE
from backlog_pilot.roadmap import HORIZON
from backlog_pilot.sync import WSJF_LABELS

SETTINGS = {
    "BP_DB_PATH": "Where the app keeps its database (SQLite file).",
    "BP_LLM_PROVIDER": "replay (offline demo, recorded drafts), anthropic (Claude API), or "
                       "foundry (Claude on Microsoft Foundry).",
    "BP_MODEL": "The Claude model (or Foundry deployment) that drafts.",
    "BP_RECORD": "true to save live drafts as recordings for the offline demo.",
    "ANTHROPIC_API_KEY": "Claude API key, when BP_LLM_PROVIDER=anthropic.",
    "ANTHROPIC_FOUNDRY_RESOURCE": "The Microsoft Foundry resource, when BP_LLM_PROVIDER=foundry.",
    "ANTHROPIC_FOUNDRY_API_KEY": "Key for Claude on Microsoft Foundry; without it, Azure sign-in "
                                 "(managed identity) is used.",
    "BP_AUTH": "demo (pick a fictional person) or oidc (single sign-on).",
    "BP_OIDC_METADATA_URL": "Your identity provider's OpenID Connect metadata address.",
    "BP_OIDC_CLIENT_ID": "The app registration's client ID.",
    "BP_OIDC_CLIENT_SECRET": "The app registration's client secret.",
    "BP_SESSION_SECRET": "A long random value that signs the sign-in cookie.",
    "BP_SESSION_HTTPS_ONLY": "false only for local testing over plain http.",
    "BP_APPROVER_ROLE": "The app role that makes someone an approver.",
    "BP_APPROVER_EMAILS": "Comma-separated emails that are approvers even without the role.",
    "BP_TRACKER": "Where approved work is pushed: unset (this app only), jira, or ado.",
    "BP_JIRA_URL": "Jira site address.",
    "BP_JIRA_PROJECT": "Jira project key.",
    "BP_JIRA_EMAIL": "Jira account email (Jira Cloud).",
    "BP_JIRA_TOKEN": "Jira API token.",
    "BP_JIRA_TYPES": "Jira issue types for epic, feature, and story, if not the defaults.",
    "BP_JIRA_EPIC_LINK_FIELD": "The Epic Link custom field, for Jira versions that need it.",
    "BP_ADO_ORG_URL": "Azure DevOps organization address.",
    "BP_ADO_PROJECT": "Azure DevOps project.",
    "BP_ADO_TOKEN": "Azure DevOps personal access token.",
    "BP_ADO_TYPES": "Azure DevOps work item types for epic, feature, and story, if not the "
                    "defaults.",
    "BP_LEADERSHIP_URL": "Leadership Pilot's address (older single-link menu).",
    "BP_SUITE_TOKEN": "The shared Mission Control token other apps use to post to the inbox; "
                      "the inbox is off when unset.",
    "MISSION_CONTROL_APPS": "The suite's apps and addresses, the same in every app.",
}

REASONS = [
    ("Changing the AI's WSJF score in review", "A reason next to the score"),
    ("Dropping anything the AI drafted", "Unticked stories, removed assumptions or questions, "
                                         "or an unticked proposed persona: one reason for the "
                                         "review"),
    ("Re-scoring a feature on Priorities", "A reason, kept in the WSJF history"),
    ("Pinning a feature to a quarter", "A reason, since a pin overrides the roadmap rules"),
    ("Rejecting a proposed persona", "A reason, since it came from what stakeholders said"),
    ("Rewording a drafted title", "No reason; the AI's wording is kept beside it and logged"),
]


def reference() -> list[ReferenceSection]:
    scale = ", ".join(map(str, WSJF_SCALE))
    return [
        ReferenceSection("Story ready checks", [
            ("Acceptance criteria", f"{MIN_CRITERIA} to {MAX_CRITERIA}; more suggests "
                                    "splitting the story"),
            ("Given/When/Then", "Every criterion has Given, When, and Then, in that order"),
            ("Testable wording", "No " + ", ".join(f'"{w}"' for w in VAGUE_WORDS)),
            ("One capability", "A note (not a warning) when 'I want' uses 'and', 'as well "
                               "as', or 'plus' two or more times"),
            ("Outcome", "A 'so that' is present"),
            ("Persona", "In the persona library (a note, not a warning)"),
            ("Quotes", "Every quote is found in the input; no quotes means AI inference"),
        ], columns=("Check", "Passes when"),
            intro="A story is ready when it has no warnings. Notes are shown but do not block."),
        ReferenceSection("Non-functional coverage", [
            (NFR_LABELS[c], "Input mentions: " + ", ".join(
                w.replace("\\b", "") + (" (whole word)" if w.startswith("\\b") else "")
                for w in pattern.split("|")))
            for c, pattern in NFR_TRIGGERS.items()
        ], columns=("Need", "Flagged as a gap when"),
            intro="If the input implies a need and no kept story is tagged with it, the review "
                  "shows a gap. Words match ignoring case and can be the start of a longer "
                  "word (secur matches security); brackets mean either letter."),
        ReferenceSection("Tracing to the stakeholder's words", [
            ("Traced", "Every quote the story cites is in the input"),
            ("Quote not found", "A cited quote is not in the input (a warning)"),
            ("AI inference", "The story cites no quote; confirm the stakeholder wants it"),
            ("Matching", "Ignores case, spacing, and surrounding punctuation; a quote with an "
                         "ellipsis must match each part, in order"),
        ], columns=("Label", "Means")),
        ReferenceSection("Duplicate detection", [
            ("Method", "TF-IDF text similarity against live features and stories; no AI"),
            ("Possible overlap", f"Similarity of {round(100 * POSSIBLE)}% or more"),
            ("Likely duplicate", f"Similarity of {round(100 * LIKELY)}% or more"),
            ("Shown", f"Up to {MAX_MATCHES} matches per drafted item"),
        ], columns=("Item", "Value")),
        ReferenceSection("WSJF", [
            ("Parts", ", ".join(WSJF_LABELS[p] for p in WSJF_PARTS)),
            ("Scale", f"Each part is one of {scale}"),
            ("Score", "(Business value + Time criticality + Risk reduction) / Job size, to one "
                      "decimal"),
        ], columns=("Item", "Value")),
        ReferenceSection("Roadmap planning", [
            ("Horizon", f"{HORIZON} quarters, starting with this one"),
            ("Capacity", "Each product's quarterly capacity, in WSJF job-size points"),
            ("Order", "Pinned features go where a person put them; in-progress features stay in "
                      "this quarter; the rest fill quarters in WSJF order (highest first) where "
                      "they fit"),
            ("Unplanned", "Scored features that fit in no quarter shown"),
            ("Moves", "Each recalculation is compared with the last; every feature that changed "
                      "quarter is logged with the cause and who caused it"),
        ], columns=("Rule", "How it works")),
        ReferenceSection("Change control", [
            ("Assumption changed by a person", "May affect the backlog: a person chooses "
                                               "re-draft or no change"),
            ("Question answered", "May affect the backlog"),
            ("Question added, reworded, or removed", "Captured, not flagged"),
            ("Input length", f"{MIN_INPUT_CHARS} to {MAX_INPUT_CHARS:,} characters"),
        ], columns=("Change", "Result")),
        ReferenceSection("When a reason is needed", REASONS, columns=("Action", "Reason")),
        ReferenceSection("Settings", sorted(SETTINGS.items()),
                         columns=("Setting", "What it does"),
                         intro="Environment settings (or a .env file next to the app)."),
    ]
