#!/usr/bin/env python3
"""Webots Supervisor controller: scene / objects / simulation data only.

Run as the controller of the single Supervisor robot (Robot.supervisor = TRUE,
controller "mcp_server").  This process no longer speaks MCP itself.  It
connects to the Rust host over IPC (see webots_mcp/ipc_client.py), publishes
its tool manifest, executes tool calls, and obeys the host's clock commands.

Threading rule (unchanged): every Webots API call runs on this main thread.
IPC threads only enqueue work; process_commands() / Clock.tick() execute it.
"""
import os
import queue
import sys

from webots_mcp import config  # configures logging
from webots_mcp import registry
from webots_mcp import runtime
from webots_mcp import scene
from webots_mcp import simulation
from webots_mcp import snapshots
import webots_mcp.tools  # noqa: F401  (importing registers all tools)

from webots_mcp.clock import Clock
from webots_mcp.ipc_client import IpcClient
from webots_mcp.runtime import (
    TIME_STEP,
    log,
    process_commands,
    server_running,
    supervisor,
)
from webots_mcp.simulation import update_simulation_task


def _refresh_world_handles():
    """Re-acquire root handles after a reset (modules import them by value)."""
    root = supervisor.getRoot()
    children = root.getField("children")
    for name, module in list(sys.modules.items()):
        if name.startswith("webots_mcp"):
            if getattr(module, "root_node", None) is not None:
                module.root_node = root
            if getattr(module, "root_children", None) is not None:
                module.root_children = children


def after_reset():
    """simulationReset() removed run-time nodes: forget everything about them."""
    scene.created_ids.clear()
    snapshots.contact_tracking.clear()
    now = supervisor.getTime()
    for task in simulation.simulation_tasks.values():
        if task["status"] == "running":
            simulation.finish_task(task, "cancelled", "Cancelled by world reset.", now)
    try:
        _refresh_world_handles()
    except Exception:
        log.exception("Could not refresh root handles after reset")


def _static_info():
    info = {
        "server_version": config.SERVER_VERSION,
        "time_step_ms": TIME_STEP,
        "pid": os.getpid(),
    }
    for key, getter in (("world_path", "getWorldPath"), ("project_path", "getProjectPath")):
        try:
            info[key] = getattr(supervisor, getter)()
        except Exception:
            info[key] = None
    return info


clock = Clock(supervisor, TIME_STEP, log, on_reset=after_reset,
              idle_sleep=config.IDLE_SLEEP, sync_gui=config.SYNC_GUI)
ipc = IpcClient(config.IPC_ADDR, config.IPC_TOKEN, _static_info(), clock, log)
clock.on_state = ipc.notify_state
runtime.clock = clock

log.info("==== Webots Supervisor (IPC) initialized ====")
log.info("Host IPC: %s | time step: %s ms | %d tools exported",
         config.IPC_ADDR, TIME_STEP, len(registry.TOOL_NAMES))
log.info("World save directory: %s", config.SAVE_DIR)

ipc.start()

# ============================================================
# Webots main loop: clock tick (step, or step(0) while paused),
# then run queued tool commands and sample observations.
# ============================================================

try:
    if clock.start():
        while clock.tick():
            process_commands()
            update_simulation_task()
            clock.after_tick(any(t["status"] == "running" for t in simulation.simulation_tasks.values()))
except Exception:
    log.exception("Fatal error in Webots Supervisor main loop")
finally:
    server_running.clear()
    ipc.stop()

    while True:
        try:
            cmd = runtime.command_queue.get_nowait()
        except queue.Empty:
            break
        try:
            if cmd.future.cancel():
                log.warning("Cancelled pending command during shutdown: %s", cmd.id)
        finally:
            try:
                runtime.command_queue.task_done()
            except Exception:
                pass

    log.info("Webots Supervisor stopped")
