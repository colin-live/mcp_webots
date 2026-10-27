#!/usr/bin/env python3
"""Export the Supervisor tool manifest without Webots.

    python export_manifest.py <out.json>

Gives the Rust host a tool list before Webots has ever run: put the file at
<state_dir>/supervisor_tools.json (the host also rewrites it on every connect).
A stand-in `controller` module is injected, so nothing here talks to Webots.
"""
import json
import os
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("controller", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import supervisor.webots_mcp.tools  # noqa: E402,F401  (registers the tools)
from webots_mcp import registry  # noqa: E402

text = json.dumps(registry.export_manifest(), ensure_ascii=False, indent=2)
if len(sys.argv) > 1:
    os.makedirs(os.path.dirname(os.path.abspath(sys.argv[1])), exist_ok=True)
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {len(registry.TOOL_NAMES)} tools to {sys.argv[1]}")
else:
    print(text)
