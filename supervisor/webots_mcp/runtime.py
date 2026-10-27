"""Main-thread runtime: Supervisor init, command dispatcher and shared utilities."""
import asyncio
import json
import math
import queue
import re
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any

from controller import Supervisor

from .config import (
    COMMAND_TIMEOUT,
    MAX_COMMANDS_PER_STEP,
    MAX_PENDING_COMMANDS,
    MAX_SAMPLE_VALUES,
    MAX_SIMULATION_HISTORY,
    MAX_SIMULATION_TARGETS,
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    MAX_VRML_LENGTH,
    PROTECTED_ROOT_TYPES,
    SCREENSHOT_DIR,
    SERVER_VERSION,
    SINGLETON_ROOT_TYPES,
    log,
)

# ============================================================
# Supervisor initialization
# ============================================================

supervisor = Supervisor()
TIME_STEP = int(supervisor.getBasicTimeStep())

root_node = supervisor.getRoot()
root_children = root_node.getField("children")

if root_children is None:
    raise RuntimeError("Webots root.children is unavailable")


# ============================================================
# Main-thread command dispatcher
# ============================================================

command_queue = queue.Queue(maxsize=MAX_PENDING_COMMANDS)
server_running = threading.Event()
server_running.set()


class Command:
    def __init__(self, fn):
        self.id = str(uuid.uuid4())
        self.fn = fn
        self.future = Future()
        self.caller_timed_out = False


class SupervisorOperationError(RuntimeError):
    def __init__(self, message, operation_id=None):
        super().__init__(message)
        self.operation_id = operation_id


def submit_command(fn, timeout=COMMAND_TIMEOUT):
    """Run fn() on the Webots main thread and return its result.

    NOTE: commands are only processed when the simulation advances a step.
    If the world is paused (or Webots is not running) this will time out.
    """
    if not server_running.is_set():
        raise RuntimeError("Webots Supervisor MCP is shutting down")

    cmd = Command(fn)

    try:
        command_queue.put(cmd, timeout=1.0)
    except queue.Full as exc:
        raise RuntimeError(
            "Supervisor command queue is full; the operation was not submitted"
        ) from exc

    try:
        return cmd.future.result(timeout=timeout)

    except FutureTimeoutError as exc:
        # A queued operation can be cancelled; a running one may still finish.
        cancelled = cmd.future.cancel()
        cmd.caller_timed_out = True

        state = (
            "cancelled_before_execution"
            if cancelled
            else "execution_status_unknown"
        )

        log.error(
            "Supervisor command timed out: id=%s state=%s timeout=%.1fs",
            cmd.id, state, timeout,
        )

        if cancelled:
            detail = (
                "The operation was cancelled before execution. "
                "Is the simulation paused?"
            )
        else:
            detail = (
                "The operation may still complete. Inspect the scene "
                "before retrying a state-changing operation."
            )

        raise SupervisorOperationError(
            f"Supervisor command timed out; operation_id={cmd.id}; "
            f"state={state}. {detail}",
            operation_id=cmd.id,
        ) from exc


def process_commands():
    """Called exclusively from the Webots controller main thread."""
    for _ in range(MAX_COMMANDS_PER_STEP):
        try:
            cmd = command_queue.get_nowait()
        except queue.Empty:
            break

        try:
            if not cmd.future.set_running_or_notify_cancel():
                log.info("Skipped cancelled Supervisor command: id=%s", cmd.id)
                continue

            result = cmd.fn()

            if cmd.caller_timed_out:
                log.warning("Late Supervisor success: id=%s", cmd.id)

            cmd.future.set_result(result)

        except Exception as exc:
            log.warning(
                "Supervisor operation failed: id=%s type=%s msg=%s",
                cmd.id, type(exc).__name__, exc,
            )
            if cmd.future.running():
                cmd.future.set_exception(exc)

        finally:
            command_queue.task_done()


# ============================================================
# Common helpers
# ============================================================

def sanitize(obj):
    """Replace NaN/inf (Webots returns them for unavailable data) with None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj


def result_json(data):
    return json.dumps(
        sanitize(data), ensure_ascii=False, allow_nan=False, default=str
    )


def tool_error(tool_name, exc):
    data = {
        "status": "error",
        "tool": tool_name,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "operation_id": getattr(exc, "operation_id", None),
    }

    if isinstance(exc, (ValueError, TypeError)):
        log.warning("MCP tool rejected: tool=%s msg=%s", tool_name, exc)
    else:
        log.error(
            "MCP tool failed: tool=%s type=%s",
            tool_name, type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    return result_json(data)


async def call_main(tool_name, operation, timeout=COMMAND_TIMEOUT):
    """Run `operation` on the Webots main thread, return a JSON string."""
    try:
        data = await asyncio.to_thread(submit_command, operation, timeout)
        return result_json(data)
    except Exception as exc:
        return tool_error(tool_name, exc)


def fmt(value):
    return repr(float(value))


def fmt_vec(values):
    return " ".join(fmt(v) for v in values)


def is_finite_seq(seq, length):
    return (
        seq is not None
        and len(seq) == length
        and all(isinstance(v, (int, float)) and math.isfinite(v) for v in seq)
    )


def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


# ---------- validators ----------

def require_finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def require_int_range(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) \
            or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def vector(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} numbers")
    return [
        require_finite_number(v, f"{name}[{i}]") for i, v in enumerate(value)
    ]


def require_color(value, name):
    color = vector(value, 3, name)
    if any(c < 0 or c > 1 for c in color):
        raise ValueError(f"{name} components must be between 0 and 1")
    return color


def require_rotation(value, name):
    rot = vector(value, 4, name)
    norm = math.sqrt(sum(v * v for v in rot[:3]))
    if norm < 1e-9:
        raise ValueError(f"{name} axis (first 3 numbers) must be non-zero")
    return [rot[0] / norm, rot[1] / norm, rot[2] / norm, rot[3]]


def require_identifier(value, what):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", value
    ):
        raise ValueError(f"{what} must match [A-Za-z_][A-Za-z0-9_]*")
    return value


def require_def_name(name):
    return require_identifier(name, "DEF name")


