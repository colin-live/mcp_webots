#!/usr/bin/env python3
"""Unit test (no Webots needed): capture_screenshot staleness, size and parameters.

    python tests/test_screenshot.py
"""
import asyncio
import json
import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from concurrent.futures import Future
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fake"))
sys.path.insert(1, str(ROOT / "supervisor"))
tmp = tempfile.mkdtemp(prefix="webots-mcp-shot-")
os.environ.update(FAKE_WEBOTS_STATE_DIR=tmp, WEBOTS_MCP_SAVE_DIR=tmp)

from webots_mcp import imaging, runtime  # noqa: E402
from webots_mcp.tools import output  # noqa: E402

failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


def make_png(w, h, color, level=0):
    """RGB PNG; level=0 -> stored deflate blocks, like Webots' near-uncompressed output."""
    row = b"\x00" + bytes(color) * w
    raw = row * h

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return imaging.PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b"")


class Viewpoint:
    def __init__(self):
        self.pos = [0.0, 0.0, 1.0]

    def getField(self, name):
        owner = self

        class F:
            def getSFVec3f(self):
                return owner.pos

            def getSFRotation(self):
                return [0, 0, 1, 0]
        return F()


vp = Viewpoint()
state = {"time": 0.0, "color": (10, 20, 30), "state": "paused", "steps": 0}


class Clock:
    def snapshot(self):
        return {"state": state["state"]}

    def submit(self, op, params):
        fut = Future()
        state["steps"] += params["count"]
        state["time"] += 0.016 * params["count"]
        state["color"] = (state["color"][0] + 1, 20, 30)   # a real step redraws
        fut.set_result({"state": "paused"})
        return fut


def export_image(path, quality):
    # While paused the view does not redraw: the file keeps the same pixels.
    Path(path).write_bytes(make_png(200, 100, state["color"]))


runtime.supervisor.exportImage = export_image
runtime.supervisor.getTime = lambda: state["time"]
runtime.clock = Clock()
output.find_root_node_by_type = lambda t: vp

stop = threading.Event()
threading.Thread(target=lambda: [runtime.process_commands() or time.sleep(0.005) for _ in iter(stop.is_set, True)], daemon=True).start()


def shot(**kw):
    return json.loads(asyncio.run(output.capture_screenshot(**kw)))


# --- PNG size: Webots-style stored PNG shrinks, pixels unchanged ---
def idat_pixels(path):
    data, pos, idat = Path(path).read_bytes(), len(imaging.PNG_SIG), []
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if kind == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + n])
        pos += 12 + n
    return zlib.decompress(b"".join(idat))


p = Path(tmp) / "a.png"
p.write_bytes(make_png(1955, 1344, (10, 20, 30), level=0))
before, pixels = p.stat().st_size, idat_pixels(p)
after = imaging.recompress_png(p, 9)
check("stored PNG is >1 MB before", before > 1_000_000, before)
check("recompress shrinks it a lot", after is not None and after < before / 20, (before, after))
check("recompressed file has identical pixels", idat_pixels(p) == pixels)
txt = Path(tmp) / "x.txt"
txt.write_text("hi")
check("recompress leaves non-PNG files alone", imaging.recompress_png(txt, 9) is None and txt.read_text() == "hi")

# --- stale detection ---
a = shot(filename="s1.png")
check("first screenshot ok, not stale", a["status"] == "success" and a["stale"] is False, a)
check("png on disk is small", a["bytes"] < 5000, a)

vp.pos = [1.0, 0.0, 1.0]            # set_viewpoint while paused, no redraw
b = shot(filename="s2.png")
check("moved viewpoint + identical bytes -> stale=true with warning", b["stale"] is True and b.get("warnings"), b)

c = shot(filename="s3.png", render_steps=1)
check("render_steps=1 while paused advances and refreshes", c["stale"] is False and state["steps"] == 1, (c, state))

d = shot(filename="s4.png")
check("same scene twice is not flagged", d["stale"] is False, d)

state["state"] = "running"
e = shot(filename="s5.png", render_steps=3)
check("render_steps ignored while running (warning)", state["steps"] == 1 and any("render_steps" in w for w in e.get("warnings", [])), e)

import inspect  # noqa: E402
check("default filename is jpg", inspect.signature(output.capture_screenshot).parameters["filename"].default == "screenshot.jpg")
bad = shot(filename="x.png", scale=2)
check("invalid scale rejected", bad["status"] == "error", bad)
bad = shot(filename="x.png", compress_level=11)
check("invalid compress_level rejected", bad["status"] == "error", bad)
bad = shot(filename="../x.png")
check("path traversal rejected", bad["status"] == "error", bad)

stop.set()
print("FAILED:", failed)
sys.exit(1 if failed else 0)
