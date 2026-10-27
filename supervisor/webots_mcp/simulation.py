"""Simulation observation tasks (sampling, statistics, history)."""
import math
import uuid

from .runtime import (
    MAX_SAMPLE_VALUES,
    MAX_SIMULATION_HISTORY,
    MAX_SIMULATION_TARGETS,
    TIME_STEP,
    distance,
    is_finite_seq,
    log,
    require_def_ref,
    require_finite_number,
    submit_command,
    supervisor,
)
from .scene import PROTO_DEF_HINT, has_physics_node, physics_role, resolve_def, safe_node_state

# ============================================================
# Experiment (observation) tasks
#
# All task state is touched only on the Webots main thread.
# Statistics are accumulated incrementally, so a summary is cheap and the
# raw frames are optional.
# ============================================================

simulation_tasks = {}
active_simulation_id = None
_simulation_counter = 0


def new_stats():
    return {
        "first_position": None,
        "last_position": None,
        "last_time": None,
        "min_position": None,
        "max_position": None,
        "path_length": 0.0,
        "max_speed": 0.0,
        "last_motion_time": None,
        "valid_samples": 0,
        "missing": False,
        "fell": False,
    }


def sample_task(task, now):
    frame = {"t": round(now - task["start_time"], 6), "nodes": {}}

    for name in task["target_defs"]:
        stats = task["stats"][name]
        node = resolve_def(name)

        if node is None:
            stats["missing"] = True
            frame["nodes"][name] = {"error": "Node no longer exists"}
            continue

        try:
            state = safe_node_state(node, include_orientation=False)
            pos = state.get("position")

            if not is_finite_seq(pos, 3):
                frame["nodes"][name] = {"error": "position unavailable"}
                continue

            if task["fall_z"] is not None and pos[2] < task["fall_z"]:
                stats["fell"] = True

            entry = {"position": pos}
            speed = None
            vel = state.get("velocity")

            if is_finite_seq(vel, 6):
                entry["velocity"] = vel
                speed = math.sqrt(sum(v * v for v in vel[:3]))

            prev = stats["last_position"]

            if prev is not None:
                step_len = distance(prev, pos)
                stats["path_length"] += step_len
                if speed is None and now > stats["last_time"]:
                    speed = step_len / (now - stats["last_time"])

            if task["include_orientation"]:
                entry["orientation"] = list(node.getOrientation())

            if speed is not None:
                entry["speed"] = speed
                stats["max_speed"] = max(stats["max_speed"], speed)

            if stats["first_position"] is None:
                stats["first_position"] = list(pos)
                stats["min_position"] = list(pos)
                stats["max_position"] = list(pos)
                stats["last_motion_time"] = now
            else:
                stats["min_position"] = [
                    min(a, b) for a, b in zip(stats["min_position"], pos)
                ]
                stats["max_position"] = [
                    max(a, b) for a, b in zip(stats["max_position"], pos)
                ]

            if speed is not None and speed > task["settle_speed"]:
                stats["last_motion_time"] = now

            stats["last_position"] = list(pos)
            stats["last_time"] = now
            stats["valid_samples"] += 1

            frame["nodes"][name] = entry

        except Exception as exc:
            frame["nodes"][name] = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    task["samples"].append(frame)


def all_settled(task, now):
    live = [
        s for s in task["stats"].values()
        if not s["missing"] and s["valid_samples"] > 0
    ]

    if not live:
        return False

    return all(now - s["last_motion_time"] >= task["settle_time"] for s in live)


def finish_task(task, status, message, now):
    global active_simulation_id

    task["status"] = status
    task["finished_time"] = now
    task["message"] = message

    if active_simulation_id == task["simulation_id"]:
        active_simulation_id = None


def update_simulation_task():
    """Called once per Webots main-loop iteration."""
    if active_simulation_id is None:
        return

    task = simulation_tasks.get(active_simulation_id)

    if task is None or task["status"] != "running":
        return

    now = supervisor.getTime()

    if now + 1e-9 >= task["next_sample_time"]:
        sample_task(task, now)

        interval = task["sample_interval"]
        while task["next_sample_time"] <= now + 1e-9:
            task["next_sample_time"] += interval

        if len(task["samples"]) >= task["max_frames"]:
            finish_task(
                task, "sample_limit_reached",
                "Maximum frame count reached; collection stopped.", now,
            )
            return

    fallen = [n for n, st in task["stats"].items() if st["fell"]]

    if fallen:
        task["stopped_early"] = True
        finish_task(
            task, "completed",
            f"Stopped early: {fallen} fell below fall_z={task['fall_z']} "
            "(no static collider underneath, or the body tunneled through "
            "it). Run check_physics_health.", now,
        )
        return

    elapsed = now - task["start_time"]

    if elapsed >= task["duration"]:
        finish_task(task, "completed", "Observation completed.", now)
    elif task["stop_when_settled"] and all_settled(task, now):
        task["stopped_early"] = True
        finish_task(
            task, "completed",
            "Stopped early: all tracked nodes have settled.", now,
        )


def get_task(simulation_id):
    task = simulation_tasks.get(simulation_id)
    if task is None:
        raise ValueError(
            f"Simulation task not found: {simulation_id!r}. "
            f"Existing ids: {list(simulation_tasks)}. "
            "Pass an empty simulation_id to use the running/latest task."
        )
    return task


def resolve_task(simulation_id=""):
    """Empty id -> the running task, otherwise the most recently created one."""
    if simulation_id:
        return get_task(simulation_id)

    if active_simulation_id in simulation_tasks:
        return simulation_tasks[active_simulation_id]

    if not simulation_tasks:
        raise ValueError(
            "No simulation task exists yet; call run_simulation or "
            "start_simulation first"
        )

    return max(simulation_tasks.values(), key=lambda t: t["created_order"])


def simulation_status_data(task):
    now = supervisor.getTime()

    if task["status"] == "running" or task["finished_time"] is None:
        elapsed = max(0.0, now - task["start_time"])
    else:
        elapsed = max(0.0, task["finished_time"] - task["start_time"])

    return {
        "simulation_id": task["simulation_id"],
        "status": task["status"],
        "duration": task["duration"],
        "elapsed": min(elapsed, task["duration"]),
        "progress": min(1.0, elapsed / task["duration"]),
        "sample_interval": task["sample_interval"],
        "sample_count": len(task["samples"]),
        "target_defs": task["target_defs"],
        "stop_when_settled": task["stop_when_settled"],
        "stopped_early": task["stopped_early"],
        "message": task["message"],
    }


def task_summary(task):
    end_time = (
        task["finished_time"]
        if task["finished_time"] is not None
        else supervisor.getTime()
    )
    out = {}

    for name in task["target_defs"]:
        s = task["stats"][name]

        if s["valid_samples"] == 0:
            out[name] = {
                "error": "no valid samples (node missing or has no pose)",
                "node_missing": s["missing"],
            }
            continue

        first, last = s["first_position"], s["last_position"]
        displacement = [b - a for a, b in zip(first, last)]
        settled = end_time - s["last_motion_time"] >= task["settle_time"]

        out[name] = {
            "first_position": first,
            "last_position": last,
            "displacement": displacement,
            "displacement_norm": math.sqrt(sum(d * d for d in displacement)),
            "path_length": s["path_length"],
            "max_speed": s["max_speed"],
            "min_position": s["min_position"],
            "max_position": s["max_position"],
            "settled": settled,
            "time_to_rest": (
                s["last_motion_time"] - task["start_time"] if settled else None
            ),
            "node_missing_during_run": s["missing"],
            "fell_below_fall_z": s["fell"],
        }

    return out


def pick_frames(samples, max_samples):
    n = len(samples)

    if n <= max_samples:
        return list(samples)

    if max_samples == 1:
        return [samples[-1]]

    step = (n - 1) / (max_samples - 1)
    indices = sorted({round(i * step) for i in range(max_samples)})
    return [samples[i] for i in indices]


def simulation_result_data(simulation_id, max_samples, summary_only):
    task = resolve_task(simulation_id)
    samples = task["samples"]

    data = {
        "status": "success",
        "simulation": simulation_status_data(task),
        "targets": task["targets"],
        "summary": task_summary(task),
        "total_samples": len(samples),
    }

    if not summary_only:
        frames = pick_frames(samples, max_samples)
        data["returned_samples"] = len(frames)
        data["samples"] = frames  # evenly spaced over the whole run

    return data


def prune_simulation_history():
    while len(simulation_tasks) > MAX_SIMULATION_HISTORY:
        candidates = [
            (task_id, task)
            for task_id, task in simulation_tasks.items()
            if task_id != active_simulation_id and task["status"] != "running"
        ]

        if not candidates:
            break

        oldest = min(candidates, key=lambda item: item[1]["created_order"])[0]
        del simulation_tasks[oldest]


def start_task(
    duration,
    sample_interval,
    target_defs,
    stop_when_settled=False,
    settle_speed=0.01,
    settle_time=0.5,
    include_orientation=False,
    fall_z=-100.0,
):
    """Validate and start an observation. Runs on the Webots main thread."""
    global active_simulation_id, _simulation_counter

    duration = require_finite_number(duration, "duration")
    interval = require_finite_number(sample_interval, "sample_interval")
    settle_speed = require_finite_number(settle_speed, "settle_speed")
    settle_time = require_finite_number(settle_time, "settle_time")

    if fall_z is not None:
        fall_z = require_finite_number(fall_z, "fall_z")

    if duration <= 0:
        raise ValueError("duration must be greater than zero")

    if interval < TIME_STEP / 1000.0:
        raise ValueError(
            "sample_interval must be at least the Webots basic time step "
            f"({TIME_STEP / 1000.0:.6g} s)"
        )

    if settle_speed < 0 or settle_time <= 0:
        raise ValueError("settle_speed must be >= 0 and settle_time > 0")

    if not isinstance(target_defs, list) or not target_defs:
        raise ValueError("target_defs must be a non-empty list of DEF names")

    if len(target_defs) > MAX_SIMULATION_TARGETS:
        raise ValueError(f"At most {MAX_SIMULATION_TARGETS} nodes can be tracked")

    names = [require_def_ref(n) for n in target_defs]

    if len(set(names)) != len(names):
        raise ValueError("target_defs contains duplicate DEF names")

    max_frames = max(1, MAX_SAMPLE_VALUES // len(names))
    expected = duration / interval + 1

    if expected > max_frames:
        raise ValueError(
            f"Too many frames ({expected:.0f} > {max_frames}); "
            "increase sample_interval or shorten duration"
        )

    if active_simulation_id is not None:
        current = simulation_tasks.get(active_simulation_id)
        if current and current["status"] == "running":
            raise RuntimeError(
                "Another observation is already running: "
                f"{active_simulation_id}. Wait for it or call reset_simulation."
            )
        active_simulation_id = None

    targets = []
    for name in names:
        node = resolve_def(name)
        if node is None:
            raise ValueError(
                f"Target DEF not found: {name}. "
                "Call get_scene_tree() to inspect valid DEF names. "
                + PROTO_DEF_HINT
            )
        targets.append({
            "def": name,
            "type": node.getTypeName(),
            "has_physics": has_physics_node(node),
        })

    now = supervisor.getTime()
    simulation_id = str(uuid.uuid4())
    _simulation_counter += 1

    task = {
        "simulation_id": simulation_id,
        "status": "running",
        "duration": duration,
        "sample_interval": interval,
        "target_defs": names,
        "targets": targets,
        "start_time": now,
        "next_sample_time": now,
        "finished_time": None,
        "samples": [],
        "stats": {name: new_stats() for name in names},
        "settle_speed": settle_speed,
        "settle_time": settle_time,
        "stop_when_settled": bool(stop_when_settled),
        "include_orientation": bool(include_orientation),
        "fall_z": fall_z,
        "stopped_early": False,
        "max_frames": max_frames,
        "message": "Observation started.",
        "created_order": _simulation_counter,
    }

    simulation_tasks[simulation_id] = task
    active_simulation_id = simulation_id
    prune_simulation_history()

    return {
        "status": "success",
        "simulation": simulation_status_data(task),
        "targets": targets,
        "note": (
            "The world is NOT restarted: this observes the running "
            "simulation from now on. Use save_snapshot/restore_snapshot "
            "to repeat an experiment from the same initial state."
        ),
    }


