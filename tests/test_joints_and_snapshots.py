#!/usr/bin/env python3
"""Unit test (no Webots needed): set_joint_position, save_world(reset_joints), snapshot persistence.

    python tests/test_joints_and_snapshots.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fake"))
sys.path.insert(1, str(ROOT / "supervisor"))
tmp = Path(tempfile.mkdtemp(prefix="webots-mcp-joints-"))
os.environ.update(FAKE_WEBOTS_STATE_DIR=str(tmp), WEBOTS_MCP_SAVE_DIR=str(tmp))

from webots_mcp import runtime, scene, snapshots  # noqa: E402
from webots_mcp.tools import modify, output, snapshot_tools  # noqa: E402

failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


class Field:
    def __init__(self, value=None):
        self.value = value

    def getSFFloat(self):
        return self.value

    def setSFFloat(self, v):
        self.value = v

    def getSFNode(self):
        return self.value

    def importSFNodeFromString(self, text):
        self.value = Node("HingeJointParameters", {"position": Field(float(text.split("position")[1].split("}")[0]))})


class Node:
    def __init__(self, type_name, fields=None):
        self.type_name, self.fields = type_name, fields or {}

    def getTypeName(self):
        return self.type_name

    def getField(self, name):
        return self.fields.get(name)


def hinge(position):
    params = Node("HingeJointParameters", {"position": Field(position)}) if position is not None else None
    return Node("HingeJoint", {"jointParameters": Field(params)})


j1, j2 = hinge(0.7), hinge(None)
j3 = Node("HingeJoint", {"jointParameters": Field(Node("HingeJointParameters", {"position": Field(0.0)}))})
runtime_nodes = {"J1": j1, "J2": j2}
scene.get_node = lambda ref: runtime_nodes[ref]
modify.get_node = scene.get_node
scene.iter_all_nodes = lambda: iter([j1, j2, j3, Node("Solid")])
output.iter_joints = scene.iter_joints

stop = threading.Event()
threading.Thread(target=lambda: [runtime.process_commands() or time.sleep(0.005) for _ in iter(stop.is_set, True)], daemon=True).start()


def run(coro):
    return json.loads(asyncio.run(coro))


# ---- set_joint_position ----
r = run(modify.set_joint_position("J1", 0.25))
check("set_joint_position writes position and reports previous", r["status"] == "success" and r["previous_position"] == 0.7
      and scene.get_joint_position(j1) == 0.25, r)
r = run(modify.set_joint_position("J2", 1.5))
check("creates jointParameters when missing", r["status"] == "success" and scene.get_joint_position(j2) == 1.5, r)
check("non-finite position rejected", run(modify.set_joint_position("J1", float("nan")))["status"] == "error")
check("invalid axis rejected", run(modify.set_joint_position("J1", 0.0, axis=3))["status"] == "error")
check("axis 2 on a HingeJoint rejected", run(modify.set_joint_position("J1", 0.0, axis=2))["status"] == "error")

# ---- save_world(reset_joints) ----
j1.fields["jointParameters"].value.fields["position"].value = 0.7
j2.fields["jointParameters"].value.fields["position"].value = 1.5


def world_save(path=None):
    """Webots writes the CURRENT joint positions into the file."""
    target = Path(path) if path else tmp / "current.wbt"
    body = "".join(f"HingeJointParameters {{\n  position {n.fields['jointParameters'].value.fields['position'].value}\n}}\n"
                   for n in (j1, j2, j3))
    target.write_text("#VRML_SIM R2025b utf8\n" + body, encoding="utf-8")
    return True


runtime.supervisor.worldSave = world_save
runtime.supervisor.getWorldPath = lambda: str(tmp / "current.wbt")

r = run(output.save_world("plain.wbt"))
check("plain save keeps the bent pose (current behaviour)", output._nonzero_saved_joints(tmp / "plain.wbt") == 2, r)

r = run(output.save_world("clean.wbt", reset_joints=True))
check("reset_joints: saved file has no non-zero joints", r["status"] == "success" and r["saved_nonzero_joints"] == 0 and r["joints_reset"] == 2, r)
check("reset_joints: live joint angles restored after saving",
      scene.get_joint_position(j1) == 0.7 and scene.get_joint_position(j2) == 1.5)

r = run(output.save_world(reset_joints=True))
check("reset_joints works for the current world file too", r["status"] == "success" and r["saved_nonzero_joints"] == 0, r)
check("bad filename rejected", run(output.save_world("../x.wbt"))["status"] == "error")

# ---- snapshots persist across a "restart" ----
runtime_nodes_snap = {"BALL": Node("Solid", {"translation": type("F", (), {"getSFVec3f": lambda s: [1, 2, 3]})(),
                                             "rotation": type("F", (), {"getSFRotation": lambda s: [0, 0, 1, 0]})()})}
snapshot_tools.get_node = lambda ref: runtime_nodes_snap[ref]
r = run(snapshot_tools.save_snapshot("base", ["BALL"]))
check("save_snapshot persists to disk", r["status"] == "success" and Path(r["persisted_to"]).is_file(), r)
snapshots.snapshots.clear()          # what a world reload / sim_restart does to the process
check("list_snapshots empty in memory is repopulated from disk", "base" in run(snapshot_tools.list_snapshots())["snapshots"])
snapshots.snapshots.clear()
check("load_all_snapshots at startup", snapshots.load_all_snapshots() == ["base"] and snapshots.snapshots["base"]["BALL"]["translation"] == [1, 2, 3])
r = run(snapshot_tools.save_snapshot("mem_only", ["BALL"], persist=False))
check("persist=false stays in memory", "persisted_to" not in r and not (snapshots.SNAPSHOT_DIR / "mem_only.json").exists(), r)
(snapshots.SNAPSHOT_DIR / "broken.json").write_text("{not json")
snapshots.snapshots.clear()
check("corrupt snapshot file is ignored", "broken" not in snapshots.load_all_snapshots())

stop.set()
print("FAILED:", failed)
sys.exit(1 if failed else 0)
