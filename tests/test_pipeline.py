import json

import pytest

from backlog_pilot.llm import LLMError
from backlog_pilot.llm.base import input_key
from backlog_pilot.llm.replay import ReplayProvider
from backlog_pilot.models import Decomposition
from backlog_pilot.pipeline.decompose import apply_edits, decompose


def test_every_scenario_has_a_valid_replay(settings, scenarios):
    """Recordings are real model output, so they may contain defects; the quality checks
    must flag any story the review screen would otherwise show as ready without criteria."""
    from backlog_pilot.pipeline.quality import check_story, is_ready
    from backlog_pilot.pipeline.trace import trace_story

    provider = ReplayProvider(settings.replay_dir)
    for scenario in scenarios:
        draft = provider.decompose(scenario["text"])
        assert draft.epics and draft.story_count() > 0
        for _, story in draft.stories():
            if len(story.acceptance_criteria) < 2:
                trace = trace_story(scenario["text"], story.source_quotes)
                assert not is_ready(check_story(story, trace, set()))


def test_replay_files_match_their_input(settings):
    for path in settings.replay_dir.glob("*.json"):
        payload = json.loads(path.read_text())
        assert path.stem == input_key(payload["input"])
        Decomposition.model_validate(payload["decomposition"])


def test_input_key_ignores_whitespace_and_case():
    assert input_key("Hello   World\n") == input_key("hello world")


def test_replay_miss_is_a_friendly_error(settings):
    with pytest.raises(LLMError, match="Offline demo mode"):
        ReplayProvider(settings.replay_dir).decompose("x" * 100)


def test_decompose_rejects_thin_input(settings):
    with pytest.raises(LLMError, match="more detail"):
        decompose(ReplayProvider(settings.replay_dir), "fix PLM")


@pytest.fixture
def draft(settings, scenarios):
    return ReplayProvider(settings.replay_dir).decompose(scenarios[0]["text"])


def test_apply_edits_keeps_only_ticked_stories_and_renames(draft):
    form = {"keep:0.0.0": "on", "title:0.0.0": "Renamed story", "title:0": "Renamed epic"}
    result = apply_edits(draft, form)
    assert result.epics[0].title == "Renamed epic"
    assert result.story_count() == 1
    assert result.epics[0].features[0].stories[0].title == "Renamed story"
    # the original draft is untouched
    assert draft.story_count() > 1


def test_apply_edits_drops_empty_features_and_epics(draft):
    assert apply_edits(draft, {}).epics == []


def test_apply_edits_ignores_blank_titles(draft):
    original = draft.epics[0].title
    result = apply_edits(draft, {"keep:0.0.0": "on", "title:0": "   "})
    assert result.epics[0].title == original


def test_parse_reviewed_notes_tracks_kept_edited_removed_and_added():
    from backlog_pilot.pipeline.decompose import parse_reviewed_notes

    items = [("assumption:0", "A"), ("assumption:2", "C edited"), ("assumption:new", "D"),
             ("assumption:new", "  "), ("assumption:9", "out of range"), ("question:0", "Q")]
    reviewed = parse_reviewed_notes("assumption", ["A", "B", "C"], items)
    assert reviewed.kept == {0: "A", 2: "C edited"}
    assert reviewed.added == ["D"]


def test_materiality_rules():
    from backlog_pilot.intakes import is_material

    assert not is_material("assumption", "created", by_ai=True)
    for change in ["created", "edited", "removed", "restored"]:
        assert is_material("assumption", change, by_ai=False)
    assert is_material("question", "answered", by_ai=False, answer_changed=True)
    for change in ["created", "edited", "removed"]:
        assert not is_material("question", change, by_ai=False)
