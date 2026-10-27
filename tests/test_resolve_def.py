#!/usr/bin/env python3
"""Unit test (no Webots needed): DEF lookup inside PROTO instances.

    python tests/test_resolve_def.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fake"))   # stub `controller`
sys.path.insert(1, str(ROOT / "supervisor"))
os.environ.setdefault("FAKE_WEBOTS_STATE_DIR", tempfile.mkdtemp())

from webots_mcp import runtime, scene  # noqa: E402

failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


class Node:
    def __init__(self, inner=None):
        self.inner = inner or {}

    def getFromProtoDef(self, name):
        return self.inner.get(name)


tcp = Node()
arm = Node({"TCP": tcp})
world = {"ARM": arm}
runtime.supervisor.getFromDef = lambda name: world.get(name)

check("plain DEF resolves", scene.resolve_def("ARM") is arm)
check("DEF inside a PROTO resolves via dotted path", scene.resolve_def("ARM.TCP") is tcp)
check("plain lookup of an inner DEF still misses", scene.resolve_def("TCP") is None)
check("unknown inner DEF -> None", scene.resolve_def("ARM.NOPE") is None)
check("unknown parent -> None", scene.resolve_def("GONE.TCP") is None)

try:
    scene.get_node("TCP")
    check("get_node error", False, "no exception")
except ValueError as exc:
    msg = str(exc)
    check("not-found error explains the PROTO limitation", "PROTO" in msg and "ARM2DOF.TCP" in msg and "inline=true" in msg, msg)

check("get_node accepts dotted path", scene.get_node("ARM.TCP") is tcp)
for bad in ("ARM..TCP", ".TCP", "ARM.", "A B"):
    try:
        scene.resolve_def(bad)
        check(f"invalid ref rejected: {bad!r}", False, "no exception")
    except ValueError:
        check(f"invalid ref rejected: {bad!r}", True)

print("FAILED:", failed)
sys.exit(1 if failed else 0)
