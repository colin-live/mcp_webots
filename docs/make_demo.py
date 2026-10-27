#!/usr/bin/env python3
"""Record a real Webots session through the MCP host and build docs/media/demo.gif.

The script talks MCP/stdio to ``webots-mcp-host`` exactly like an agent would:
it creates a project, starts Webots, builds a small scene, steps the simulation
and captures every frame with the ``capture_screenshot`` tool.  No screen
recording and no fake rendering: the frames come from Webots' own 3D viewport.

Requirements
------------
* Webots installed (``--webots-home``)
* Pillow (``pip install pillow``) for GIF assembly
* a built host binary (``cargo build --release`` in ``host/``)

Usage
-----
    python docs/make_demo.py --webots-home "C:\\Program Files\\Webots"

    # keep the raw frames / skip the GIF
    python docs/make_demo.py --webots-home ... --keep-frames
"""
import argparse
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXE = ".exe" if os.name == "nt" else ""

# ---------------------------------------------------------------- scene ----
# ENU world: x = east, y = north, z = up.  The stack sits at the origin.
BOXES = [
    ("BOX_1", "crimson", (0.85, 0.10, 0.16), (0.00, 0.00, 0.13), (0.24, 0.24, 0.24), 0.00),
    ("BOX_2", "orange", (0.95, 0.42, 0.05), (0.015, 0.010, 0.38), (0.24, 0.24, 0.24), 0.08),
    ("BOX_3", "amber", (0.98, 0.72, 0.08), (-0.012, 0.014, 0.63), (0.24, 0.24, 0.24), -0.05),
    ("BOX_4", "emerald", (0.08, 0.68, 0.38), (0.014, -0.010, 0.88), (0.24, 0.24, 0.24), 0.06),
    ("BOX_5", "azure", (0.06, 0.50, 0.85), (-0.010, 0.012, 1.13), (0.24, 0.24, 0.24), -0.04),
    ("BOX_6", "violet", (0.42, 0.22, 0.85), (0.010, 0.000, 1.38), (0.24, 0.24, 0.24), 0.05),
]
BALL = ("BALL", "gold", (1.00, 0.82, 0.20), (0.18, 0.10, 2.45), (0.20,), 2.0)
STAGE = ("STAGE", "slate", (0.09, 0.11, 0.16), (0.0, 0.0, -0.04), (30.0, 30.0, 0.10), 0.0)
FRAMES = 60
STEPS_PER_FRAME = 2
CAPTION_H = 46


# ---------------------------------------------------------------- MCP ----
class Host:
    def __init__(self, exe: Path, env: dict, log_path: Path):
        self.log_path = log_path
        self.proc = subprocess.Popen(
            [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(log_path, "w", encoding="utf-8"),
            env=env, text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0
        self.calls = 0

    def rpc(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise SystemExit(f"host closed stdout; see {self.log_path}")
            reply = json.loads(line)
            if reply.get("id") == self._id:
                return reply

    def tool_names(self):
        return [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]

    def call(self, name, **kwargs):
        self.calls += 1
        reply = self.rpc("tools/call", {"name": name, "arguments": kwargs})
        if "error" in reply:
            raise SystemExit(f"{name} failed: {reply['error']}")
        result = json.loads(reply["result"]["content"][0]["text"])
        if reply["result"].get("isError"):
            raise SystemExit(f"{name} failed: {result}")
        print(f"  {name}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())[:90]})", flush=True)
        return result

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def look_at(eye, target):
    """Viewpoint orientation for the ENU camera (base forward = +X, up = +Z)."""
    def norm(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def sub(a, b):
        return [a[i] - b[i] for i in range(3)]

    def dot(a, b):
        return sum(a[i] * b[i] for i in range(3))

    def cross(a, b):
        return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]

    f = norm(sub(target, eye))
    up = [0.0, 0.0, 1.0]
    u = norm([up[i] - dot(up, f) * f[i] for i in range(3)])
    side = cross(u, f)
    m = [[f[0], side[0], u[0]], [f[1], side[1], u[1]], [f[2], side[2], u[2]]]
    tr = m[0][0] + m[1][1] + m[2][2]
    angle = math.acos(max(-1.0, min(1.0, (tr - 1.0) / 2.0)))
    axis = [m[2][1] - m[1][2], m[0][2] - m[2][0], m[1][0] - m[0][1]]
    axis = norm(axis)
    return [round(v, 5) for v in axis] + [round(angle, 5)]


# ---------------------------------------------------------------- GIF ----
def build_gif(shots, out_path):
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\consola.ttf", 17)
        small = ImageFont.truetype(r"C:\Windows\Fonts\consola.ttf", 15)
    except OSError:
        font = small = ImageFont.load_default()

    processed, durations = [], []
    for shot in shots:
        im = Image.open(shot["path"]).convert("RGB")
        # crop the Webots viewport to 16:9 without stretching it
        w, h = im.size
        target_h = int(w * 9 / 16)
        if h > target_h:
            top = max(0, (h - target_h) // 2)
            im = im.crop((0, top, w, top + target_h))
        im = im.resize((800, int(im.height * 800 / im.width)), Image.LANCZOS)

        canvas = Image.new("RGB", (im.width, im.height + CAPTION_H), (13, 17, 23))
        canvas.paste(im, (0, 0))
        d = ImageDraw.Draw(canvas)
        d.rectangle([0, im.height, im.width, im.height + 1], fill=(45, 53, 66))
        d.text((14, im.height + 14), shot["caption"], fill=(226, 232, 240), font=font)
        label = "webots-mcp"
        d.text((im.width - 14 - d.textlength(label, font=small), im.height + 15),
               label, fill=(94, 234, 212), font=small)
        processed.append(canvas)
        durations.append(shot["duration"])

    # one shared palette keeps the GIF small and the colours stable
    palette_src = processed[len(processed) // 2]
    palette = palette_src.quantize(colors=160, method=Image.MEDIANCUT)
    frames = [im.quantize(palette=palette, dither=Image.FLOYDSTEINBERG) for im in processed]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out_path, save_all=True, append_images=frames[1:],
        duration=durations, loop=0, optimize=True, disposal=1,
    )
    return out_path


# ---------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webots-home", default=os.environ.get("WEBOTS_HOME", r"C:\Program Files\Webots"))
    ap.add_argument("--webots-bin")
    ap.add_argument("--host-bin", default=str(REPO / "host" / "target" / "release" / f"webots-mcp-host{EXE}"))
    ap.add_argument("--out", default=str(REPO / "docs" / "media" / "demo.gif"))
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--no-gif", action="store_true")
    args = ap.parse_args()

    host_bin = Path(args.host_bin)
    if not host_bin.is_file():
        sys.exit(f"host binary not found: {host_bin} (run `cargo build --release` in host/)")

    work = Path(tempfile.mkdtemp(prefix="webots-mcp-demo-"))
    save_dir = work / "save"
    env = dict(
        os.environ,
        WEBOTS_HOME=args.webots_home,
        WEBOTS_MCP_IPC=f"127.0.0.1:{free_port()}",
        WEBOTS_MCP_STATE_DIR=str(work / "state"),
        WEBOTS_MCP_SUPERVISOR_DIR=str(REPO / "supervisor"),
        WEBOTS_MCP_PROJECTS_DIR=str(work / "projects"),
        WEBOTS_MCP_PYTHON=sys.executable,
        WEBOTS_MCP_SAVE_DIR=str(save_dir),
        WEBOTS_MCP_LOG="info",
    )
    if args.webots_bin:
        env["WEBOTS_BIN"] = args.webots_bin

    print(f"work dir: {work}")
    host = Host(host_bin, env, work / "host.log")
    shots = []

    def shot(name, caption, duration):
        result = host.call("capture_screenshot", filename=name)
        shots.append({"path": Path(result["path"]), "caption": caption, "duration": duration})

    try:
        host.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "demo-recorder", "version": "1"}})
        host.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        host.proc.stdin.flush()
        for _ in range(100):
            if "project_create" in host.tool_names():
                break
            time.sleep(0.1)

        project = host.call("project_create", project_name="mcp_demo",
                            description="Scene recorded for the README demo")
        world = project["world_path"]

        host.call("sim_start", world=world, connect_timeout_s=240)
        eye, target = [-3.0, 2.5, 2.55], [0.0, 0.0, 0.8]
        host.call("set_viewpoint", position=eye, orientation=look_at(eye, target))
        host.call("set_node_field", def_name="type:Viewpoint", field_name="fieldOfView", value=1.35)
        shot("build_000.png", "MCP ▸ sim_start  ▸  project + world created, Webots paused", 320)

        stage_def, _label, stage_color, stage_pos, stage_size, _m = STAGE
        host.call("create_primitive", def_name=stage_def, primitive_type="box",
                  translation=list(stage_pos), size=list(stage_size), color=list(stage_color), mass=0.0)
        shot("build_001.png", "MCP ▸ create_primitive  ▸  STAGE (static ground)", 170)

        for def_name, _label, color, translation, size, rotation in BOXES:
            host.call("create_primitive", def_name=def_name, primitive_type="box",
                      translation=list(translation), size=list(size), color=list(color),
                      rotation=[0, 0, 1, rotation], mass=0.4)
            shot(f"build_{len(shots):03d}.png", f"MCP ▸ create_primitive  ▸  {def_name} (dynamic)", 150)

        ball_def, _label, color, translation, size, mass = BALL
        host.call("create_primitive", def_name=ball_def, primitive_type="sphere",
                  translation=list(translation), size=list(size), color=list(color), mass=mass)
        shot(f"build_{len(shots):03d}.png", "MCP ▸ create_primitive  ▸  BALL (2 kg, dropped)", 300)

        # Step the paused world frame by frame and record what Webots renders.
        for i in range(FRAMES):
            if i:
                host.call("sim_step", count=STEPS_PER_FRAME)
            shot(f"frame_{i:03d}.png",
                 f"MCP ▸ sim_step ×{STEPS_PER_FRAME}  ▸  t = {i * STEPS_PER_FRAME * 0.016:4.2f} s"
                 if i else "MCP ▸ capture_screenshot  ▸  scene ready, simulation paused",
                 55)

        # One real experiment call, so the README can show measured numbers too.
        result = host.call("run_simulation", duration=1.2, target_defs=[ball_def, "BOX_1"],
                           sample_interval=0.1, wait_timeout=60)
        print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
        host.call("sim_stop")
    finally:
        host.close()

    if args.no_gif:
        print(f"frames kept in {work}")
        return

    out = build_gif(shots, Path(args.out))
    print(f"GIF: {out} ({out.stat().st_size / 1024 / 1024:.2f} MB, {len(shots)} frames)")
    if not args.keep_frames:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
