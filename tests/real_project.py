#!/usr/bin/env python3
"""REAL Webots: project_create -> world_add_robot -> sim_start -> reload -> stop.

    python tests/real_project.py --webots-home D:\\Webots

Checks that a project built by project_manager (world file, EXTERNPROTO,
robot, linked supervisor launcher) really loads in Webots and that the
supervisor binds and survives a world reload.  A Webots window opens.
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
ap.add_argument("--robot", default="Pioneer3dx")
ap.add_argument("--host-bin", default=str(MCP_DIR / "host" / "target" / "release" / f"webots-mcp-host{EXE}"))
args = ap.parse_args()

tmp = Path(tempfile.mkdtemp(prefix="webots-mcp-proj-"))
env = dict(os.environ, WEBOTS_HOME=args.webots_home, WEBOTS_MCP_SUPERVISOR_DIR=str(SUPERVISOR_DIR),
           WEBOTS_MCP_STATE_DIR=str(tmp / "state"), WEBOTS_MCP_PROJECTS_DIR=str(tmp / "projects"),
           WEBOTS_MCP_PYTHON=sys.executable, WEBOTS_MCP_IPC="127.0.0.1:47803")
if args.webots_bin:
    env["WEBOTS_BIN"] = args.webots_bin
log = tmp / "host.log"
host = subprocess.Popen([args.host_bin], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log, "w", encoding="utf-8"),
                        env=env, text=True, encoding="utf-8", bufsize=1)
_id = 0
failed = 0


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
    reply = rpc("tools/call", {"name": name, "arguments": kw})
    if "error" in reply:
        return {"rpc_error": reply["error"]}
    out = json.loads(reply["result"]["content"][0]["text"])
    out["_isError"] = reply["result"]["isError"]
    return out


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {str(extra)[:700]}"), flush=True)
    failed += 0 if cond else 1


def node_types():
    tree = call("get_scene_tree")
    return [(n.get("def"), n.get("type")) for n in tree.get("nodes", [])], tree


try:
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "proj", "version": "0"}})
    host.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    host.stdin.flush()
    for _ in range(100):  # project manager needs a moment
        if "project_create" in [t["name"] for t in rpc("tools/list")["result"]["tools"]]:
            break
        time.sleep(0.1)

    d = call("project_create", project_name="arena", description="real webots check")
    check("project_create", d.get("status") == "success", d)
    world = d["world_path"]
    print("     world:", world)
    d = call("world_add_robot", project_name="arena", robot=args.robot, translation=[0.5, 0, 0.1], fields='controller "<none>"')
    check(f"world_add_robot {args.robot}", d.get("status") == "success", d)
    if d.get("status") == "success":
        print("     proto:", d["proto_url"], " def:", d["def_name"])
    d = call("project_check", project_name="arena")
    check("project_check passes", d.get("integrity") == "passed", d)
    print("     world file:\n" + "\n".join("       " + l for l in Path(world).read_text(encoding="utf-8").splitlines()[:40]))

    d = call("sim_start", world=world, connect_timeout_s=120)
    check("sim_start on the generated world (supervisor bound via link launcher)", d.get("status") == "success" and d["simulation"]["state"] == "paused", d)
    if d.get("status") == "success":
        nodes, tree = node_types()
        types = {t for _, t in nodes}
        defs = {x for x, _ in nodes}
        check("scene contains floor, supervisor and the robot", {"FLOOR", "MCP_SUPERVISOR"} <= defs and any(t and t.startswith("Pioneer") for t in types), nodes)
        check("robot has a DEF", "PIONEER3DX" in defs, defs)
        d = call("sim_step", count=20)
        check("sim_step 20 with a robot in the world", d.get("status") == "success" and d["simulation"]["steps_done"] == 20, d)
        d = call("get_node_state", def_name="PIONEER3DX")
        check("get_node_state on the embedded robot", d.get("status") == "success", d)
        if d.get("status") == "success":
            print("     robot position:", d["state"]["position"])

        before = call("sim_status")["supervisor"]["info"]["session"]
        d = call("sim_reset", kind="reload")
        check("sim_reset(reload): supervisor reconnects", d.get("status") == "success" and d["supervisor"]["session"] > before, d)
        if d.get("status") == "success":
            check("tools work after reload", call("get_simulation_info").get("status") == "success")
            nodes, _ = node_types()
            check("scene intact after reload", "PIONEER3DX" in {x for x, _ in nodes}, nodes)
            d = call("sim_status")
            print("     after reload:", json.dumps(d["simulation"]))

        d = call("sim_reset", kind="restart")
        check("sim_reset(restart)", d.get("status") == "success", d)
    d = call("sim_stop")
    check("sim_stop graceful", d.get("graceful") is True, d)
finally:
    try:
        host.stdin.close()
        rc = host.wait(timeout=30)
    except Exception:
        host.kill()
        rc = -1
    check("host exits cleanly", rc == 0, rc)
    print(f"\nhost log: {log}\nFAILED: {failed}")
    sys.exit(1 if failed else 0)
