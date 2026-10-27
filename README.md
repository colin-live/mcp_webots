# Webots MCP

**Drive a live Webots simulation from any MCP agent.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[简体中文](README.zh-CN.md)

Webots MCP is a local [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI agent create a Webots project, build and edit a 3D scene, step the simulation, run physics experiments, inspect results, and take screenshots — all through ordinary MCP tool calls.

![An agent builds a six-box stack and drops a ball on it in Webots](docs/media/demo.gif)

*The demo above is a real Webots session recorded through the MCP host: `project_create` → `sim_start` → scene building while paused → a physics run. No mock renderer.*

## Why this exists

Webots is a desktop application with its own process lifecycle, simulation clock, and Python controller API. An agent cannot simply "run a simulation" by executing a script: it needs a long-lived controller inside Webots, deterministic pause/step control, and a way to discover what the running scene contains.

This project puts those pieces behind one MCP endpoint:

- the **Rust host** owns Webots start/stop, the simulation clock, and the MCP session;
- a **Python Supervisor controller** runs inside Webots and exposes scene, physics, query, and experiment tools;
- a separate **project manager** process handles project and world files, even before Webots is running.

The agent sees one flat, dynamically discovered tool list — it never has to manage ports, controller processes, or Webots' GUI.

## Architecture

```text
MCP client (Claude Desktop, IDE agent, custom client, ...)
        │  MCP over stdio
        ▼
webots-mcp-host (Rust)
        │  starts / stops Webots, owns pause / step / reset
        │
        ├── TCP 127.0.0.1:<port>, NDJSON ──▶ Supervisor controller (Python, inside Webots)
        │                                      scene / physics / query / experiment tools
        │
        └── stdin/stdout ───────────────────▶ project_manager.py (independent process)
                                               projects / worlds / supervisor binding
```

- **The host is the only MCP server.** It merges its own 8 clock tools with the tools published by the Supervisor and the project manager.
- **Tool discovery is dynamic.** Each Python process sends a `hello` message with its tool manifest. The host caches the manifest, emits `notifications/tools/list_changed`, and can therefore show tools from a previous session before Webots starts again.
- **The Supervisor never blocks while paused.** It calls `step(0)` instead of `step(dt)` in pause mode, so scene edits, queries, and screenshots keep working without advancing simulation time.
- **The project manager is optional but recommended.** It enables `project_*`, `world_*`, and `supervisor_bind`, and works without a Webots installation.

## Features

- **Project scaffolding** — create a runnable Webots project (folders, `project.json`, ENU world with lighting/background/floor, Supervisor robot) in one call.
- **Scene building while paused** — primitives, raw VRML components, lights, backgrounds, and viewpoints; the scene changes without time advancing.
- **Deterministic clock control** — pause, resume in real-time or fast mode, advance exactly *N* basic time steps, and reset physics / simulation / world / process independently.
- **Physics experiments** — sample node positions over time, stop when objects settle or fall out of the world, and get displacement, path length, max speed, and rest time in one result.
- **Debugging tools for agents** — scene tree, full node details, contact points, static scene validation, and a physics health check that classifies every `Solid` as dynamic, static collider, or missing collision.
- **State snapshots** — save and restore node poses, then re-run the same experiment under different parameters.
- **Screenshots and world saves** — capture the actual Webots 3D viewport or persist the current world.
- **No Python packages required at runtime** — the Supervisor tool registry uses only the standard library and generates JSON schemas from type hints.

A fully connected session exposes **55 tools**: 8 host clock tools, 10 project/world tools, and 37 Supervisor tools.

## Requirements

| Component | Requirement |
|---|---|
| Webots | R2025a or newer; tested with **R2025b** on Windows |
| Rust | A recent stable toolchain (edition 2021) |
| Python | **3.10+** (the Supervisor uses modern type syntax) |
| OS | Developed and tested on **Windows 10/11**; Linux/macOS are not yet verified |
| Build tools | `cargo`; no system libraries beyond the Rust toolchain |

Webots must be able to create an OpenGL window. The screenshot and demo tools need a working GPU/OpenGL context — a plain headless shell or some remote-desktop sessions are not enough.

## Quick start

### 1. Clone and build the host

```bash
git clone https://github.com/colin-live/mcp_webots.git
cd mcp_webots/host
cargo build --release
```

The binary is written to `host/target/release/webots-mcp-host` (`.exe` on Windows).

### 2. Configure your MCP client

Register the binary as a stdio MCP server. Example for a generic client:

```json
{
  "mcpServers": {
    "webots": {
      "command": "D:/tools/mcp_webots/host/target/release/webots-mcp-host.exe",
      "env": {
        "WEBOTS_HOME": "C:/Program Files/Webots",
        "WEBOTS_MCP_SUPERVISOR_DIR": "D:/tools/mcp_webots/supervisor",
        "WEBOTS_PROJECTS_DIR": "D:/webots_projects",
        "WEBOTS_MCP_PYTHON": "python"
      }
    }
  }
}
```

Use absolute paths. Forward slashes avoid JSON escaping on Windows.

| Variable | Purpose |
|---|---|
| `WEBOTS_HOME` | Webots installation directory. If omitted, the host probes common install locations and `PATH`. |
| `WEBOTS_MCP_SUPERVISOR_DIR` | Path to this repository's `supervisor/` directory. **Enables the project manager** and is used as the shared controller source. |
| `WEBOTS_PROJECTS_DIR` | Root directory where `project_create` writes projects. Defaults to a path inferred from the supervisor location, so set it explicitly. |
| `WEBOTS_MCP_PYTHON` | Python executable used for `project_manager.py`. Defaults to `python` on Windows, `python3` elsewhere. |
| `WEBOTS_MCP_WORLD` | Default `.wbt` world used by `sim_start` when no `world` argument is given. |
| `WEBOTS_BIN` | Explicit path to the Webots executable, overriding `WEBOTS_HOME` discovery. |
| `WEBOTS_MCP_STATE_DIR` | Cache directory for the Supervisor tool manifest. Defaults to a per-user cache directory. |
| `WEBOTS_MCP_SAVE_DIR` | Where the Supervisor writes screenshots and saved worlds. Project-created controllers default this to `<project>/data`. |
| `WEBOTS_MCP_LOG` | Host log level, e.g. `debug`. Logs go to stderr; stdout is reserved for MCP messages. |
| `WEBOTS_MCP_TOKEN` | Optional shared secret that the Supervisor must present. Useful if other local processes could reach the IPC port. |
| `WEBOTS_MCP_AUTO_RESUME` | Set to `0` to stop `run_simulation` from automatically resuming a paused world. |

### 3. Let the agent work

Ask your agent to call `get_workflow_guide` first; it returns the registered tool names and the recommended build → experiment → analyze → fix loop.

Example prompt:

> Create a Webots project called `drop_test`, build a six-box stack and a ball above it, run the simulation for three seconds, capture a screenshot, and tell me whether the stack collapsed.

A typical manual sequence looks like this:

```text
project_create(project_name="drop_test")
sim_start(world="<world_path returned by project_create>")
create_primitive(def_name="BALL", primitive_type="Sphere", size=[0.2], mass=2.0,
                 translation=[0.2, 0.0, 2.5], color=[1.0, 0.8, 0.2])
set_viewpoint(...)                       # frame the scene for screenshots
save_snapshot(name="start")              # optional: repeatable initial state
run_simulation(duration=2.0, target_defs=["BALL"])
capture_screenshot(filename="drop_test.png")
sim_stop()
```

Because `sim_start` defaults to **pause** mode, all building steps happen at `t = 0`. `run_simulation` resumes automatically when needed and re-pauses when the observation finishes.

## Tool catalog

Tool names and JSON schemas are the MCP-facing interface. Descriptions currently ship in Chinese; names, parameters, and results are language-neutral JSON.

### Host and simulation clock (8)

| Tool | What it does |
|---|---|
| `sim_start` | Start Webots and load a world. Defaults to paused, so the scene can be built first. Waits for the Supervisor to connect. |
| `sim_stop` | Gracefully stop the Supervisor, then Webots (or force-kill the process tree). |
| `sim_restart` | Stop and relaunch Webots for a clean world state. |
| `sim_pause` | Pause the simulation clock. Scene and query tools remain usable. |
| `sim_resume` | Run in `real_time` or `fast` mode. |
| `sim_step` | Advance exactly `count` basic time steps from pause, then pause again. |
| `sim_reset` | Reset at four levels: `physics`, `simulation`, `reload`, or `restart`. |
| `sim_status` | Report the process, Supervisor connection, clock state, mode, and simulation time. |

Reset semantics:

| `sim_reset(kind=...)` | Effect |
|---|---|
| `physics` | Zero physical state; time and scene stay as they are. |
| `simulation` | Reset time and restore the saved world; runtime-created nodes are removed. Save first if you need them. |
| `reload` | Reload the world and restart the Supervisor; snapshots and observations are lost. |
| `restart` | Stop and relaunch Webots. |

### Projects and world files (10)

Provided by `project_manager.py`; available without Webots.

| Tool | What it does |
|---|---|
| `project_list` | List projects under the projects root. |
| `project_create` | Create a complete runnable project: directories, manifest, ENU world, floor, lighting, Supervisor robot and controller. Returns `world_path`. |
| `project_info` | Show worlds, PROTOs, controllers, Supervisor binding, manifest, and a directory tree. |
| `project_check` | Static validation of world headers, `EXTERNPROTO` references, controller folders, and Supervisor binding. |
| `project_delete` | Delete a project; requires the project name to be repeated exactly. |
| `project_read_file` | Read a UTF-8 text file inside a project, with path-escape protection. |
| `project_write_file` | Write a text file inside a project, creating parent directories. |
| `world_add_robot` | Insert a robot into a `.wbt` file, add the `EXTERNPROTO`, and optionally write its controller source. |
| `world_remove_node` | Remove a root-level `DEF` node from a world file. |
| `supervisor_bind` | Attach a Supervisor robot and controller to an existing project/world. |

When a project is created, the controller is installed in **link** mode by default: a tiny launcher points at this repository's `supervisor/` code, so there is only one copy to maintain. Use `mode="copy"` to make a project self-contained.

World-file tools edit the `.wbt` on disk. Call `sim_restart` or `sim_reset(kind="reload")` before expecting the running simulation to see the change.

### Scene, physics, queries, and experiments (37)

These tools are executed by the Supervisor inside the running Webots process.

| Group | Tools |
|---|---|
| Build | `create_primitive`, `create_component`, `create_components`, `add_light`, `set_world_appearance`, `set_viewpoint`, `delete_node`, `clear_created_nodes` |
| Edit | `set_node_field`, `set_node_pose`, `import_into_field`, `set_world_physics`, `set_contact_properties` |
| Physics | `set_node_velocity`, `apply_force`, `apply_torque`, `reset_node_physics` |
| Query | `get_scene_tree`, `get_node_details`, `get_node_state`, `get_nodes_by_type`, `get_contact_points`, `check_physics_health`, `validate_scene` |
| Experiment | `get_simulation_info`, `start_simulation`, `run_simulation`, `get_simulation_status`, `get_simulation_result`, `list_simulations`, `reset_simulation` |
| Snapshots | `save_snapshot`, `restore_snapshot`, `list_snapshots` |
| Output | `capture_screenshot`, `save_world` |
| Guide | `get_workflow_guide` |

Useful details:

- `create_primitive` builds `Box`, `Sphere`, `Cylinder`, or `Capsule` solids. `mass=0` creates a static collider (floor/wall); `mass>0` creates a dynamic body.
- `run_simulation` starts an observation, waits for it, and returns a summary plus sampled trajectory frames. Set `stop_when_settled=true` to end early, or use `start_simulation` + status/result polling for long runs.
- `check_physics_health` distinguishes bodies that are correctly static, dynamic, or missing collision geometry — often the fastest way to diagnose objects that fall through the floor.
- `capture_screenshot` writes to `<save dir>/screenshots/` (JPEG by default) and reflects the current `Viewpoint`; frame the scene with `set_viewpoint` first. PNGs are re-compressed losslessly (Webots exports them almost uncompressed, ~7.9 MB at 1955×1344); `scale` (needs Pillow) shrinks the image. Webots does not redraw while paused, so the result carries `stale: true` when the image is byte-identical to the previous one although the scene or viewpoint changed — resume in `real_time` first, or pass `render_steps=1` (advances simulation time).
- Snapshot tools store node poses only. They do not snapshot velocities, contacts, or the world file.

## Repository layout

```text
host/                     Rust MCP host
  src/mcp.rs              MCP stdio session and dynamic tool list
  src/sim.rs              Webots process + clock tools (sim_*)
  src/pm.rs               project_manager child process
  src/ipc.rs              NDJSON IPC to the Supervisor
supervisor/               Python code executed inside Webots
  mcp_server.py           Supervisor controller entry point
  project_manager.py      project/world tools (standalone process)
  webots_mcp/             clock, IPC client, scene helpers, tool modules
tests/                    end-to-end tests and fake Webots stand-ins
docs/                     demo generator and demo media
```

## Testing

The end-to-end suite runs the **real** Supervisor code against a fake Webots process, so it needs no Webots installation:

```bash
cd host
cargo build --release
cd ..
python tests/e2e.py --release
```

It covers dynamic tool exposure, scene tools while paused, exact stepping, pause/resume, reset/reload/restart, graceful shutdown, project-manager crash recovery, and host exit on stdin close.

Optional tests against a real Webots installation (a window will open):

```bash
python tests/real_smoke.py  --webots-home "C:\Program Files\Webots"
python tests/real_project.py --webots-home "C:\Program Files\Webots"
```

`real_smoke.py` drives a typical build-and-run session; `real_project.py` verifies that a generated project, its `EXTERNPROTO` entries, linked controller, and Supervisor binding survive a real world reload.

## Recreating the demo GIF

The demo is produced by driving a real Webots instance through the MCP host, not by screen recording:

```bash
pip install pillow
python docs/make_demo.py --webots-home "C:\Program Files\Webots"
```

The script creates a temporary project, starts Webots, builds the scene, steps the simulation, captures frames with `capture_screenshot`, and assembles `docs/media/demo.gif`. Add `--keep-frames` to inspect the raw frames.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Webots could not initialize the rendering system` or `Failed to load opengl32sw` | Webots has no usable OpenGL context. Run on a normal desktop session with GPU drivers; remote desktop, headless sessions, and missing drivers commonly cause this. |
| Project tools are missing from `tools/list` | `WEBOTS_MCP_SUPERVISOR_DIR` is not set, or it does not point at the `supervisor/` directory. |
| `sim_start` times out waiting for the Supervisor | The world has no Supervisor robot with controller `mcp_server`, or the controller cannot reach the host. Check the Webots console and the host's stderr log (`WEBOTS_MCP_LOG=debug`). |
| `sim_step` advances more than requested | Another non-synchronized controller is also advancing the simulation. Use a single synchronized Supervisor and robots with `synchronization TRUE`. |
| Newly added world nodes are not visible | World-file edits (`world_add_robot`, `world_remove_node`) require `sim_restart` or `sim_reset(kind="reload")`. |
| Runtime-created objects disappear after reset | `sim_reset(kind="simulation")` restores the saved world. Call `save_world` first if the objects should persist. |
| Webots stays open after the host is killed | On Windows the host uses a process tree kill for normal shutdown, but a hard-killed host cannot clean up its child. Close Webots manually. |

## Limitations

- Windows is the tested platform. The code is intended to be portable, but Linux/macOS path detection and process-tree termination have not been validated.
- The tool surface covers common scene building, rigid-body physics, and observation workflows — not the entire Webots API (no camera image streams, lidar processing, motor control loops, or custom physics plugins).
- `sim_step` precision assumes the Supervisor is the only controller advancing the world.
- Tool descriptions are currently written in Chinese; MCP tool names, schemas, and returned JSON keys are stable and language-neutral.
- Screenshots require a visible 3D viewport. Fast mode may not render every frame.

## Contributing

Issues and pull requests are welcome.

- Add Supervisor tools with the `@tool` decorator in `supervisor/webots_mcp/tools/`; JSON schemas are generated from type hints.
- Tools that must control the Webots process or clock belong in the Rust host (`host/src/sim.rs`), not in the Supervisor.
- Run `python tests/e2e.py --release` before opening a pull request.
- Keep new user-facing documentation in English and update `README.zh-CN.md` when behavior changes.

## License

[Apache License 2.0](LICENSE).
