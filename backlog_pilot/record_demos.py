"""Run every demo scenario through the live model and save the responses for offline replay.

Usage: python -m backlog_pilot.record_demos [scenario-id ... | audit]   (default: everything)
`audit` records the search plans for the suggested audit-log questions.
Credentials come from the environment (API key, `ant auth login`, or workload identity
federation in CI); see .env.example.
"""

import json
import sys

from backlog_pilot.config import load_settings
from backlog_pilot.db import utcnow
from backlog_pilot.llm import DraftContext, LLMError
from backlog_pilot.llm.anthropic_provider import AnthropicProvider
from backlog_pilot.llm.replay import record, record_audit_plan
from backlog_pilot.personas import PersonaBrief


def main() -> int:
    settings = load_settings()
    provider = AnthropicProvider(settings.model)
    scenarios = json.loads(settings.scenarios_path.read_text())["scenarios"]
    args = set(sys.argv[1:])
    record_audit = not args or "audit" in args
    if args:  # record only what was named
        wanted = args - {"audit"}
        unknown = wanted - {s["id"] for s in scenarios}
        if unknown:
            print(f"Unknown scenario id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        scenarios = [s for s in scenarios if s["id"] in wanted]
    # Demos are recorded against the starter persona library the app seeds.
    personas = [PersonaBrief(p["name"], p["description"])
                for p in json.loads(settings.personas_path.read_text())["personas"]]
    failures = 0
    for scenario in scenarios:
        try:
            draft = provider.decompose(scenario["text"], DraftContext(personas=personas))
        except LLMError as e:
            failures += 1
            print(f"FAIL {scenario['id']}: {e}", file=sys.stderr)
            continue
        path = record(settings.replay_dir, scenario["text"], draft)
        print(f"ok   {scenario['id']}: {len(draft.epics)} epics, "
              f"{draft.story_count()} stories -> {path.name}")
    if record_audit:
        questions = json.loads(settings.audit_questions_path.read_text())["questions"]
        for question in questions:
            try:
                plan = provider.plan_audit(question, utcnow().date(), [])
            except LLMError as e:
                failures += 1
                print(f"FAIL audit '{question}': {e}", file=sys.stderr)
                continue
            path = record_audit_plan(settings.replay_dir, question, plan)
            print(f"ok   audit '{question}' -> {path.name}: {plan.explanation}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
