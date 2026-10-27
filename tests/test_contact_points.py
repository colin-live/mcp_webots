#!/usr/bin/env python3
"""Unit test (no Webots needed): get_contact_points semantics.

    python tests/test_contact_points.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fake"))
sys.path.insert(1, str(ROOT / "supervisor"))
os.environ.setdefault("FAKE_WEBOTS_STATE_DIR", tempfile.mkdtemp())

from webots_mcp import runtime, snapshots  # noqa: E402
from webots_mcp.tools import query  # noqa: E402

failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


class Node:
    def __init__(self, node_id, def_name, type_name="Solid"):
        self.id, self.def_name, self.type_name = node_id, def_name, type_name
        self.enabled = []          # include_descendants flags passed to enable...
        self.points = []           # what getContactPoints returns once tracking is on

    def getId(self):
        return self.id

    def getDef(self):
        return self.def_name

    def getTypeName(self):
        return self.type_name

    def enableContactPointsTracking(self, period, descendants):
        self.enabled.append(descendants)

    def getContactPoints(self, descendants):
        return self.points if self.enabled else []


ball, link2, ground = Node(56, "BALL"), Node(7, "LINK2"), Node(5, "GROUND")
by_id = {n.id: n for n in (ball, link2, ground)}
runtime.supervisor.getFromId = lambda i: by_id.get(i)
query.get_node = lambda ref: {"BALL": ball, "LINK2": link2}[ref]
snapshots.contact_tracking.clear()

stop = threading.Event()
threading.Thread(target=lambda: [runtime.process_commands() or time.sleep(0.005) for _ in iter(stop.is_set, True)], daemon=True).start()


def contacts(name, **kw):
    return json.loads(asyncio.run(query.get_contact_points(name, **kw)))


P = SimpleNamespace
ball.points = [P(point=[0.0, 0.0, 0.4825], node_id=56), P(point=[0.1, 0.0, 0.0], node_id=56)]
link2.points = [P(point=[0.0, 0.0, 0.4825], node_id=7)]

# First call on BALL only starts tracking (this is why LINK2 looked "empty" in the field report)
a = contacts("BALL")
check("first call enables tracking and says so", a["tracking_started"] is True and "next" in a["note"], a)
check("first call returns the points the stub reports", a["contact_count"] == 2, a)

# node_id is the OWNER of the point (the queried node), not the other body
check("node_id is the queried solid, not the partner", all(c["node_id"] == 56 for c in a["contacts"]), a)
check("owner_def / owner_type resolved", a["contacts"][0]["owner_def"] == "BALL" and a["contacts"][0]["owner_type"] == "Solid", a)
check("no partner known yet (LINK2 not tracked)", all(c["touching"] == [] for c in a["contacts"]), a)

# LINK2 first call: tracking just started -> explicit note instead of a silent 0
link2.points = []
b = contacts("LINK2")
check("LINK2 first call: tracking_started + note", b["tracking_started"] is True and b["contact_count"] == 0 and b["note"], b)

# Next step the collision is recorded; now BALL's point lists LINK2 as touching
link2.points = [P(point=[0.0, 0.0, 0.4825], node_id=7)]
c = contacts("BALL")
touching = [t["def"] for pt in c["contacts"] for t in pt["touching"]]
check("tracked partner is listed in touching", touching == ["LINK2"], c)
check("tracking not restarted on repeat calls", c["tracking_started"] is False and ball.enabled == [False], (c, ball.enabled))

# include_descendants upgrade re-enables tracking (it was silently ignored before)
d = contacts("BALL", include_descendants=True)
check("switching to include_descendants re-enables tracking", d["tracking_started"] is True and ball.enabled == [False, True], (d, ball.enabled))

stop.set()
print("FAILED:", failed)
sys.exit(1 if failed else 0)
