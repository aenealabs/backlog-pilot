import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from authlib.integrations.starlette_client.apps import StarletteOAuth2App
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

from backlog_pilot.app import create_app
from backlog_pilot.auth import AuthSettings, user_from_claims

AUTH = AuthSettings("oidc", "https://login.example/.well-known/openid-configuration",
                    "client", "secret", "session-secret-for-tests",
                    approver_emails=frozenset({"lead@corp.example"}), https_only=False)


def test_user_from_claims_maps_roles_and_allow_list():
    approver = user_from_claims({"sub": "1", "oid": "o1", "name": "Pat",
                                 "email": "Pat@Corp.Example",
                                 "roles": ["BacklogPilot.Approver"]}, AUTH)
    assert approver.approver and approver.email == "pat@corp.example" and approver.id == "o1"
    listed = user_from_claims({"sub": "2", "preferred_username": "lead@corp.example"}, AUTH)
    assert listed.approver and listed.name == "lead@corp.example"
    reader = user_from_claims({"sub": "3", "name": "Kim", "email": "kim@corp.example"}, AUTH)
    assert not reader.approver and reader.actor == "Kim (Contributor)"


@pytest.fixture
def signed_in_client(settings, engine, monkeypatch):
    claims = {}

    async def fake_redirect(self, request, redirect_uri, **kwargs):
        return RedirectResponse(f"https://login.example/authorize?redirect_uri={redirect_uri}")

    async def fake_token(self, request, **kwargs):
        return {"userinfo": claims}

    monkeypatch.setattr(StarletteOAuth2App, "authorize_redirect", fake_redirect)
    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", fake_token)
    client = TestClient(create_app(settings, engine=engine, auth=AUTH), follow_redirects=False)
    return client, claims


def test_pages_require_sign_in(signed_in_client):
    client, _ = signed_in_client
    response = client.get("/roadmap")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.post("/decompose", data={"text": "x"}).status_code == 401
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/suite-static/suite.css").status_code == 200  # shared styles, public
    login = client.get("/login")
    assert login.headers["location"].startswith("https://login.example/authorize")
    assert "/auth/callback" in login.headers["location"]


def test_sign_in_returns_to_page_and_roles_gate_decisions(signed_in_client):
    client, claims = signed_in_client
    claims.update({"sub": "u1", "name": "Kim Reader", "email": "kim@corp.example"})
    client.get("/priorities")  # remembered as the page to return to
    response = client.get("/auth/callback?code=c&state=s")
    assert response.status_code == 303 and response.headers["location"] == "/priorities"
    page = client.get("/priorities").text
    assert "Kim Reader · Contributor" in page and "Sign out" in page
    assert client.post("/roadmap/plm/capacity", data={}).status_code == 403
    assert "signed_in" in client.get("/audit").text

    client.get("/logout")
    claims.clear()
    claims.update({"sub": "u2", "name": "Pat Approver", "email": "pat@corp.example",
                   "roles": ["BacklogPilot.Approver"]})
    client.get("/auth/callback?code=c&state=s")
    assert client.post("/roadmap/plm/capacity", data={}).status_code == 303


# ---------- Microsoft Foundry ----------

def test_foundry_provider_request_shape(monkeypatch):
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured["path"] = self.path
            captured["headers"] = {k.lower(): v for k, v in self.headers.items()}
            captured["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "azure-key")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL",
                       f"http://127.0.0.1:{server.server_port}/anthropic/")
    from backlog_pilot.llm import LLMError
    from backlog_pilot.llm.foundry import foundry_provider

    provider = foundry_provider("my-claude-deployment")
    provider.client = provider.client.with_options(max_retries=0)
    with pytest.raises(LLMError):
        provider.decompose("ECO approvals take nineteen days and engineers cannot see why.")
    server.shutdown()
    body = captured["body"]
    assert captured["path"].startswith("/anthropic/v1/messages")
    assert body["model"] == "my-claude-deployment"
    assert "fallbacks" not in body  # not available on Foundry; client-side middleware instead
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert captured["headers"].get("x-api-key") == "azure-key"
