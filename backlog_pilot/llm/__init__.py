from backlog_pilot.config import Settings
from backlog_pilot.llm.base import DraftContext, LLMError, LLMProvider, ScoredFeature
from backlog_pilot.llm.replay import ReplayProvider, record, record_audit_plan


class RecordingProvider:
    """Wraps a live provider and saves each response for later offline replay."""

    def __init__(self, inner: LLMProvider, settings: Settings):
        self.inner = inner
        self.settings = settings
        self.name = f"{inner.name} (recording)"

    def decompose(self, text: str, context=None):
        result = self.inner.decompose(text, context or DraftContext())
        record(self.settings.replay_dir, text, result)
        return result

    def plan_audit(self, question: str, today, actors):
        plan = self.inner.plan_audit(question, today, actors)
        record_audit_plan(self.settings.replay_dir, question, plan)
        return plan

    def answer_audit(self, question: str, events):
        return self.inner.answer_audit(question, events)


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "replay":
        return ReplayProvider(settings.replay_dir)
    if settings.llm_provider == "anthropic":
        from backlog_pilot.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(settings.model)
        return RecordingProvider(provider, settings) if settings.record else provider
    if settings.llm_provider == "foundry":
        from backlog_pilot.llm.foundry import foundry_provider

        provider = foundry_provider(settings.model)
        return RecordingProvider(provider, settings) if settings.record else provider
    raise ValueError(f"Unknown BP_LLM_PROVIDER: {settings.llm_provider!r}")


__all__ = ["DraftContext", "LLMError", "LLMProvider", "ScoredFeature", "build_provider"]
