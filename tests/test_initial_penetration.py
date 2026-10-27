#!/usr/bin/env python3
"""Unit test (no Webots needed): initial interpenetration detection in check_physics_health.

    python tests/test_initial_penetration.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fake"))
sys.path.insert(1, str(ROOT / "supervisor"))
os.environ.setdefault("FAKE_WEBOTS_STATE_DIR", tempfile.mkdtemp())

from webots_mcp import physics  # noqa: E402

failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


S, B = "sphere", "box"
pen = physics.penetration_depth
check("sphere/sphere overlap", abs(pen(((S, 1.0), [0, 0, 0]), ((S, 1.0), [1.5, 0, 0])) - 0.5) < 1e-9)
check("sphere/sphere apart", pen(((S, 1.0), [0, 0, 0]), ((S, 1.0), [3, 0, 0])) < 0)
check("sphere embedded 1 cm in a post", abs(pen(((S, 0.1), [0.19, 0, 0]), ((B, [0.2, 0.2, 1.0]), [0, 0, 0])) - 0.01) < 1e-9)
check("sphere resting on a box is not an overlap", abs(pen(((S, 0.1), [0, 0, 0.6]), ((B, [1, 1, 1.0]), [0, 0, 0]))) < 1e-9)
check("argument order does not matter", abs(pen(((B, [0.2, 0.2, 1.0]), [0, 0, 0]), ((S, 0.1), [0.19, 0, 0])) - 0.01) < 1e-9)
check("sphere centre inside box", pen(((S, 0.1), [0, 0, 0]), ((B, [1, 1, 1.0]), [0, 0, 0])) > 0.5)
check("box/box overlap uses smallest axis", abs(pen(((B, [1, 1, 1]), [0, 0, 0]), ((B, [1, 1, 1]), [0.9, 0.2, 0])) - 0.1) < 1e-9)


class F:
    def __init__(self, v):
        self.v = v

    def getSFNode(self): return self.v
    def getSFFloat(self): return self.v
    def getSFVec3f(self): return self.v
    def getSFRotation(self): return [0, 0, 1, 0]


class N:
    def __init__(self, name, nid, geom, pos, dynamic):
        self.name, self.nid, self.geom, self.pos, self.dynamic = name, nid, geom, pos, dynamic

    def getDef(self): return self.name
    def getId(self): return self.nid
    def getTypeName(self): return "Solid"
    def getField(self, n):
        return {"boundingObject": F(self.geom), "rotation": F(None)}.get(n)


class G(N):
    def __init__(self, kind, **fields):
        self.kind, self.fields = kind, fields

    def getTypeName(self): return self.kind
    def getField(self, n): return F(self.fields[n])


ball = N("BALL", 1, G("Sphere", radius=0.1), [0.19, 0, 0.5], True)
post = N("POST", 2, G("Box", size=[0.2, 0.2, 1.0]), [0, 0, 0.5], False)
floor = N("FLOOR", 3, G("Box", size=[5, 5, 0.2]), [0, 0, -0.1], False)
nodes = [ball, post, floor]
physics.iter_all_nodes = lambda: iter(nodes)
physics.physics_role = lambda n: "dynamic" if n.dynamic else "static_collider"
physics.safe_node_state = lambda n, include_orientation=False: {"position": n.pos, "velocity": [0] * 6}

found, skipped = physics.find_initial_penetrations()
check("ball embedded in post is reported", [(e["a"], e["b"]) for e in found] == [("BALL", "POST")] and abs(found[0]["depth_m"] - 0.01) < 1e-6, found)
check("static/static pairs (post vs floor) are ignored", all("FLOOR" not in (e["a"], e["b"]) for e in found))

report = physics.physics_health()
check("check_physics_health surfaces it as an issue and hint",
      report["initial_penetrations"] and any("overlaps POST" in f for i in report["issues"] for f in i["flags"]) and report["hints"], report)

ball.pos = [0.5, 0, 0.5]
check("no report once separated", physics.physics_health()["initial_penetrations"] == [])

print("FAILED:", failed)
sys.exit(1 if failed else 0)
