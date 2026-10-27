#!/usr/bin/env python3
"""Smoke test against a REAL Webots (a window will open).

    python tests/real_smoke.py --webots-home D:\\Webots [--webots-bin ...webotsw.exe] [--world ...agents.wbt]

Not an assertion suite: it drives the host through a typical session and
prints what each step returned, so problems with a real installation show up.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent.parent
SUPERVISOR_DIR = MCP_DIR / "supervisor"
EXE = ".exe" if os.name == "nt" else ""
ap = argparse.ArgumentParser()
ap.add_argument("--webots-home", required=True)
ap.add_argument("--webots-bin")
ap.add_argument("--world", default=str(MCP_DIR.parent.parent / "worlds" / "agents.wbt"))
ap.add_argument("--host-bin", default=str(MCP_DIR / "host" / "target" / "release" / f"webots-mcp-host{EXE}"))
ap.add_argument("--keep-open", action="store_true", help="do not stop Webots at the end")
args = ap.parse_args()

tmp = Path(tempfile.mkdtemp(prefix="webots-mcp-real-"))
env = dict(os.environ, WEBOTS_HOME=args.webots_home, WEBOTS_MCP_SUPERVISOR_DIR=str(SUPERVISOR_DIR),
           WEBOTS_MCP_STATE_DIR=str(tmp / "state"), WEBOTS_MCP_PROJECTS_DIR=str(tmp / "projects"),
           WEBOTS_MCP_PYTHON=sys.executable, WEBOTS_MCP_LOG="info", WEBOTS_MCP_IPC="127.0.0.1:47801")
if args.webots_bin:
    env["WEBOTS_BIN"] = args.webots_bin
log = tmp / "host.log"
host = subprocess.Popen([args.host_bin], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log, "w", encoding="utf-8"),
                        env=env, text=True, encoding="utf-8", bufsize=1)
_id = 0


def rpc(method, params=None):
    global _id
    _id += 1
    host.stdin.write(json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}) + "\n")
    host.stdin.flush()
    while True:
        line = host.stdout.readline()
        if not line:
            raise SystemExit("host closed stdout; log: " + str(log))
        msg = json.loads(line)
        if msg.get("id") == _id:
            return msg


def call(name, **kw):
    t0 = time.time()
    reply = rpc("tools/call", {"name": name, "arguments": kw})
    if "error" in reply:
        out = {"rpc_error": reply["error"]}
    else:
        try:
            out = json.loads(reply["result"]["content"][0]["text"])
        except ValueError:
            out = {"raw": reply["result"]["content"][0]["text"][:300]}
        out["_isError"] = reply["result"]["isError"]
    text = json.dumps(out, ensure_ascii=False)
    print(f"--- {name} {kw if kw else ''}  ({time.time() - t0:.1f}s)\n    {text[:600]}{'...' if len(text) > 600 else ''}", flush=True)
    return out


try:
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}})
    host.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    host.stdin.flush()
    d = call("sim_start", world=args.world, connect_timeout_s=120)
    if d.get("status") == "success":
        call("get_simulation_info")
        call("get_scene_tree")
        call("sim_step", count=10)
        # build a scene while paused: static floor + a falling dynamic box
        call("create_primitive", def_name="SMOKE_FLOOR", primitive_type="box", translation=[0, 0, -0.1], size=[4, 4, 0.2], mass=0.0, color=[0.6, 0.6, 0.6])
        call("create_primitive", def_name="SMOKE_BOX", primitive_type="box", translation=[0, 0, 1.0], size=[0.2, 0.2, 0.2], mass=1.0, color=[0.9, 0.2, 0.2])
        call("check_physics_health")
        call("get_node_state", def_name="SMOKE_BOX")
        # the host should auto-resume the paused world for the experiment and re-pause afterwards
        call("run_simulation", duration=1.5, target_defs=["SMOKE_BOX"], sample_interval=0.25, wait_timeout=60)
        call("sim_status")
        call("get_node_state", def_name="SMOKE_BOX")
        call("sim_resume", mode="real_time")
        time.sleep(1.0)
        call("sim_status")
        call("sim_pause")
        call("sim_reset", kind="simulation")
        call("get_node_state", def_name="SMOKE_BOX")  # created at run time -> gone after reset
        call("sim_status")
    if not args.keep_open:
        call("sim_stop")
finally:
    try:
        host.stdin.close()
        rc = host.wait(timeout=30)
    except Exception:
        host.kill()
        rc = -1
    print(f"\nhost exit code: {rc}\nhost log: {log}")
