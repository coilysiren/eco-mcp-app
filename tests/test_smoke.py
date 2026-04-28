"""End-to-end smoke test for the HTTP app.

Covers the public-facing routes without spinning up a real network server:
  - `/healthz` for k8s probes
  - `/` for the tiny JSON landing page
  - `/preview` with upstream mocked (happy path + failure path)
  - `/mcp/` returns a sensible 4xx when called without a valid MCP client
    handshake (we only care that the route is mounted and reachable)
"""

from __future__ import annotations

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from eco_mcp_app import server as eco_server
from eco_mcp_app.http_app import create_app
from eco_mcp_app.server import DEFAULT_ECO_INFO_URL


@pytest.fixture(autouse=True)
def _clear_info_cache() -> None:
    eco_server._info_cache.clear()


_FAKE_INFO: dict[str, object] = {
    "Description": "<color=green>Eco</color> via <color=blue>Sirens</color>",
    "DetailedDescription": "Test server",
    "Category": "Test",
    "DiscordAddress": "https://discord.gg/example",
    "Version": "0.13.0.2",
    "Language": "English",
    "IsPaused": False,
    "HasPassword": False,
    "AdminOnline": True,
    "OnlinePlayers": 7,
    "TotalPlayers": 67,
    "ActiveAndOnlinePlayers": 7,
    "PeakActivePlayers": 38,
    "OnlinePlayersNames": ["alice", "bob"],
    "WorldSize": "0.52 km²",
    "Plants": 96000,
    "Animals": 0,
    "Laws": 3,
    "TotalCulture": 171.0,
    "DaysRunning": 2,
    "DaysUntilMeteor": 57,
    "HasMeteor": True,
    "CollaborationLevel": "HighCollaboration",
    "GameSpeed": "Slow",
    "SimulationLevel": "Full",
    "EconomyDesc": "473 trades, 0 contracts",
    "ExhaustionActive": False,
    "ExhaustionAfterHours": 0.0,
    "ExhaustionHoursGainPerWeekday": {},
    "Playtimes": "",
    "ServerAchievementsDict": {},
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_root_redirects_to_preview(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/preview"


def test_info(client: TestClient) -> None:
    r = client.get("/info")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "eco-mcp-app"
    assert body["mcp"] == "/mcp/"
    assert body["preview"] == "/preview"


@respx.mock
def test_preview_renders_card(client: TestClient) -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(200, json=_FAKE_INFO))
    r = client.get("/preview")
    assert r.status_code == 200
    html = r.text
    # Shell rendered
    assert "<html" in html.lower()
    # Card rendered (not the empty state)
    assert "Eco" in html
    # Player names are redacted
    assert "alice" not in html
    assert "bob" not in html


@respx.mock
def test_preview_handles_upstream_error(client: TestClient) -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(side_effect=httpx.ConnectError("refused"))
    r = client.get("/preview")
    assert r.status_code == 200
    # Error partial should be spliced in; shell still returned OK.
    assert "<html" in r.text.lower()


@respx.mock
def test_preview_json_returns_payload(client: TestClient) -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(200, json=_FAKE_INFO))
    r = client.get("/preview.json")
    assert r.status_code == 200
    body = r.json()
    # Payload shape comes from to_payload(); just sanity-check it's structured
    # data, not HTML, and that redaction still applies.
    assert isinstance(body, dict)
    assert "alice" not in r.text
    assert "<html" not in r.text.lower()


@respx.mock
def test_preview_json_upstream_error(client: TestClient) -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(side_effect=httpx.ConnectError("refused"))
    r = client.get("/preview.json")
    assert r.status_code == 502
    assert "error" in r.json()


@respx.mock
def test_preview_tool_json_suffix(client: TestClient) -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(200, json=_FAKE_INFO))
    r = client.get("/preview/get_eco_server_status.json")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert "<html" not in r.text.lower()


@respx.mock
def test_preview_forwards_server_arg(client: TestClient) -> None:
    route = respx.get("http://eco.example.com:5679/info").mock(
        return_value=httpx.Response(200, json=_FAKE_INFO)
    )
    r = client.get("/preview", params={"server": "eco.example.com:5679"})
    assert r.status_code == 200
    assert route.called


def test_mcp_mount_reachable() -> None:
    # No valid MCP handshake — we just want to prove the route is mounted
    # (not a 404). Using TestClient as context manager to engage lifespan,
    # which starts the StreamableHTTPSessionManager task group.
    with TestClient(create_app()) as c:
        r = c.get("/mcp/")
        assert r.status_code != 404
