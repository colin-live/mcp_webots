#!/usr/bin/env python3
"""Unit test (no Webots needed): 'id:<n>' node references and get_joint_info.

    python tests/test_node_id_and_joint_info.py
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
sys.path.insert(0, str(ROOT / "tests" / "fake"))   # stub `controller`
sys.path.insert(1, str(ROOT / "supervisor"))
os.environ.setdefault("FAKE_WEBOTS_STATE_DIR", tempfile.mkdtemp())

from webots_mcp import runtime, scene  # noqa: E402
from webots_mcp.tools import modify, query  # noqa: E402

failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


# ---------- minimal fakes of the Webots node / field API ----------

class Field:
    def __init__(self, name, type_name, value=None):
        self.name, self.type_name, self.value = name, type_name, value

    def getName(self):
        return self.name

    def getTypeName(self):
        return self.type_name

    def getSFFloat(self):
        return self.value

    getSFString = getSFVec3f = getSFNode = getSFFloat

    def getCount(self):
        return len(self.value)

    def getMFNode(self, i):
        return self.value[i]


class Node:
    def __init__(self, node_id, type_name, def_name=None, **fields):
        self.id, self.type_name, self.def_name, self.fields = node_id, type_name, def_name, fields
        self.writes = []

    def getId(self):
        return self.id

    def getDef(self):
        return self.def_name or ""

    def getTypeName(self):
        return self.type_name

    def getField(self, name):
        return self.fields.get(name)

    def getNumberOfFields(self):
        return len(self.fields)

    def getFieldByIndex(self, i):
        return list(self.fields.values())[i]


def sf(name, type_name, value):
    return Field(name, type_name, value)


def mf(name, nodes):
    return Field(name, "MFNode", list(nodes))


def node_field(name, node):
    return Field(name, "SFNode", node)


def hinge_params(node_id, position=0.5, axis=(0.0, 0.0, 1.0), min_stop=-1.0, max_stop=1.0):
    return Node(node_id, "HingeJointParameters",
                position=sf("position", "SFFloat", position), axis=sf("axis", "SFVec3f", list(axis)),
                anchor=sf("anchor", "SFVec3f", [0.0, 0.0, 0.0]), minStop=sf("minStop", "SFFloat", min_stop),
                maxStop=sf("maxStop", "SFFloat", max_stop), dampingConstant=sf("dampingConstant", "SFFloat", 0.02))


def rotational_motor(node_id, name="arm_motor", max_torque=5.0):
    return Node(node_id, "RotationalMotor", name=sf("name", "SFString", name),
                maxTorque=sf("maxTorque", "SFFloat", max_torque), maxVelocity=sf("maxVelocity", "SFFloat", 10.0),
                minPosition=sf("minPosition", "SFFloat", 0.0), maxPosition=sf("maxPosition", "SFFloat", 0.0))


def position_sensor(node_id, name="arm_sensor"):
    return Node(node_id, "PositionSensor", name=sf("name", "SFString", name))


def joint(node_id, type_name, params, devices, end=None, def_name=None, **extra):
    return Node(node_id, type_name, def_name,
                jointParameters=node_field("jointParameters", params), device=mf("device", devices),
                endPoint=node_field("endPoint", end), **extra)


# ---------- the fake world ----------

arm_link = Node(21, "Solid", "ARM_LINK")
motor, sensor = rotational_motor(19), position_sensor(20)
good = joint(17, "HingeJoint", hinge_params(18), [motor, sensor], arm_link, "ARM_JOINT")

slider = joint(30, "SliderJoint", None, [Node(31, "LinearMotor", name=sf("name", "SFString", "rail_motor"),
                                              maxForce=sf("maxForce", "SFFloat", 100.0))], Node(32, "Solid"), "RAIL")
messy = joint(40, "HingeJoint", hinge_params(41),
              [rotational_motor(42, "m"), rotational_motor(43, "m"), position_sensor(44, ""), position_sensor(45, "s")],
              None, "MESSY")
hinge2 = Node(50, "Hinge2Joint", "H2",
              jointParameters=node_field("jointParameters", hinge_params(51)),
              jointParameters2=node_field("jointParameters2",
                                          Node(52, "JointParameters", position=sf("position", "SFFloat", 0.1),
                                               axis=sf("axis", "SFVec3f", [1.0, 0.0, 0.0]))),
              device=mf("device", [rotational_motor(53, "yaw")]),
              device2=mf("device2", [rotational_motor(54, "pitch")]),
              endPoint=node_field("endPoint", Node(55, "Solid")))
plain_solid = Node(60, "Solid", "BOX")
world_info = Node(1, "WorldInfo", title=sf("title", "SFString", "t"))

by_id = {n.id: n for n in (good, motor, sensor, arm_link, slider, messy, hinge2, plain_solid, world_info,
                           good.fields["jointParameters"].value)}
by_def = {n.def_name: n for n in (good, arm_link, slider, messy, hinge2, plain_solid)}
runtime.supervisor.getFromId = lambda i: by_id.get(i)
runtime.supervisor.getFromDef = lambda name: by_def.get(name)
scene.find_root_node_by_type = lambda t: world_info if t == "WorldInfo" else None

stop = threading.Event()
threading.Thread(target=lambda: [runtime.process_commands() or time.sleep(0.005) for _ in iter(stop.is_set, True)],
                 daemon=True).start()


def run(coro):
    return json.loads(asyncio.run(coro))


# ---------- parse_node_id_ref ----------

check("'id:19' parses", scene.parse_node_id_ref("id:19") == 19)
check("spaces after the colon are tolerated", scene.parse_node_id_ref("id: 19 ") == 19)
for plain in ("ARM", "ARM.TCP", "type:WorldInfo", "", None, 19):
    check(f"not an id reference: {plain!r}", scene.parse_node_id_ref(plain) is None)
for bad in ("id:", "id:abc", "id:0", "id:-3", "id:+5", "id:1.5", "id:19x", "id:\u0663", f"id:{2**31}"):
    try:
        scene.parse_node_id_ref(bad)
        check(f"invalid id reference rejected: {bad!r}", False, "no exception")
    except ValueError as exc:
        check(f"invalid id reference rejected: {bad!r}", "id:<positive integer>" in str(exc), str(exc))

# ---------- get_node_by_id / get_readable_node ----------

check("get_node_by_id returns the node", scene.get_node_by_id(19) is motor)
try:
    scene.get_node_by_id(999)
    check("unknown id -> error", False, "no exception")
except ValueError as exc:
    check("unknown id -> error that explains ids change on reload", "999" in str(exc) and "reload" in str(exc), str(exc))

runtime.supervisor.getFromId = lambda i: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    scene.get_node_by_id(19)
    check("getFromId raising -> ValueError", False, "no exception")
except ValueError:
    check("getFromId raising -> ValueError", True)
runtime.supervisor.getFromId = lambda i: by_id.get(i)

check("readable: id reference", scene.get_readable_node("id:20") is sensor)
check("readable: DEF reference", scene.get_readable_node("ARM_JOINT") is good)
check("readable: type reference", scene.get_readable_node("type:WorldInfo") is world_info)

# Modifying paths stay DEF-only.
try:
    scene.get_node("id:19")
    check("get_node (used by modifying tools) rejects id references", False, "no exception")
except ValueError:
    check("get_node (used by modifying tools) rejects id references", True)
r = run(modify.set_node_field("id:19", "maxTorque", 1.0))
check("set_node_field does not accept an id reference", r.get("status") == "error", r)

# ---------- read-only tools take ids ----------

r = run(query.get_node_details("id:19"))
check("get_node_details by id: a DEF-less motor", r["status"] == "success" and r["node"]["type"] == "RotationalMotor"
      and r["fields"]["name"]["value"] == "arm_motor" and r["fields"]["maxTorque"]["value"] == 5.0, r)
check("get_node_details still takes a DEF", run(query.get_node_details("ARM_JOINT"))["node"]["id"] == 17)
r = run(query.get_node_details("id:abc"))
check("get_node_details: malformed id -> error status", r["status"] == "error" and "id:<positive integer>" in json.dumps(r), r)
r = run(query.get_node_details("id:424242"))
check("get_node_details: unknown id -> error status", r["status"] == "error" and "424242" in json.dumps(r), r)
r = run(query.get_node_state("id:21"))
check("get_node_state by id", r["status"] == "success" and r["state"]["id"] == 21 and r["state"]["def"] == "ARM_LINK", r)

# ---------- get_joint_info ----------

r = run(query.get_joint_info("ARM_JOINT"))
check("hinge: success and type", r["status"] == "success" and r["joint_type"] == "HingeJoint" and r["joint"]["def"] == "ARM_JOINT", r)
axis1 = r["axes"][0]
check("hinge: parameters of axis 1", axis1["axis_index"] == 1 and axis1["values"]["axis"] == [0.0, 0.0, 1.0]
      and axis1["values"]["minStop"] == -1.0 and axis1["values"]["maxStop"] == 1.0
      and axis1["values"]["dampingConstant"] == 0.02 and axis1["node"]["id"] == 18, axis1)
check("hinge: device names and types", [(d["type"], d["name"]) for d in r["devices"]]
      == [("RotationalMotor", "arm_motor"), ("PositionSensor", "arm_sensor")], r["devices"])
check("hinge: motor limits", r["devices"][0]["maxTorque"] == 5.0 and r["devices"][0]["maxVelocity"] == 10.0, r["devices"][0])
check("hinge: summary", r["summary"] == {"motor_names": ["arm_motor"], "sensor_names": ["arm_sensor"]}, r["summary"])
check("hinge: end point and no warnings", r["end_point"]["def"] == "ARM_LINK" and r["warnings"] == [], r)
check("hinge: device ids are reported so they can be queried with id:<n>", [d["id"] for d in r["devices"]] == [19, 20], r["devices"])

r = run(query.get_joint_info("id:17"))
check("get_joint_info accepts an id reference", r["status"] == "success" and r["joint"]["id"] == 17, r)

r = run(query.get_joint_info("RAIL"))
check("slider: null jointParameters is reported, not guessed", r["axes"][0]["values"] is None
      and any("jointParameters is null" in w for w in r["warnings"]), r)
check("slider: LinearMotor limits use maxForce", r["devices"][0]["type"] == "LinearMotor" and r["devices"][0]["maxForce"] == 100.0
      and "maxTorque" not in r["devices"][0], r["devices"])
check("slider: no sensor -> empty sensor list", r["summary"]["sensor_names"] == [] and r["summary"]["motor_names"] == ["rail_motor"], r["summary"])

r = run(query.get_joint_info("MESSY"))
warn = " | ".join(r["warnings"])
check("messy: several motors on one axis", "2 motors" in warn and "takes the first one" in warn, warn)
check("messy: several sensors on one axis", "2 sensors" in warn, warn)
check("messy: empty device name", "empty name" in warn, warn)
check("messy: duplicate device name", "'m' is used by 2 devices" in warn, warn)
check("messy: null endPoint", "endPoint is null" in warn and r["end_point"] is None, warn)

r = run(query.get_joint_info("H2"))
check("hinge2: both axes", [a["axis_index"] for a in r["axes"]] == [1, 2] and r["axes"][1]["values"]["axis"] == [1.0, 0.0, 0.0], r["axes"])
check("hinge2: device2 belongs to axis 2", [(d["name"], d["axis_index"]) for d in r["devices"]] == [("yaw", 1), ("pitch", 2)], r["devices"])
check("hinge2: parameters without an anchor field omit the key", "anchor" not in r["axes"][1]["values"], r["axes"][1])

r = run(query.get_joint_info("BOX"))
check("non-joint node -> error status", r["status"] == "error" and "not a HingeJoint" in json.dumps(r), r)
check("unknown DEF -> error status", run(query.get_joint_info("NOPE"))["status"] == "error")

stop.set()
print("FAILED:", failed)
sys.exit(1 if failed else 0)
