"""fetch_eco_info failure paths + URL wiring.

Uses respx to intercept httpx so we verify the normalized URL is what we hit.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from eco_mcp_app import server as eco_server
from eco_mcp_app.server import DEFAULT_ECO_INFO_URL, fetch_eco_info


@pytest.fixture(autouse=True)
def _clear_info_cache() -> None:
    eco_server._info_cache.clear()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_default_server_appends_source_url() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(
        return_value=httpx.Response(200, json={"Description": "Eco via Sirens"})
    )
    data = await fetch_eco_info()
    assert data["Description"] == "Eco via Sirens"
    assert data["_sourceUrl"] == DEFAULT_ECO_INFO_URL


@pytest.mark.asyncio
@respx.mock
async def test_fetch_normalizes_host_only() -> None:
    respx.get("http://eco.example.com:3001/info").mock(
        return_value=httpx.Response(200, json={"Description": "x"})
    )
    data = await fetch_eco_info("eco.example.com")
    assert data["_sourceUrl"] == "http://eco.example.com:3001/info"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_raises_on_5xx() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_eco_info()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_raises_on_connect_error() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        await fetch_eco_info()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_raises_on_timeout() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        await fetch_eco_info()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_caches_within_ttl() -> None:
    route = respx.get(DEFAULT_ECO_INFO_URL).mock(
        return_value=httpx.Response(200, json={"Description": "cached"})
    )
    await fetch_eco_info()
    await fetch_eco_info()
    await fetch_eco_info()
    assert route.call_count == 1
