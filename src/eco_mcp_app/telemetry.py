"""Sentry SDK init for eco-mcp-app.

Mirrors the pattern in coilysiren/backend's telemetry.py. Called at process
startup from both the stdio entrypoint (__main__.py) and the ASGI entrypoint
(http_app.py) so errors get captured regardless of transport.

When SENTRY_DSN is set, initializes a real client with Starlette + FastAPI
integrations. Otherwise falls back to `sentry_sdk.init()` with no DSN, which
is a no-op that swallows captures silently. This keeps local dev quiet and
the production pod wired in via the ExternalSecret in deploy/main.yml.
"""

from __future__ import annotations

import os

import sentry_sdk
import sentry_sdk.integrations.fastapi as sentry_fastapi
import sentry_sdk.integrations.starlette as sentry_starlette

_initialized = False


def init_sentry() -> None:
    """Idempotent. Safe to call from multiple entry points in the same process."""
    global _initialized
    if _initialized:
        return
    dsn = os.getenv("SENTRY_DSN")
    if dsn:
        sentry_sdk.init(
            dsn=dsn,
            integrations=[
                sentry_starlette.StarletteIntegration(),
                sentry_fastapi.FastApiIntegration(),
            ],
        )
    else:
        sentry_sdk.init()
    _initialized = True
