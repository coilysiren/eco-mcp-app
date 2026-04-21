"""Dev-only browser livereload via websocket. No-op unless DEBUG is set.

Pairs with `uvicorn --reload`:
  1. uvicorn restarts the Python process on `.py` changes.
  2. `watchfiles.awatch` sends "reload" on any template / asset change.
  3. The client's `onclose → reconnect` loop survives the uvicorn restart —
     once reconnected, the next file change triggers the browser refresh.

Jinja2's `auto_reload` already picks up template edits server-side without a
process restart; this module's job is to close the loop by telling any open
browser tab to re-fetch.
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from watchfiles import awatch

DEBUG: bool = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

# Watch the package root — covers server.py, http_app.py, templates/, assets/.
# Don't widen to the repo root (pulls in investigation/, .venv/, etc).
_PKG_ROOT = Path(__file__).resolve().parent
WATCH_PATHS: tuple[Path, ...] = (_PKG_ROOT,)


async def _livereload_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    try:
        async for _ in awatch(*(str(p) for p in WATCH_PATHS)):
            await ws.send_text("reload")
    except WebSocketDisconnect:
        pass


livereload_route = WebSocketRoute("/ws/livereload", _livereload_endpoint)


# Injected into the rendered shell only when DEBUG is set. The reconnect loop
# is load-bearing: without it, the first uvicorn --reload restart kills the
# socket and livereload silently stops working.
LIVERELOAD_SCRIPT = """
<script>
(() => {
  const connect = () => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + "/ws/livereload");
    ws.onmessage = (e) => { if (e.data === "reload") location.reload(); };
    ws.onclose = () => setTimeout(connect, 500);
  };
  connect();
})();
</script>
"""
