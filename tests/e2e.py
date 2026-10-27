#!/usr/bin/env python3
"""End-to-end test: MCP client -> Rust host -> (fake) Webots -> REAL supervisor code.

    python tests/e2e.py [--host-bin PATH] [--release]

No Webots installation is needed: tests/fake/ provides a stand-in `controller`
module and a stand-in webots executable that run the real mcp_server.py.
"""
import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parent.parent
SUPERVISOR_DIR = MCP_DIR / "supervisor"
FAKE = MCP_DIR / "tests" / "fake"
EXE = ".exe" if os.name == "nt" else ""

ap = argparse.ArgumentParser()
ap.add_argument("--host-bin")
ap.add_argument("--release", action="store_true")
args = ap.parse_args()
HOST = Path(args.host_bin) if args.host_bin else MCP_DIR / "host" / "target" / ("release" if args.release else "debug") / f"webots-mcp-host{EXE}"
if not HOST.exists():
    sys.exit(f"host binary not found: {HOST}  (run `cargo build` in {MCP_DIR / 'host'})")

tmp = Path(tempfile.mkdtemp(prefix="webots-mcp-e2e-"))
state, projects, fakehome, flags = tmp / "state", tmp / "projects", tmp / "webots_home", tmp / "flags"
for d in (state, projects, flags, fakehome / "resources", fakehome / "projects/robots/adept/pioneer3/protos",
          fakehome / "projects/objects/backgrounds/protos"):
    d.mkdir(parents=True, exist_ok=True)
(fakehome / "resources/version.txt").write_text("R2025b\n")
(fakehome / "projects/robots/adept/pioneer3/protos/Pioneer3dx.proto").write_text("PROTO Pioneer3dx [ ] { Robot { } }\n")
for n in ("TexturedBackground", "TexturedBackgroundLight"):
    (fakehome / f"projects/objects/backgrounds/protos/{n}.proto").write_text("")

if os.name == "nt":
    launcher = tmp / "fake_webots.cmd"
    launcher.write_text(f'@echo off\r\n"{sys.executable}" "{FAKE / "fake_webots.py"}" %*\r\n')
else:
    launcher = tmp / "fake_webots.sh"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE / "fake_webots.py"}" "$@"\n')
    launcher.chmod(0o755)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]

env = dict(os.environ,
           WEBOTS_BIN=str(launcher), WEBOTS_MCP_IPC=f"127.0.0.1:{port}", WEBOTS_MCP_STATE_DIR=str(state),
           WEBOTS_MCP_SUPERVISOR_DIR=str(SUPERVISOR_DIR), WEBOTS_MCP_PYTHON=sys.executable,
           WEBOTS_MCP_PROJECTS_DIR=str(projects), WEBOTS_HOME=str(fakehome),
           FAKE_WEBOTS_STATE_DIR=str(flags), MCP_SERVER_DIR=str(SUPERVISOR_DIR), WEBOTS_MCP_SAVE_DIR=str(tmp / "save"),
           FAKE_WEBOTS_MAX_SECONDS="240", WEBOTS_MCP_LOG="info")
log_path = tmp / "host.log"
host = subprocess.Popen([str(HOST)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log_path, "w", encoding="utf-8"),
                        env=env, text=True, encoding="utf-8", bufsize=1)

_id = 0
notes = []


def rpc(method, params=None):
    global _id
    _id += 1
    host.stdin.write(json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}) + "\n")
    host.stdin.flush()
    while True:
        line = host.stdout.readline()
        if not line:
            raise SystemExit("host closed stdout; log: " + str(log_path))
        msg = json.loads(line)
        if msg.get("id") == _id:
            return msg
        notes.append(msg)


def call(name, **kw):
    reply = rpc("tools/call", {"name": name, "arguments": kw})
    if "error" in reply:
        return {"rpc_error": reply["error"]}
    data = json.loads(reply["result"]["content"][0]["text"])
    data["_isError"] = reply["result"]["isError"]
    return data


failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


def tool_names():
    return [t["name"] for t in rpc("tools/list")["result"]["tools"]]


try:
    r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}})
    check("initialize", r["result"]["capabilities"]["tools"]["listChanged"])
    host.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    host.stdin.flush()

    names = []
    for _ in range(100):
        names = tool_names()
        if "project_create" in names:
            break
        time.sleep(0.1)
    check("native + project_manager tools listed before Webots starts", {"sim_start", "project_create", "world_add_robot", "supervisor_bind"} <= set(names), names)
    check("supervisor tools not listed yet (no cache)", "get_workflow_guide" not in names)
    d = call("get_workflow_guide")
    check("supervisor tool before sim_start -> unknown tool", "rpc_error" in d, d)

    d = call("project_create", project_name="arena", description="e2e")
    check("project_create", d["status"] == "success", d)
    world = d["world_path"]
    d = call("world_add_robot", project_name="arena", robot="Pioneer3dx", translation=[1, 0, 0.1])
    check("world_add_robot", d["status"] == "success", d)
    check("project_check passes", call("project_check", project_name="arena")["integrity"] == "passed")

    d = call("sim_start", world=world)
    check("sim_start -> paused", d["status"] == "success" and d["simulation"]["state"] == "paused", d)
    names = tool_names()
    check("real supervisor tools exposed", {"get_workflow_guide", "get_scene_tree", "create_primitive", "run_simulation", "save_world"} <= set(names), names)
    check("host-owned set_simulation_speed hidden", "set_simulation_speed" not in names)
    check("tool list changed notification sent", any(n.get("method") == "notifications/tools/list_changed" for n in notes))
    print(f"     ({len(names)} tools total)")

    d = call("get_simulation_info")
    check("real tool runs on the main thread while PAUSED", d["status"] == "success" and not d["_isError"], d)
    d = call("get_workflow_guide")
    check("get_workflow_guide", d["status"] == "success" and "set_simulation_speed" not in json.dumps(d, ensure_ascii=False), str(d)[:300])
    d = call("create_primitive", shape="nonsense")
    check("tool-level validation error -> isError", d["_isError"] or d.get("status") == "error", d)

    def clock():
        return call("sim_status")["simulation"]

    check("clock frozen while paused", clock()["sim_time"] == clock()["sim_time"] == 0.0)
    d = call("sim_step", count=5)
    check("sim_step 5 is exact", d["simulation"]["steps_done"] == 5 and abs(d["simulation"]["sim_time"] - 0.08) < 1e-9 and d["simulation"]["state"] == "paused", d)
    d = call("sim_resume", mode="fast")
    check("sim_resume fast", d["simulation"]["state"] == "running", d)
    time.sleep(0.4)
    check("time advances while running", clock()["sim_time"] > 0.5, clock())
    check("sim_step rejected while running", call("sim_step", count=1)["_isError"])
    check("sim_pause", call("sim_pause")["simulation"]["state"] == "paused")
    t = clock()["sim_time"]
    time.sleep(0.3)
    check("time frozen after pause", clock()["sim_time"] == t)
    check("tools still work after pause", call("get_simulation_info")["status"] == "success")

    d = call("sim_reset", kind="simulation")
    check("reset simulation -> time 0, paused", d["simulation"]["sim_time"] == 0.0 and d["simulation"]["state"] == "paused", d)
    check("reset physics", call("sim_reset", kind="physics")["status"] == "success")
    check("tools work after reset", call("get_simulation_info")["status"] == "success")

    before = call("sim_status")["supervisor"]["info"]["session"]
    d = call("sim_reset", kind="reload")
    check("reload -> supervisor reconnects", d["status"] == "success" and d["supervisor"]["session"] > before, d)
    check("tools work after reload", call("get_simulation_info")["status"] == "success")

    check("double sim_start rejected", call("sim_start")["error_type"] == "AlreadyRunning")
    d = call("sim_reset", kind="restart")
    check("restart", d["status"] == "success" and d.get("started") is True, d)
    d = call("sim_stop")
    check("graceful stop", d["stopped"] and d["graceful"] and not d["killed"], d)
    check("after stop: SupervisorNotConnected", call("get_simulation_info")["error_type"] == "SupervisorNotConnected")
    check("cached tool list survives Webots exit", "get_scene_tree" in tool_names())

    d = call("sim_start", mode="real_time")
    check("start directly in real_time", d["simulation"]["state"] == "running" and d["simulation"]["mode"] == "real_time", d)
    call("sim_stop")

    # project manager crash -> automatic restart
    pids = re.findall(r"project manager started \(pid Some\((\d+)\)\)", log_path.read_text(encoding="utf-8", errors="replace"))
    if pids:
        try:
            os.kill(int(pids[-1]), signal.SIGTERM)
        except OSError:
            pass
        ok = False
        for _ in range(100):
            if call("project_list").get("status") == "success":
                ok = True
                break
            time.sleep(0.2)
        check("project manager restarted after crash", ok)
    else:
        print("SKIP project manager crash test (pid not found in log)")
finally:
    try:
        host.stdin.close()
        rc = host.wait(timeout=30)
    except Exception:
        host.kill()
        rc = -1
    check("host exits cleanly when stdin closes", rc == 0, rc)
    print(f"\nhost log: {log_path}")
    print("FAILED:", failed)
    sys.exit(1 if failed else 0)
