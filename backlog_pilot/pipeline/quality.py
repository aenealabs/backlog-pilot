"""Deterministic quality checks: a story-level "ready" check and draft-level coverage of
the non-functional needs the stakeholder's words imply.

The model tags stories with non-functional categories; whether the draft covers what the
input mentions is decided here, by rules a reviewer can read.
"""

import re
from dataclasses import dataclass

from backlog_pilot.models import NFR_CATEGORIES, Decomposition, UserStory
from backlog_pilot.pipeline.trace import INFERRED, UNVERIFIED, StoryTrace

WARN, INFO = "warn", "info"

MIN_CRITERIA, MAX_CRITERIA = 2, 5
_GHERKIN = re.compile(r"\bgiven\b.*\bwhen\b.*\bthen\b", re.IGNORECASE | re.DOTALL)
# Words that make an acceptance criterion untestable (shown in the Help reference).
VAGUE_WORDS = ("fast", "quickly", "easy", "easily", "user-friendly", "user friendly",
               "intuitive", "seamless", "seamlessly", "robust", "appropriate", "appropriately",
               "efficient", "efficiently", "as needed", "etc")
_VAGUE = re.compile(r"\b(" + "|".join(re.escape(w) for w in VAGUE_WORDS) + r")\b",
                    re.IGNORECASE)
_SEVERAL = re.compile(r"\b(and|as well as|plus)\b", re.IGNORECASE)

NFR_LABELS = {
    "security": "Security",
    "export_control": "Export control (ITAR/EAR)",
    "data_governance": "Data governance",
    "auditability": "Auditability",
    "integration_reliability": "Integration reliability",
}

# Words in the stakeholder input that imply a non-functional need.
NFR_TRIGGERS = {
    "security": r"secur|cyber|access control|authori[sz]|mfa|single sign|vulnerab|unsupported",
    "export_control": r"\bitar\b|\bear\b|export[- ]control|licen[sc]ed to",
    "data_governance": r"duplicate|master data|data quality|governance|classif",
    "auditability": r"audit|regulat|retention|traceab|compliance|evidence",
    "integration_reliability": r"\bsap\b|integration|interface|transfer|sync",
}
assert set(NFR_TRIGGERS) == set(NFR_CATEGORIES)


@dataclass(frozen=True)
class Issue:
    level: str
    code: str
    message: str


def check_story(story: UserStory, trace: StoryTrace, known_personas: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    criteria = [c for c in story.acceptance_criteria if c.strip()]
    if len(criteria) < MIN_CRITERIA:
        found = {0: "No acceptance criteria", 1: "Only 1 acceptance criterion"}[len(criteria)]
        issues.append(Issue(WARN, "few_criteria", f"{found}; add at least {MIN_CRITERIA}."))
    elif len(criteria) > MAX_CRITERIA:
        issues.append(Issue(WARN, "many_criteria",
                            f"{len(criteria)} acceptance criteria; consider splitting the story."))
    not_gherkin = [i + 1 for i, c in enumerate(criteria) if not _GHERKIN.search(c)]
    if not_gherkin:
        issues.append(Issue(WARN, "not_gherkin",
                            f"Criteria {', '.join(map(str, not_gherkin))} are not in "
                            "Given/When/Then form."))
    vague = sorted({m.group(0).lower() for c in criteria for m in _VAGUE.finditer(c)})
    if vague:
        issues.append(Issue(WARN, "untestable",
                            f"Untestable wording in criteria: {', '.join(vague)}."))
    if len(_SEVERAL.findall(story.i_want)) >= 2:
        issues.append(Issue(INFO, "compound",
                            "The capability joins several things; check it is one small story."))
    if not story.so_that.strip():
        issues.append(Issue(WARN, "no_outcome", "No 'so that' outcome."))
    persona = " ".join(story.as_a.split()).lower()
    if persona not in known_personas:
        issues.append(Issue(INFO, "persona", f"'{persona}' is not in the persona library."))
    if trace.status == UNVERIFIED:
        issues.append(Issue(WARN, "misquote", "Its quotes do not appear in the input."))
    elif trace.status == INFERRED:
        issues.append(Issue(INFO, "inferred", "AI inference: confirm the stakeholder wants it."))
    return issues


def is_ready(issues: list[Issue]) -> bool:
    return not any(i.level == WARN for i in issues)


@dataclass(frozen=True)
class Coverage:
    category: str
    label: str
    mentioned: bool  # the input implies this need
    covered: bool  # at least one story is tagged with it

    @property
    def gap(self) -> bool:
        return self.mentioned and not self.covered


def nfr_coverage(source: str, draft: Decomposition,
                 kept: set[str] | None = None) -> list[Coverage]:
    """Which non-functional needs the input implies, and whether stories cover them.

    `kept` limits coverage to the story paths the reviewer is keeping.
    """
    text = source.lower()
    tagged = {c for path, story in draft.stories() if kept is None or path in kept
              for c in story.nfr_categories}
    return [Coverage(c, NFR_LABELS[c], bool(re.search(NFR_TRIGGERS[c], text)), c in tagged)
            for c in NFR_CATEGORIES]
