"""Stdio entrypoint. Spawned by Claude Desktop per `claude_desktop_config.json`."""

from __future__ import annotations

import asyncio
import os
import sys

from .server import serve


def main() -> None:
    # Optional RPC tee for debugging which host is talking and what it advertises.
    # Mirrors kapwing-mcp-app's KAPWING_MCP_RPC_LOG pattern.
    log_path = os.environ.get("ECO_MCP_APP_RPC_LOG")
    if log_path:
        # Only tee outbound; stdin handling by the mcp SDK uses its own reader.
        orig_write = sys.stdout.buffer.write
        with open(log_path, "ab") as log:

            def tee(data: bytes) -> int:
                log.write(data)
                log.flush()
                return orig_write(data)

            sys.stdout.buffer.write = tee  # type: ignore[method-assign]
            asyncio.run(serve())
    else:
        asyncio.run(serve())


if __name__ == "__main__":
    main()
