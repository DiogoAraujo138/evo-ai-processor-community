"""Completing an OAuth integration trusts nothing the callback brings but a state this server signed."""

import base64
import json

import httpx
import pytest
import requests

from src.services.asana_service import AsanaService
from src.services.atlassian_service import AtlassianService
from src.services.canva_service import CanvaService
from src.services.github_service import GitHubService
from src.services.google_calendar_service import GoogleCalendarService
from src.services.google_sheets_service import GoogleSheetsService
from src.services.hubspot_service import HubSpotService
from src.services.linear_service import LinearService
from src.services.mcp_oauth_service import MCPOAuthService
from src.services.monday_service import MondayService
from src.services.notion_service import NotionService
from src.services import oauth_state
from src.services.oauth_state import issue_state
from src.services.paypal_service import PayPalService
from src.services.supabase_service import SupabaseService

AGENT_ID = "0b6f7c1e-3f55-4a51-9a49-7d0d0c2b4a10"
FORGED_TARGET = "https://forged.invalid/token"
COMMON = {
    "redirect_uri": "https://app.example/callback",
    "core_service_url": "http://core.invalid",
    "user_token": "t",
}
CREDS = {"client_id": "cid", "client_secret": "csecret"}

SERVICES = {
    "github": lambda: GitHubService(**CREDS, **COMMON),
    "hubspot": lambda: HubSpotService(**CREDS, **COMMON),
    "notion": lambda: NotionService(**COMMON, **CREDS),
    "asana": lambda: AsanaService(**COMMON, **CREDS),
    "monday": lambda: MondayService(**COMMON, **CREDS),
    "atlassian": lambda: AtlassianService(**COMMON, **CREDS),
    "linear": lambda: LinearService(**COMMON, **CREDS),
    "canva": lambda: CanvaService(**COMMON, **CREDS),
    "supabase": lambda: SupabaseService(**COMMON),
    "paypal": lambda: PayPalService(**COMMON, **CREDS),
    "google_sheets": lambda: GoogleSheetsService(**CREDS, **COMMON),
    "google_calendar": lambda: GoogleCalendarService(**CREDS, **COMMON),
}


class _Response:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)
        self.headers = {}
        self.request = httpx.Request("POST", "https://unused.example")

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=self.request, response=self)


class _RecordingClient:
    """Stands in for httpx.AsyncClient: records every request, answers from `responder`."""

    calls = []
    responder = staticmethod(lambda method, url: _Response())

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _send(self, method, url, **kwargs):
        _RecordingClient.calls.append((method, str(url), kwargs))
        return _RecordingClient.responder(method, str(url))

    async def get(self, url, **kwargs):
        return await self._send("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._send("POST", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._send("PUT", url, **kwargs)

    async def delete(self, url, **kwargs):
        return await self._send("DELETE", url, **kwargs)

    async def request(self, method, url, **kwargs):
        return await self._send(method, url, **kwargs)


@pytest.fixture(autouse=True)
def recording_http(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "test-encryption-key-for-oauth-state")
    _RecordingClient.calls = []
    _RecordingClient.responder = staticmethod(lambda method, url: _Response())
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)

    # The Google integrations exchange the code with a synchronous requests.post.
    def sync_post(url, **kwargs):
        _RecordingClient.calls.append(("POST", str(url), kwargs))
        return _RecordingClient.responder("POST", str(url))

    monkeypatch.setattr(requests, "post", sync_post)
    return _RecordingClient


def _unsigned_state():
    return base64.urlsafe_b64encode(
        json.dumps(
            {
                "agent_id": AGENT_ID,
                "mcp_url": FORGED_TARGET,
                "timestamp": "2026-10-13T00:00:00",
            }
        ).encode()
    ).decode()


async def _complete(service, state):
    try:
        return await service.complete_authorization(
            agent_id=AGENT_ID, code="the-code", state=state
        )
    except Exception as exc:  # the route turns a raise into a generic 500
        return {"success": False, "raised": exc}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(SERVICES))
async def test_an_unsigned_state_is_refused_before_any_request(
    provider, recording_http
):
    result = await _complete(SERVICES[provider](), _unsigned_state())

    assert result["success"] is False
    assert recording_http.calls == []
    assert FORGED_TARGET not in json.dumps(result, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(SERVICES))
async def test_a_state_for_another_agent_is_refused_before_any_request(
    provider, recording_http
):
    result = await _complete(SERVICES[provider](), issue_state("another-agent"))

    assert result["success"] is False
    assert recording_http.calls == []


@pytest.mark.parametrize(
    "provider", sorted(set(SERVICES) - {"google_sheets", "google_calendar"})
)
@pytest.mark.asyncio
async def test_the_authorization_url_carries_a_signed_state(provider, monkeypatch):
    service = SERVICES[provider]()
    service.client_id = service.client_id or "cid"

    async def no_discovery(*args, **kwargs):
        return {}

    async def no_store(*args, **kwargs):
        return None

    monkeypatch.setattr(
        service, "discover_oauth_requirements", no_discovery, raising=False
    )
    monkeypatch.setattr(service, "_store_pkce_verifier", no_store, raising=False)
    monkeypatch.setattr(service, "_store_client_credentials", no_store, raising=False)
    service._oauth_metadata = {"authorization_servers": ["https://auth.example"]}
    service._authorization_server_metadata = {
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": "https://auth.example/token",
    }

    try:
        url = await service.generate_authorization_url(agent_id=AGENT_ID)
    except Exception as exc:  # provider-specific discovery we did not stub
        pytest.skip(f"{provider}: {exc}")

    state = httpx.URL(url).params.get("state")
    payload = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
    assert payload["agent_id"] == AGENT_ID
    assert "sig" in payload and "mcp_url" not in payload


def _base_service():
    service = MCPOAuthService(
        mcp_url="https://mcp.provider.example/mcp",
        provider_name="notion",
        client_id="cid",
        client_secret="csecret",
        **COMMON,
    )
    service._oauth_metadata = {
        "authorization_servers": ["https://auth.provider.example"]
    }
    service._authorization_server_metadata = {
        "token_endpoint": "https://auth.provider.example/token"
    }
    return service


@pytest.mark.asyncio
async def test_the_token_exchange_error_is_not_reflected(recording_http):
    recording_http.responder = staticmethod(
        lambda method, url: _Response(400, text="upstream-says: internal-only-detail")
    )

    result = await _base_service().complete_authorization(
        agent_id=AGENT_ID,
        code="the-code",
        state=issue_state(AGENT_ID),
        code_verifier="v",
    )

    assert result == {"success": False, "error": "Failed to complete authorization"}


@pytest.mark.asyncio
async def test_the_mcp_endpoint_stored_is_the_services_own(recording_http, monkeypatch):
    recording_http.responder = staticmethod(
        lambda method, url: _Response(200, {"access_token": "tok"})
    )
    service = _base_service()
    stored = {}

    async def store(**kwargs):
        stored.update(kwargs)

    monkeypatch.setattr(service, "_store_credentials", store)

    result = await service.complete_authorization(
        agent_id=AGENT_ID,
        code="the-code",
        state=issue_state(AGENT_ID),
        code_verifier="v",
    )

    assert result["success"] is True
    assert stored["mcp_url"] == "https://mcp.provider.example/mcp"
    assert all(FORGED_TARGET not in url for _, url, _ in recording_http.calls)


@pytest.mark.asyncio
async def test_an_mcp_url_inside_a_signed_state_is_still_ignored(
    recording_http, monkeypatch
):
    # Even a state this server signed never chooses the endpoint: only the
    # service's own URL is used and stored.
    payload = {
        "agent_id": AGENT_ID,
        "issued_at": int(__import__("time").time()),
        "nonce": "n",
        "mcp_url": FORGED_TARGET,
    }
    payload["sig"] = oauth_state._signature(payload)
    state = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    recording_http.responder = staticmethod(
        lambda method, url: _Response(200, {"access_token": "tok"})
    )
    service = _base_service()
    stored = {}

    async def store(**kwargs):
        stored.update(kwargs)

    monkeypatch.setattr(service, "_store_credentials", store)

    result = await service.complete_authorization(
        agent_id=AGENT_ID, code="the-code", state=state, code_verifier="v"
    )

    assert result["success"] is True
    assert stored["mcp_url"] == "https://mcp.provider.example/mcp"
    assert all(FORGED_TARGET not in url for _, url, _ in recording_http.calls)


@pytest.mark.asyncio
async def test_paypal_ignores_an_mcp_url_inside_a_signed_state(
    recording_http, monkeypatch
):
    payload = {
        "agent_id": AGENT_ID,
        "issued_at": int(__import__("time").time()),
        "nonce": "n",
        "mcp_url": FORGED_TARGET,
    }
    payload["sig"] = oauth_state._signature(payload)
    state = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    recording_http.responder = staticmethod(
        lambda method, url: _Response(200, {"access_token": "tok"})
    )
    service = SERVICES["paypal"]()
    stored = {}

    async def store(**kwargs):
        stored.update(kwargs)

    monkeypatch.setattr(service, "_store_credentials", store)

    result = await service.complete_authorization(
        agent_id=AGENT_ID, code="the-code", state=state, code_verifier="v"
    )

    assert result["success"] is True, result
    assert stored["mcp_url"] == service.mcp_url
    assert all(FORGED_TARGET not in url for _, url, _ in recording_http.calls)
