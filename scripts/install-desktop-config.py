#!/usr/bin/env python3
"""Register eco-mcp-app in Claude Desktop's config.

Backs up the existing file with a timestamp, then adds an mcpServers.eco-mcp-app
entry that spawns `uv run python -m eco_mcp_app` from this project's root.

Usage: python scripts/install-desktop-config.py
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    cfg_path = (
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )
    if not cfg_path.exists():
        print(
            f"[eco-mcp-app] {cfg_path} does not exist — open Claude Desktop once first.",
            file=sys.stderr,
        )
        return 1

    backup = cfg_path.with_suffix(f".json.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(cfg_path, backup)
    print(f"[eco-mcp-app] backed up config to {backup.name}")

    cfg = json.loads(cfg_path.read_text())
    cfg.setdefault("mcpServers", {})

    repo = Path(__file__).resolve().parent.parent
    uv_path = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")

    cfg["mcpServers"]["eco-mcp-app"] = {
        "command": uv_path,
        "args": [
            "--directory",
            str(repo),
            "run",
            "python",
            "-m",
            "eco_mcp_app",
        ],
        "env": {},
    }

    cfg_path.write_text(json.dumps(cfg, indent=2))
    print(f"[eco-mcp-app] wrote {cfg_path.name}")
    print(f"[eco-mcp-app] uv = {uv_path}")
    print(f"[eco-mcp-app] repo = {repo}")
    print()
    print("Now fully quit Claude Desktop (⌘Q) and relaunch.")
    print("Then in a fresh chat: 'Use eco-mcp-app to show me the Eco server status.'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
