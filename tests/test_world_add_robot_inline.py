#!/usr/bin/env python3
"""Unit test (no Webots needed): world_add_robot with and without inline=true.

    python tests/test_world_add_robot_inline.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "supervisor"))

tmp = Path(tempfile.mkdtemp(prefix="webots-mcp-inline-"))
os.environ["WEBOTS_MCP_PROJECTS_DIR"] = str(tmp)
os.environ["WEBOTS_MCP_SUPERVISOR_DIR"] = str(ROOT / "supervisor")

import project_manager as pm  # noqa: E402

ARM = '''#VRML_SIM R2025b utf8
PROTO Arm2Dof [
  field SFVec3f    translation 0 0 0
  field SFRotation rotation    0 0 1 0
  field SFString   name        "arm"
  field SFString   controller  "arm_ctrl"
  field SFFloat    reach       0.5
]
{
  Robot {
    translation IS translation
    rotation IS rotation
    name IS name
    controller IS controller
    children [
      DEF LINK1 Solid {
        children [
          DEF TCP Pose { translation 0 0 0.5 }
        ]
      }
    ]
  }
}
'''

failed = 0


def check(label, cond, extra=""):
    global failed
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"\n     -> {extra}"))
    failed += 0 if cond else 1


def call(fn, **kw):
    return json.loads(asyncio.run(fn(**kw)))


d = call(pm.project_create, project_name="p")
check("project_create", d["status"] == "success", d)
(tmp / "p" / "protos" / "Arm2Dof.proto").write_text(ARM, encoding="utf-8")

# default mode: PROTO instance, but the result explains the DEF visibility rule
d = call(pm.world_add_robot, project_name="p", robot="Arm2Dof", translation=[1, 0, 0.1])
check("default mode adds a PROTO instance", d["status"] == "success" and not d.get("inline"), d)
check("default mode lists inner DEFs", d.get("inner_defs") == ["LINK1", "TCP"], d)
check("default mode explains the dotted path", "ARM2DOF.LINK1" in d.get("inner_def_note", ""), d)
world = (tmp / "p" / "worlds" / "main.wbt").read_text(encoding="utf-8")
check("world has DEF ARM2DOF Arm2Dof", "DEF ARM2DOF Arm2Dof {" in world and "DEF TCP" not in world, world)

# inline mode: body expanded, inner DEFs are plain DEFs, overrides applied once
d = call(pm.world_add_robot, project_name="p", robot="Arm2Dof", def_name="ARM_B", inline=True,
         translation=[0, 2, 0], controller="my_ctrl", fields='name "armB"')
check("inline adds nodes", d["status"] == "success" and d.get("inline") is True, d)
check("inline: inner DEFs reported", d.get("inner_defs") == ["LINK1", "TCP"], d)
world = (tmp / "p" / "worlds" / "main.wbt").read_text(encoding="utf-8")
blk = world[world.index("DEF ARM_B Robot {"):]
check("inline writes a Robot node", "DEF ARM_B Robot {" in world and "Arm2Dof {" not in blk, blk)
check("inline: inner DEF TCP is a plain DEF", "DEF TCP Pose" in world)
check("inline: IS references expanded", " IS " not in blk, blk)
check("inline: overrides applied without duplicates",
      blk.count("translation 0.0 2.0 0.0") == 1 and blk.count("controller ") == 1 and 'controller "my_ctrl"' in blk
      and blk.count("name ") == 1 and 'name "armB"' in blk, blk)
check("inline: defaults kept for untouched fields", "rotation 0 0 1 0" in blk, blk)

# second inline copy must not silently duplicate DEF TCP
d = call(pm.world_add_robot, project_name="p", robot="Arm2Dof", def_name="ARM_C", inline=True)
check("inline twice -> DefExists error", d["status"] == "error" and d["error_type"] == "DefExists", d)

# template PROTOs are refused with a hint
(tmp / "p" / "protos" / "Tpl.proto").write_text(
    "PROTO Tpl [ field SFInt32 n 2 ]\n{\n  %< const x = fields.n.value; >%\n  Robot { }\n}\n", encoding="utf-8")
d = call(pm.world_add_robot, project_name="p", robot="Tpl", inline=True)
check("template PROTO -> InlineUnsupported", d["status"] == "error" and d["error_type"] == "InlineUnsupported", d)

print("FAILED:", failed)
sys.exit(1 if failed else 0)
