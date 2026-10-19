"""Offline provider: serves recorded responses so demos never depend on the network."""

import json
from pathlib import Path

from backlog_pilot.audit_search import AuditPlan
from backlog_pilot.llm.base import LLMError, input_key
from backlog_pilot.models import Decomposition


class ReplayProvider:
    name = "replay"

    def __init__(self, replay_dir: Path):
        self.replay_dir = replay_dir

    def decompose(self, text: str, context=None) -> Decomposition:
        # Recordings were made against the context of their day; the context is ignored.
        path = self.replay_dir / f"{input_key(text)}.json"
        if not path.exists():
            raise LLMError(
                "Offline demo mode only has recorded responses for the demo scenarios. "
                "Pick a scenario, or set BP_LLM_PROVIDER=anthropic for live mode."
            )
        return Decomposition.model_validate(json.loads(path.read_text())["decomposition"])

    def plan_audit(self, question: str, today, actors) -> AuditPlan:
        """Recorded search plans for the suggested questions. Plans hold no event data,
        so they replay correctly against whatever is in the log now."""
        path = audit_plan_path(self.replay_dir, question)
        if not path.exists():
            raise LLMError("Offline demo mode only knows the suggested questions. Use the "
                           "search filters, or switch to live AI mode for any question.")
        return AuditPlan.model_validate(json.loads(path.read_text())["plan"])

    def answer_audit(self, question: str, events):
        return None  # offline: the app shows a summary computed from the events instead


def record(replay_dir: Path, text: str, decomposition: Decomposition) -> Path:
    replay_dir.mkdir(parents=True, exist_ok=True)
    path = replay_dir / f"{input_key(text)}.json"
    payload = {"input": text, "decomposition": decomposition.model_dump()}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def audit_plan_path(replay_dir: Path, question: str) -> Path:
    return replay_dir / "audit" / f"{input_key(question)}.json"


def record_audit_plan(replay_dir: Path, question: str, plan: AuditPlan) -> Path:
    path = audit_plan_path(replay_dir, question)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"question": question, "plan": plan.model_dump(mode="json")},
                               indent=2) + "\n")
    return path
