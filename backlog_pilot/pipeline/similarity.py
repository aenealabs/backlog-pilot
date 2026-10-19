"""Find existing backlog items a draft may duplicate.

Plain TF-IDF cosine similarity over titles and descriptions: deterministic, explainable,
and dependency-free. It catches shared vocabulary, not synonyms, so matches are suggestions
with a score and the reviewer decides whether items are related.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from backlog_pilot.models import Decomposition

POSSIBLE, LIKELY = 0.18, 0.50  # cosine thresholds, tuned on the demo scenarios
MAX_MATCHES = 2

_STOP = set("""a an the and or of to for in on at by with from into is are be as it its this
that these those their our your my we i you they them can will should must may when then than
so not no all any each every per via using use used new existing who what where which how one
two own make makes so_that want""".split())


def tokens(text: str) -> list[str]:
    out = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _STOP or len(word) < 2:
            continue
        for suffix in ("ing", "ed", "es", "s"):  # crude stemming: "transfers" ~ "transfer"
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
        out.append(word)
    return out


@dataclass(frozen=True)
class Candidate:
    """An existing backlog item that new work could overlap."""
    key: str
    item_type: str  # feature | story
    title: str
    text: str
    status: str


@dataclass(frozen=True)
class Match:
    key: str
    title: str
    status: str
    score: float

    @property
    def likely(self) -> bool:
        return self.score >= LIKELY

    @property
    def percent(self) -> int:
        return round(self.score * 100)


def _vectors(docs: list[list[str]]) -> list[dict[str, float]]:
    df = Counter(w for doc in docs for w in set(doc))
    n = len(docs)
    vectors = []
    for doc in docs:
        weights = {w: c * math.log((1 + n) / (1 + df[w]) + 1) for w, c in Counter(doc).items()}
        norm = math.sqrt(sum(x * x for x in weights.values())) or 1.0
        vectors.append({w: x / norm for w, x in weights.items()})
    return vectors


def find_overlaps(draft: Decomposition, candidates: list[Candidate]) -> dict[str, list[Match]]:
    """Possible overlaps for each draft feature ('e.f') and story ('e.f.s'), best first.

    Features are compared with existing features and stories with existing stories.
    """
    items: list[tuple[str, str, str]] = []  # (path, type, text)
    for ei, epic in enumerate(draft.epics):
        for fi, feature in enumerate(epic.features):
            items.append((f"{ei}.{fi}", "feature", f"{feature.title} {feature.description}"))
            for si, story in enumerate(feature.stories):
                items.append((f"{ei}.{fi}.{si}", "story", f"{story.title} {story.i_want}"))
    if not items or not candidates:
        return {}
    vectors = _vectors([tokens(t) for _, _, t in items] + [tokens(c.text) for c in candidates])
    draft_vecs, cand_vecs = vectors[: len(items)], vectors[len(items):]
    overlaps: dict[str, list[Match]] = {}
    for (path, item_type, _), v in zip(items, draft_vecs, strict=True):
        scored = []
        for c, cv in zip(candidates, cand_vecs, strict=True):
            if c.item_type != item_type:
                continue
            score = sum(x * cv.get(w, 0.0) for w, x in v.items())
            if score >= POSSIBLE:
                scored.append(Match(c.key, c.title, c.status, round(score, 2)))
        if scored:
            overlaps[path] = sorted(scored, key=lambda m: -m.score)[:MAX_MATCHES]
    return overlaps


def find_overlaps_text(text: str, candidates: list[Candidate],
                       minimum: float = POSSIBLE) -> list[Match]:
    """Candidates most similar to one piece of text, best first (any candidate type)."""
    if not candidates:
        return []
    vectors = _vectors([tokens(text)] + [tokens(c.text) for c in candidates])
    v, cand_vecs = vectors[0], vectors[1:]
    scored = []
    for c, cv in zip(candidates, cand_vecs, strict=True):
        score = sum(x * cv.get(w, 0.0) for w, x in v.items())
        if score > minimum or (minimum == 0.0 and score > 0):
            scored.append(Match(c.key, c.title, c.status, round(score, 2)))
    return sorted(scored, key=lambda m: -m.score)
