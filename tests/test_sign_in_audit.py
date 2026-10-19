"""With single sign-on on, refused attempts by people who are not signed in are audited too."""

from dataclasses import replace

import pytest
from authlib.integrations.starlette_client.apps import StarletteOAuth2App
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backlog_pilot.app import create_app
from backlog_pilot.auth import AuthSettings
from backlog_pilot.config import load_settings
from backlog_pilot.db import AuditEvent, make_engine

AUTH = AuthSettings("oidc", "https://login.example/.well-known/openid-configuration",
                    "client", "secret", "session-secret-for-tests", https_only=False)


@pytest.fixture
def app(tmp_path, monkeypatch):
    async def bad_token(self, request, **kwargs):
        raise ValueError("state mismatch")

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", bad_token)
    settings = replace(load_settings(), db_path=str(tmp_path / "t.db"))
    engine = make_engine(settings.db_path)
    return TestClient(create_app(settings, engine=engine, auth=AUTH),
                      follow_redirects=False), engine


def events(engine, action):
    with Session(engine) as s:
        return s.exec(select(AuditEvent).where(AuditEvent.action == action)).all()


def test_actions_without_signing_in_and_failed_sign_ins_are_audited(app):
    client, engine = app
    assert client.get("/").status_code == 303  # a page view is sent to sign in, not refused
    assert events(engine, "access_denied") == []
    assert client.post("/anything", data={"x": "1"}).status_code == 401
    denied = events(engine, "access_denied")
    assert len(denied) == 1 and denied[0].actor == "not signed in"
    assert "POST /anything" in denied[0].detail
    failed = client.get("/auth/callback?code=x&state=y")
    assert failed.status_code == 401 and "Sign-in failed" in failed.text
    assert "ValueError" in events(engine, "sign_in_failed")[0].detail
