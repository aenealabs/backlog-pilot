import html
import json
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backlog_pilot.app import create_app
from backlog_pilot.config import load_settings
from backlog_pilot.db import make_engine
from backlog_pilot.intakes import IntakeStore
from backlog_pilot.trackers import MockTracker


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_LLM_PROVIDER", "replay")
    monkeypatch.setenv("BP_RECORD", "false")
    return replace(load_settings(), db_path=str(tmp_path / "test.db"))


@pytest.fixture
def scenarios(settings):
    return [s for s in json.loads(settings.scenarios_path.read_text())["scenarios"]
            if not s.get("draft")]


@pytest.fixture
def engine(settings):
    return make_engine(settings.db_path)


@pytest.fixture
def tracker(engine):
    return MockTracker(engine)


@pytest.fixture
def intakes(engine):
    return IntakeStore(engine)


@pytest.fixture
def client(settings, engine):
    return TestClient(create_app(settings, engine=engine))


def payload_from(review_html: str) -> str:
    return html.unescape(re.search(r'name="payload" value="([^"]*)"', review_html).group(1))


def source_from(review_html: str) -> str:
    return html.unescape(re.search(r'name="source_text" value="([^"]*)"', review_html).group(1))
