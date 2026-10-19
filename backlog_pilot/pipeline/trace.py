"""Trace stories back to the stakeholder's own words.

The model cites verbatim quotes; this module checks each one against the source text
deterministically, so a story's "traced" label never rests on the model's say-so.
"""

import re
from dataclasses import dataclass, field

TRACED, UNVERIFIED, INFERRED = "traced", "unverified", "inferred"

_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_EDGE = " \t\n\"'“”‘’.,;:"


def _pattern(fragment: str) -> re.Pattern | None:
    words = fragment.strip(_EDGE).split()
    if not words:
        return None
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)


def find_spans(source: str, quote: str) -> list[tuple[int, int]] | None:
    """Character spans of the quote in the source, or None if any part is missing.

    Matching ignores case, whitespace differences, and surrounding punctuation; a quote
    that elides text with an ellipsis must match each part, in order.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for fragment in _ELLIPSIS.split(quote):
        pattern = _pattern(fragment)
        if pattern is None:
            continue
        match = pattern.search(source, start)
        if match is None:
            return None
        spans.append(match.span())
        start = match.end()
    return spans or None


@dataclass
class StoryTrace:
    status: str
    quotes: list[tuple[str, bool]]  # (quote, found in source)


def trace_story(source: str, quotes: list[str]) -> StoryTrace:
    checked = [(q, find_spans(source, q) is not None) for q in quotes if q.strip()]
    if not checked:
        return StoryTrace(INFERRED, [])
    return StoryTrace(TRACED if any(ok for _, ok in checked) else UNVERIFIED, checked)


@dataclass
class Segment:
    text: str
    paths: list[str] = field(default_factory=list)  # stories citing this stretch of text


def highlight(source: str, quotes_by_path: dict[str, list[str]]) -> list[Segment]:
    """Split the source into segments tagged with the stories that cite them."""
    boundaries = {0, len(source)}
    cited: list[tuple[int, int, str]] = []
    for path, quotes in quotes_by_path.items():
        for quote in quotes:
            for begin, end in find_spans(source, quote) or []:
                cited.append((begin, end, path))
                boundaries.update((begin, end))
    points = sorted(boundaries)
    segments = []
    for begin, end in zip(points, points[1:], strict=False):
        paths = sorted({p for b, e, p in cited if b <= begin and end <= e})
        if segments and segments[-1].paths == paths:
            segments[-1].text += source[begin:end]
        else:
            segments.append(Segment(source[begin:end], paths))
    return segments


def coverage(traces: dict[str, StoryTrace]) -> dict[str, int]:
    counts = {TRACED: 0, UNVERIFIED: 0, INFERRED: 0}
    for t in traces.values():
        counts[t.status] += 1
    return counts
