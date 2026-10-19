"""Intake -> decomposition -> human edits. The LLM drafts; the product owner decides."""

from collections.abc import Iterable, Mapping

from backlog_pilot.intakes import ReviewedNotes
from backlog_pilot.llm import DraftContext, LLMError, LLMProvider
from backlog_pilot.models import WSJF_PARTS, WSJF_SCALE, Decomposition, WsjfEstimate

MIN_INPUT_CHARS = 40
MAX_INPUT_CHARS = 20_000


def decompose(provider: LLMProvider, text: str,
              context: DraftContext | None = None) -> Decomposition:
    text = text.strip()
    if len(text) < MIN_INPUT_CHARS:
        raise LLMError("Add more detail: describe the problem, who has it, and why it matters.")
    if len(text) > MAX_INPUT_CHARS:
        raise LLMError(f"Input is too long ({len(text):,} characters; limit {MAX_INPUT_CHARS:,}).")
    return provider.decompose(text, context or DraftContext())


def apply_edits(draft: Decomposition, form: Mapping[str, str]) -> Decomposition:
    """Apply the review screen's edits.

    Field names are path-addressed: `title:0` (epic), `title:0.1` (feature),
    `title:0.1.2` (story), and `keep:0.1.2` for each story checkbox left ticked.
    WSJF scores arrive as `wsjf:0.1:business_value` (and the other parts), with
    `wsjf_reason:0.1` saying why the reviewer changed the AI's score, and existing
    items the reviewer links as related as `related:0.1:BP-103` (feature) or
    `related:0.1.2:BP-120` (story). Features left with no stories, and epics left with no
    features, are dropped. Unticked stories are listed in `_dropped`, and `drop_reason` says
    why; a changed title keeps the AI's wording in `_ai_title`.
    """
    result = draft.model_copy(deep=True)
    epics = []
    for ei, epic in enumerate(result.epics):
        epic._ai_title = _retitle(epic, form.get(f"title:{ei}", ""))
        epic._product = form.get(f"product:{ei}") or None
        features = []
        for fi, feature in enumerate(epic.features):
            path = f"{ei}.{fi}"
            feature._ai_title = _retitle(feature, form.get(f"title:{path}", ""))
            feature._ai_wsjf = feature.wsjf
            feature._related = _related(form, path)
            feature.wsjf = _edited_wsjf(feature.wsjf, form, path)
            feature._wsjf_reason = form.get(f"wsjf_reason:{path}", "").strip()
            stories = []
            for si, story in enumerate(feature.stories):
                spath = f"{path}.{si}"
                if f"keep:{spath}" not in form:
                    result._dropped.append(f"story '{story.title}'")
                    continue
                story._ai_title = _retitle(story, form.get(f"title:{spath}", ""))
                story._related = _related(form, spath)
                stories.append(story)
            if stories:
                feature.stories = stories
                features.append(feature)
        if features:
            epic.features = features
            epics.append(epic)
    result.epics = epics
    result._drop_reason = form.get("drop_reason", "").strip()
    return result


def _retitle(item, new: str) -> str:
    """Apply the reviewer's title; return the AI's title if it changed, else ""."""
    new = new.strip()
    if not new or new == item.title:
        return ""
    ai, item.title = item.title, new
    return ai


def unexplained_adjustments(approved: Decomposition) -> list[str]:
    """Features whose AI-drafted WSJF score the reviewer changed without saying why."""
    return [f.title for e in approved.epics for f in e.features
            if f._ai_wsjf is not None and f.wsjf is not None and not f._wsjf_reason
            and _parts(f.wsjf) != _parts(f._ai_wsjf)]


def _parts(w: WsjfEstimate) -> dict[str, int]:
    return {p: getattr(w, p) for p in WSJF_PARTS}


def parse_reviewed_notes(kind: str, originals: list[str],
                         items: Iterable[tuple[str, str]]) -> ReviewedNotes:
    """Read one kind of note from the review form.

    Originals arrive as `<kind>:<index>` (absent when the reviewer removed them); notes the
    reviewer added arrive as repeated `<kind>:new` fields. A blanked original counts as removed.
    """
    reviewed = ReviewedNotes(kind=kind, originals=list(originals))
    for name, value in items:
        if not name.startswith(f"{kind}:"):
            continue
        suffix = name.split(":", 1)[1]
        if suffix == "new":
            if value.strip():
                reviewed.added.append(value.strip())
        elif suffix.isdigit() and int(suffix) < len(originals) and value.strip():
            reviewed.kept[int(suffix)] = value.strip()
    return reviewed


def _edited_wsjf(current: WsjfEstimate | None, form: Mapping[str, str],
                 path: str) -> WsjfEstimate | None:
    values = {}
    for part in WSJF_PARTS:
        raw = form.get(f"wsjf:{path}:{part}", "")
        if raw.isdigit() and int(raw) in WSJF_SCALE:
            values[part] = int(raw)
    if not values:
        return current
    if current is None:
        if len(values) < len(WSJF_PARTS):
            return None
        return WsjfEstimate(**values, rationale="Scored by the reviewer.")
    return current.model_copy(update=values)


def _related(form: Mapping[str, str], path: str) -> list[str]:
    prefix = f"related:{path}:"
    return [name[len(prefix):] for name in form if name.startswith(prefix)]
