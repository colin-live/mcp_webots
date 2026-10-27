"""Webots Supervisor MCP package.

Importing the package must work outside Webots (project_manager.py and
export_manifest.py use `registry`), so `runtime` (which needs Webots'
`controller` module) is NOT imported here; import it explicitly where needed.
"""

from . import config  # ensure logging configured
from . import registry

__all__ = ["config", "registry"]
