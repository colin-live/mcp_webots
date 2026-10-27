"""Pretends to be webots(.exe) in the end-to-end tests.

Runs the REAL controller (mcp_server.py + webots_mcp) against the stub
`controller` module, and restarts it when it asks for a world reload, like
Webots does.  Gives up after FAKE_WEBOTS_MAX_SECONDS so nothing can leak.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ["FAKE_WEBOTS_STATE_DIR"]
DEADLINE = time.time() + float(os.environ.get("FAKE_WEBOTS_MAX_SECONDS", "300"))

CHILD = (
    "import os, runpy, sys\n"
    f"sys.path.insert(0, {HERE!r})\n"                      # stub `controller`
    f"sys.path.insert(1, {os.environ['MCP_SERVER_DIR']!r})\n"  # real webots_mcp
    f"runpy.run_path(os.path.join({os.environ['MCP_SERVER_DIR']!r}, 'mcp_server.py'), run_name='__main__')\n"
)

print("fake webots args:", sys.argv[1:], flush=True)
for name in ("reload.flag", "quit.flag", "stall.flag"):
    try:
        os.remove(os.path.join(STATE, name))
    except OSError:
        pass

while time.time() < DEADLINE:
    rc = subprocess.call([sys.executable, "-c", CHILD], cwd=STATE)
    if os.path.exists(os.path.join(STATE, "quit.flag")):
        print("fake webots: quit", flush=True)
        sys.exit(0)
    if os.path.exists(os.path.join(STATE, "reload.flag")):
        os.remove(os.path.join(STATE, "reload.flag"))
        time.sleep(0.4)
        if os.path.exists(os.path.join(STATE, "stall.flag")):
            # Real Webots quirk: a reloaded world that stays paused never launches its
            # controller, so the process lives on with no supervisor.
            os.remove(os.path.join(STATE, "stall.flag"))
            print("fake webots: reload (controller NOT restarted)", flush=True)
            while time.time() < DEADLINE:
                time.sleep(0.2)
            sys.exit(3)
        print("fake webots: reload", flush=True)
        continue
    print("fake webots: controller exited rc=%s" % rc, flush=True)
    sys.exit(1)
sys.exit(2)
