import logging
import os
from pathlib import Path

# ============================================================
# Configuration
# ============================================================

SERVER_VERSION = "2.2.0"

HOST = os.environ.get("WEBOTS_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBOTS_MCP_PORT", "8000"))

COMMAND_TIMEOUT = 20.0
MAX_COMMANDS_PER_STEP = 16
MAX_PENDING_COMMANDS = 256
MAX_VRML_LENGTH = 100_000
MAX_TREE_NODES = 5000
MAX_TREE_DEPTH = 20

MAX_SIMULATION_HISTORY = 10
MAX_SIMULATION_TARGETS = 100
MAX_SAMPLE_VALUES = 200_000  # frames * targets kept per observation

SAVE_DIR = Path(os.environ.get("WEBOTS_MCP_SAVE_DIR", os.getcwd())).resolve()
SCREENSHOT_DIR = SAVE_DIR / "screenshots"

# Root node types that exist once per world.
SINGLETON_ROOT_TYPES = ("Background", "WorldInfo", "Viewpoint")
PROTECTED_ROOT_TYPES = ("WorldInfo", "Viewpoint")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("webots-mcp")


# ============================================================
# IPC to the Rust host / simulation clock
# ============================================================

# The host listens here. Webots (and this controller) inherit the environment of
# the process that launched Webots, so the host passes the address per launch.
IPC_ADDR = os.environ.get("WEBOTS_MCP_IPC", "127.0.0.1:47800")
IPC_TOKEN = os.environ.get("WEBOTS_MCP_TOKEN") or None

IDLE_SLEEP = float(os.environ.get("WEBOTS_MCP_IDLE_SLEEP", "0.005"))  # s, while paused
# Follow Play/Pause clicked in the Webots GUI (experimental, off by default).
SYNC_GUI = os.environ.get("WEBOTS_MCP_SYNC_GUI", "0") == "1"
