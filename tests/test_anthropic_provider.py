from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backlog_pilot.llm import DraftContext, LLMError, ScoredFeature
from backlog_pilot.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from backlog_pilot.models import Decomposition
from backlog_pilot.personas import PersonaBrief

DRAFT = Decomposition(summary="s", epics=[], assumptions=[], open_questions=[])


class FakeClient:
    def __init__(self, response):
        self.calls = []
        self.response = response
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        yield SimpleNamespace(get_final_message=lambda: self.response)


def provider_for(stop_reason="end_turn", parsed=DRAFT):
    client = FakeClient(SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed))
    return AnthropicProvider("claude-opus-5-5", client=client), client


def test_returns_parsed_output_and_sends_expected_request():
    provider, client = provider_for()
    context = DraftContext(
        personas=[PersonaBrief("eco approver", "Approves engineering changes.")],
        scored_features=[ScoredFeature("SAP BOM upsert", 13, 8, 5, 8)])
    assert provider.decompose("ECO approvals are slow", context) is DRAFT
    call = client.calls[0]
    assert "- eco approver: Approves engineering changes." in call["system"]
    assert "source_quotes" in call["system"] and "WSJF" in call["system"]
    message = call["messages"][0]["content"]
    assert "- SAP BOM upsert: business value 13, time criticality 8" in message
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is Decomposition
    assert call["thinking"] == {"type": "adaptive"}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert "ECO approvals are slow" in call["messages"][0]["content"]


@pytest.mark.parametrize("stop_reason, message", [
    ("refusal", "declined"),
    ("max_tokens", "cut off"),
])
def test_bad_stop_reasons_raise(stop_reason, message):
    provider, _ = provider_for(stop_reason=stop_reason)
    with pytest.raises(LLMError, match=message):
        provider.decompose("text")


def test_missing_parsed_output_raises():
    provider, _ = provider_for(parsed=None)
    with pytest.raises(LLMError, match="valid backlog"):
        provider.decompose("text")
