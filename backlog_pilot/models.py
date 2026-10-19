"""Domain models.

`Decomposition` is both the structured-output schema the LLM must fill and the payload
the review screen edits, so it stays free of tracker concerns (ids, status, timestamps).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# Modified Fibonacci scale used for every WSJF component.
WSJF_SCALE = (1, 2, 3, 5, 8, 13, 20)
Fib = Literal[1, 2, 3, 5, 8, 13, 20]
WSJF_PARTS = ("business_value", "time_criticality", "risk_reduction", "job_size")

NFR_CATEGORIES = ("security", "export_control", "data_governance", "auditability",
                  "integration_reliability")
Nfr = Literal["security", "export_control", "data_governance", "auditability",
              "integration_reliability"]


def _require_all(schema: dict) -> None:
    # Fields added after the first recordings carry defaults so older replays still load,
    # but the model is always asked to fill every field.
    schema["required"] = list(schema.get("properties", {}))


class UserStory(BaseModel):
    model_config = ConfigDict(json_schema_extra=_require_all)

    title: str = Field(description="Short, action-oriented story title.")
    as_a: str = Field(description="The persona: one of the approved persona names when one fits.")
    i_want: str = Field(description="The capability the persona needs.")
    so_that: str = Field(description="The business outcome the capability enables.")
    acceptance_criteria: list[str] = Field(
        description="Gherkin-style criteria, each one 'Given ... When ... Then ...'."
    )
    source_quotes: list[str] = Field(
        default_factory=list,
        description="Exact, verbatim excerpts of the stakeholder input that this story answers, "
        "copied character for character (no paraphrase, no ellipses). Empty when the story is "
        "your own inference, such as a security or governance need the input did not state.",
    )
    nfr_categories: list[Nfr] = Field(
        default_factory=list,
        description="Non-functional areas this story delivers, if any. Empty for purely "
        "functional stories.",
    )
    # Existing backlog keys the reviewer linked as related (never serialized).
    _related: list[str] = PrivateAttr(default_factory=list)
    _ai_title: str = PrivateAttr(default="")  # the AI's wording, if the reviewer changed it


class WsjfEstimate(BaseModel):
    model_config = ConfigDict(json_schema_extra=_require_all)

    business_value: Fib = Field(description="Value to the business and users, relative.")
    time_criticality: Fib = Field(description="How much value decays if this waits.")
    risk_reduction: Fib = Field(
        description="Risk reduction or opportunity enablement (including compliance).")
    job_size: Fib = Field(description="Relative effort to deliver the feature.")
    rationale: str = Field(description="One or two sentences justifying the four scores.")

    @property
    def cost_of_delay(self) -> int:
        return self.business_value + self.time_criticality + self.risk_reduction

    @property
    def score(self) -> float:
        return round(self.cost_of_delay / self.job_size, 1)


class Feature(BaseModel):
    model_config = ConfigDict(json_schema_extra=_require_all)

    title: str
    description: str
    stories: list[UserStory]
    wsjf: WsjfEstimate | None = Field(
        default=None, description="Draft WSJF scores for this feature. Always provide them.")
    # The model's own scores, kept beside any reviewer adjustment (never serialized).
    _ai_wsjf: WsjfEstimate | None = PrivateAttr(default=None)
    _wsjf_reason: str = PrivateAttr(default="")  # why the reviewer changed the AI's score
    _related: list[str] = PrivateAttr(default_factory=list)
    _ai_title: str = PrivateAttr(default="")  # the AI's wording, if the reviewer changed it


class Epic(BaseModel):
    title: str
    description: str
    business_outcome: str = Field(description="Measurable outcome this epic should move.")
    features: list[Feature]
    # Product key the reviewer assigned (never serialized).
    _product: str | None = PrivateAttr(default=None)
    _ai_title: str = PrivateAttr(default="")  # the AI's wording, if the reviewer changed it


class PersonaProposal(BaseModel):
    name: str = Field(description="Role name in lower case, e.g. 'supplier quality engineer'.")
    description: str = Field(description="One sentence: who they are and what they do.")
    goals: list[str]
    pain_points: list[str]


class Decomposition(BaseModel):
    model_config = ConfigDict(json_schema_extra=_require_all)

    summary: str = Field(description="One or two sentences restating the request.")
    epics: list[Epic]
    assumptions: list[str] = Field(description="Assumptions made where the input was silent.")
    open_questions: list[str] = Field(
        description="Questions the product owner should take back to the stakeholder."
    )
    proposed_personas: list[PersonaProposal] = Field(
        default_factory=list,
        description="Personas used in stories that are not in the approved persona list. "
        "Empty when every story uses an approved persona.",
    )

    # What the reviewer dropped from the AI's draft, and why (never serialized).
    _dropped: list[str] = PrivateAttr(default_factory=list)
    _drop_reason: str = PrivateAttr(default="")

    def story_count(self) -> int:
        return sum(len(f.stories) for e in self.epics for f in e.features)

    def stories(self):
        """(path, story) pairs, where path is 'epic.feature.story' as the review form uses."""
        for ei, epic in enumerate(self.epics):
            for fi, feature in enumerate(epic.features):
                for si, story in enumerate(feature.stories):
                    yield f"{ei}.{fi}.{si}", story
