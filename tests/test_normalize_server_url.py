"""URL normalization covers the shapes users actually type into Eco server lists."""

from __future__ import annotations

import pytest

from eco_mcp_app.server import DEFAULT_ECO_INFO_URL, normalize_server_url


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, DEFAULT_ECO_INFO_URL),
        ("", DEFAULT_ECO_INFO_URL),
        ("   ", DEFAULT_ECO_INFO_URL),
        ("eco.example.com", "http://eco.example.com:3001/info"),
        ("192.168.1.5", "http://192.168.1.5:3001/info"),
        ("10.0.0.5:4001", "http://10.0.0.5:4001/info"),
        ("eco.example.com:5679", "http://eco.example.com:5679/info"),
        ("http://eco.example.com:3001/info", "http://eco.example.com:3001/info"),
        ("https://eco.example.com/info", "https://eco.example.com:3001/info"),
        ("http://host:3001/", "http://host:3001/info"),
        ("http://host:3001", "http://host:3001/info"),
        ("  host:3001  ", "http://host:3001/info"),
    ],
)
def test_normalize_server_url(raw: str | None, expected: str) -> None:
    assert normalize_server_url(raw) == expected
