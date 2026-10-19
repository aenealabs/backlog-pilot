"""Prompt text for the decomposition step."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from backlog_pilot.personas import PersonaBrief

if TYPE_CHECKING:  # llm.base imports this module's package; avoid the cycle at runtime
    from backlog_pilot.llm.base import ScoredFeature

DECOMPOSE_SYSTEM = """\
You are a senior IT product owner for Product Engineering and R&D systems at a global \
industrial manufacturer. The portfolio covers PLM (Enovia / 3DEXPERIENCE), CAD data \
management, simulation, product configuration, engineering change management (ECR/ECO), \
engineering BOMs and their integration with SAP ERP, and engineering data governance.

Turn the stakeholder input you are given into an agile backlog:
- Epics express a business outcome; features are releasable increments; user stories \
are small enough for one sprint.
- Write stories as "As a <persona>, I want <capability>, so that <outcome>".
- Give every story two to four acceptance criteria in Given/When/Then form that a tester \
could verify.
- Cover non-functional needs where the input implies them: cybersecurity, data quality \
and governance, auditability, and export-controlled (ITAR/EAR) engineering data.
- Do not invent facts. Where the input is silent, record an assumption; where a \
decision belongs to the stakeholder, record an open question.
- Prefer fewer, sharper items over many thin ones: usually one or two epics, two to four \
features each, and two to four stories per feature.

Traceability: for every story, copy into source_quotes the exact words of the stakeholder \
input that the story answers, character for character. Quote whole phrases or sentences; \
never paraphrase, summarize, or join fragments with ellipses. A story you added on your own \
judgment (for example a security, audit, or data-governance need the input did not state) \
gets an empty source_quotes list, so reviewers can see it is your inference.

Non-functional tags: tag each story in nfr_categories with the non-functional areas it \
delivers (security, export_control, data_governance, auditability, \
integration_reliability); leave it empty for purely functional stories.

WSJF: give every feature draft WSJF scores on the modified Fibonacci scale \
(1, 2, 3, 5, 8, 13, 20): business_value, time_criticality, risk_reduction (risk reduction \
or opportunity enablement, including compliance), and job_size (relative effort). Scores \
are relative: spread them so the features can be ranked, and reserve 20 for the extreme. \
Explain the four scores in one or two sentences of rationale.
"""

PERSONA_RULES = """
Personas: the team keeps an approved persona library, listed below. Use these names \
exactly, in lower case, as the "as a" persona whenever one fits. Only when no approved \
persona fits, use a new specific role name and add it to proposed_personas with a one-line \
description, goals, and pain points; a reviewer decides whether it joins the library.

Approved personas:
"""


def decompose_system(personas: Sequence[PersonaBrief]) -> str:
    if not personas:
        return DECOMPOSE_SYSTEM
    listing = "\n".join(f"- {p.name}: {p.description}" for p in personas)
    return DECOMPOSE_SYSTEM + PERSONA_RULES + listing + "\n"


def redraft_input(source_text: str, assumptions: list[str],
                  answered: list[tuple[str, str]], open_questions: list[str]) -> str:
    """Stakeholder input for a re-draft: the original request plus what the team now knows."""
    parts = [source_text.strip()]
    if assumptions:
        parts.append("Assumptions confirmed by the product owner (treat as facts):\n"
                     + "\n".join(f"- {a}" for a in assumptions))
    if answered:
        parts.append("Answers from the stakeholder:\n"
                     + "\n".join(f"- Q: {q}\n  A: {a}" for q, a in answered))
    if open_questions:
        parts.append("Questions still open (keep them open unless the input answers them):\n"
                     + "\n".join(f"- {q}" for q in open_questions))
    return "\n\n".join(parts)


def decompose_user_message(text: str, scored: "Sequence[ScoredFeature]" = ()) -> str:
    message = f"Stakeholder input:\n\n<input>\n{text.strip()}\n</input>"
    if scored:
        rows = "\n".join(
            f"- {f.title}: business value {f.business_value}, time criticality "
            f"{f.time_criticality}, risk reduction {f.risk_reduction}, job size {f.job_size}"
            for f in scored)
        message += ("\n\nFeatures already scored in this backlog. Calibrate your WSJF scores "
                    "against them so the new features rank sensibly among them:\n" + rows)
    return message


# ---------- audit log questions ----------

def audit_plan_system() -> str:
    from backlog_pilot.audit_search import CATEGORIES

    categories = "\n".join(f"- {name}: actions {', '.join(patterns)}"
                           for name, patterns in CATEGORIES.items())
    return f"""\
You turn a question about Backlog Pilot's audit log into a search plan. You do not answer
the question; code runs your plan against the log.

Each audit event has a time, an actor ("Name (Role)"), an action, and a detail text that
names items (BP-123 for backlog items, IN-4 for intakes), old and new values, and any reason
given. Event categories:
{categories}

Rules:
- Use keywords only for words that would literally appear in the detail text (a feature
  name, a reason, a value). Leave keywords empty when categories, people, items, or dates
  already narrow the search; extra keywords make matches fail.
- Resolve relative dates ("last week", "since Monday", "in September") against today's date.
- Prefer broader plans: a reviewer can narrow results, but cannot see what a plan excluded.
"""


def audit_plan_message(question: str, today, actors: list[str]) -> str:
    people = "\n".join(f"- {a}" for a in actors[:50]) or "- (none yet)"
    return (f"Today is {today.isoformat()}.\n\nPeople who appear in the log:\n{people}\n\n"
            f"Question:\n<question>\n{question.strip()}\n</question>")


AUDIT_ANSWER_SYSTEM = """\
You answer a question about Backlog Pilot's audit log using only the events provided. Each
event is numbered (#id). Cite every fact with its event number in square brackets, like
[#42]. If the events do not answer the question, say what they do show and what is missing.
Be brief: a few sentences or a short list. Never invent events, people, dates, or reasons.
"""


def audit_answer_message(question: str, events: list) -> str:
    lines = "\n".join(f"#{e.id} | {e.at:%Y-%m-%d %H:%M} UTC | {e.actor} | {e.action} | "
                      f"{e.detail}" for e in events)
    return (f"Question:\n<question>\n{question.strip()}\n</question>\n\n"
            f"Matching events (newest first):\n<events>\n{lines}\n</events>")
