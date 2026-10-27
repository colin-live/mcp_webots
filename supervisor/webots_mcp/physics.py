"""Physics health inspection."""
import math

from .scene import fmt, is_finite_seq, iter_all_nodes, physics_role, safe_node_state
from .config import log

def physics_health(max_abs_position=1000.0, max_speed=50.0):
    counts = {
        "dynamic": 0,
        "static_collider": 0,
        "no_collision": 0,
        "dynamic_without_boundingObject": 0,
    }
    bodies, issues = [], []

    for node in iter_all_nodes():
        role = physics_role(node)

        if role is None:
            continue

        counts[role] += 1

        state = safe_node_state(node, include_orientation=False)
        pos = state.get("position")
        vel = state.get("velocity")
        speed = (
            math.sqrt(sum(v * v for v in vel[:3]))
            if is_finite_seq(vel, 6) else None
        )

        flags = []

        if role == "dynamic_without_boundingObject":
            flags.append(
                "has Physics but no boundingObject: Webots does not simulate it"
            )

        if is_finite_seq(pos, 3) and any(abs(c) > max_abs_position for c in pos):
            flags.append(
                f"far from the origin (|coordinate| > {max_abs_position:g}): "
                "runaway body that fell out of the world?"
            )

        if speed is not None and speed > max_speed:
            flags.append(
                f"very high speed ({speed:.1f} m/s): it can tunnel through "
                "thin static colliders in a single step"
            )

        entry = {
            "def": node.getDef() or None,
            "id": node.getId(),
            "type": node.getTypeName(),
            "role": role,
            "position": pos,
            "speed": speed,
        }

        if flags:
            issues.append({"def": entry["def"], "id": entry["id"], "flags": flags})

        if (entry["def"] or flags) and len(bodies) < 100:
            bodies.append(entry)

    hints = []

    if counts["dynamic"] and not counts["static_collider"]:
        hints.append(
            "There are dynamic bodies but no static collider: nothing can stop "
            "them. Add a ground, e.g. create_primitive(Box, mass=0)."
        )

    if issues:
        hints.append(
            "For runaway bodies call set_node_pose (it resets physics first, "
            "then writes the pose and zeroes velocity) or delete_node."
        )

    return {
        "status": "success",
        "counts": counts,
        "has_static_collider": counts["static_collider"] > 0,
        "issues": issues,
        "bodies": bodies,
        "hints": hints,
        "note": (
            "physics=null with a boundingObject is the correct configuration "
            "of a static ground/wall; only dynamic bodies have a Physics node."
        ),
    }


