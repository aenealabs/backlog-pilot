"""Provider interface shared by the live and replay LLM backends."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from backlog_pilot.models import Decomposition
from backlog_pilot.personas import PersonaBrief


class LLMError(Exception):
    """A provider could not produce a usable decomposition; the message is user-facing."""


@dataclass(frozen=True)
class ScoredFeature:
    """An existing feature's WSJF scores, shown to the model so new scores are relative."""
    title: str
    business_value: int
    time_criticality: int
    risk_reduction: int
    job_size: int


@dataclass(frozen=True)
class DraftContext:
    """What the team already knows that every draft should respect."""
    personas: Sequence[PersonaBrief] = ()
    scored_features: Sequence[ScoredFeature] = ()


class LLMProvider(Protocol):
    name: str

    def decompose(self, text: str, context: DraftContext | None = None) -> Decomposition: ...


def input_key(text: str) -> str:
    """Stable key for a stakeholder input, insensitive to whitespace differences."""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]
