"""Physics health inspection."""
import math

from .scene import fmt, is_finite_seq, iter_all_nodes, physics_role, safe_node_state
from .config import log

PENETRATION_TOLERANCE = 1e-3  # metres


def _primitive(node):
    """('sphere', r) / ('box', [x, y, z]) for a plain Sphere/Box boundingObject, else None."""
    field = node.getField("boundingObject")
    geom = field.getSFNode() if field is not None else None
    if geom is not None and geom.getTypeName() == "Shape":
        gf = geom.getField("geometry")
        geom = gf.getSFNode() if gf is not None else None
    if geom is None:
        return None
    kind = geom.getTypeName()
    if kind == "Sphere":
        return ("sphere", float(geom.getField("radius").getSFFloat()))
    if kind == "Box":
        return ("box", [float(v) for v in geom.getField("size").getSFVec3f()])
    return None


def _unrotated(node):
    rot = node.getField("rotation")
    if rot is None:
        return True
    return abs(rot.getSFRotation()[3]) < 1e-6


def penetration_depth(a, b):
    """Overlap depth of two primitives ((kind, size), centre) in metres; <= 0 means apart.

    Axis-aligned only: boxes must be unrotated (callers filter), spheres are exact.
    """
    (ka, sa), pa = a
    (kb, sb), pb = b
    if ka == "sphere" and kb == "sphere":
        return sa + sb - math.dist(pa, pb)
    if ka == "box" and kb == "sphere":
        (ka, sa), pa, (kb, sb), pb = (kb, sb), pb, (ka, sa), pa
    if ka == "sphere":  # sphere vs box
        closest = [min(max(pa[i], pb[i] - sb[i] / 2), pb[i] + sb[i] / 2) for i in range(3)]
        d = math.dist(pa, closest)
        if d > 0:
            return sa - d
        # centre inside the box: distance to the nearest face
        return sa + min(sb[i] / 2 - abs(pa[i] - pb[i]) for i in range(3))
    # box vs box: smallest axis overlap (SAT for axis-aligned boxes)
    return min((sa[i] + sb[i]) / 2 - abs(pa[i] - pb[i]) for i in range(3))


def find_initial_penetrations(tolerance=PENETRATION_TOLERANCE):
    """Pairs of Sphere/Box colliders that overlap right now, with at least one dynamic body."""
    items, skipped = [], 0
    for node in iter_all_nodes():
        role = physics_role(node)
        if role not in ("dynamic", "static_collider"):
            continue
        prim = _primitive(node)
        pos = safe_node_state(node, include_orientation=False).get("position")
        if prim is None or not is_finite_seq(pos, 3) or (prim[0] == "box" and not _unrotated(node)):
            skipped += 1
            continue
        items.append((node, role, prim, pos))
    found = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (na, ra, pa, xa), (nb, rb, pb, xb) = items[i], items[j]
            if ra == "static_collider" and rb == "static_collider":
                continue
            depth = penetration_depth((pa, xa), (pb, xb))
            if depth > tolerance:
                found.append({"a": na.getDef() or na.getId(), "b": nb.getDef() or nb.getId(),
                              "depth_m": round(depth, 6)})
    found.sort(key=lambda e: -e["depth_m"])
    return found, skipped


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

    penetrations, skipped = find_initial_penetrations()
    for pair in penetrations:
        issues.append({"def": pair["a"], "id": None, "flags": [
            f"overlaps {pair['b']} by {pair['depth_m'] * 1000:.1f} mm: bodies that start "
            "interpenetrating get pushed apart on the first step (a visible kick at t=0)"]})

    hints = []

    if penetrations:
        hints.append(
            "Interpenetrating colliders were found: move one of them apart "
            "(set_node_pose) before running, or the first frames will show a spurious impulse. "
            f"Checked Sphere/Box colliders only (unrotated boxes); {skipped} other collider(s) skipped."
        )

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
        "initial_penetrations": penetrations,
        "bodies": bodies,
        "hints": hints,
        "note": (
            "physics=null with a boundingObject is the correct configuration "
            "of a static ground/wall; only dynamic bodies have a Physics node."
        ),
    }


