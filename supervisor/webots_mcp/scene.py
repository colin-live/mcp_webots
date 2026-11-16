"""Scene graph, node fields, VRML import and node discovery helpers."""
import math
import re
from typing import Any

from .runtime import (
    TIME_STEP,
    distance,
    fmt,
    fmt_vec,
    is_finite_seq,
    log,
    require_color,
    require_def_name,
    require_def_ref,
    require_finite_number,
    require_identifier,
    require_int_range,
    require_rotation,
    root_children,
    root_node,
    submit_command,
    supervisor,
    vector,
)
from .config import (
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    MAX_VRML_LENGTH,
    PROTECTED_ROOT_TYPES,
    SINGLETON_ROOT_TYPES,
)

# ---------- node lookup ----------

def find_root_node_by_type(node_type):
    for i in range(root_children.getCount()):
        node = root_children.getMFNode(i)
        if node is not None and node.getTypeName() == node_type:
            return node
    return None


PROTO_DEF_HINT = (
    "DEF names declared inside a PROTO body are not visible to the Supervisor by "
    "plain DEF name. Address them through the PROTO instance with a dotted path "
    "'<instance DEF>.<inner DEF>', e.g. 'ARM2DOF.TCP' for DEF TCP inside "
    "DEF ARM2DOF Arm2Dof {...}; get_scene_tree() lists the instance. "
    "Alternatively add the robot with world_add_robot(inline=true), which writes "
    "the PROTO body into the world so its inner DEFs become plain DEFs."
)


def resolve_def(ref):
    """Return the node for a DEF name or a dotted PROTO path, or None.

    'MY_ROBOT.TCP' = DEF TCP defined inside the PROTO instance DEF MY_ROBOT
    (Node.getFromProtoDef); nested instances chain: 'A.B.C'.
    """
    parts = require_def_ref(ref).split(".")
    node = supervisor.getFromDef(parts[0])
    for inner in parts[1:]:
        if node is None:
            return None
        lookup = getattr(node, "getFromProtoDef", None)
        if lookup is None:
            raise ValueError(
                "This Webots version cannot look up DEFs inside a PROTO "
                "(Node.getFromProtoDef is missing); use world_add_robot(inline=true)."
            )
        node = lookup(inner)
    return node


def get_node(ref):
    """Resolve a DEF name, 'PARENT.INNER' (DEF inside a PROTO), or 'type:<NodeType>'."""
    if isinstance(ref, str) and ref.startswith("type:"):
        type_name = require_identifier(ref[5:], "node type")
        node = find_root_node_by_type(type_name)
        if node is None:
            raise ValueError(f"No root node of type {type_name} in the world")
        return node

    node = resolve_def(ref)

    if node is None:
        raise ValueError(
            f"Node not found: DEF {ref}. "
            "Use get_scene_tree() to inspect existing DEF names. "
            "A node type name is not a DEF name; for root nodes without a "
            "DEF use 'type:<NodeType>', e.g. 'type:WorldInfo'. "
            + PROTO_DEF_HINT
        )

    return node


# ---------- read-only lookup by node id ----------

MAX_NODE_ID = 2**31 - 1


def parse_node_id_ref(ref):
    """The node id of an 'id:<n>' reference, or None when `ref` is not an id reference."""
    if not isinstance(ref, str) or not ref.startswith("id:"):
        return None
    text = ref[3:].strip()
    if not (text.isascii() and text.isdigit()) or not 0 < int(text) <= MAX_NODE_ID:
        raise ValueError("A node id reference must look like 'id:<positive integer>', e.g. 'id:19'")
    return int(text)


def get_node_by_id(node_id):
    try:
        node = supervisor.getFromId(node_id)
    except Exception:
        node = None
    if node is None:
        raise ValueError(
            f"No node with id {node_id}. Ids come from get_scene_tree, get_nodes_by_type and "
            "get_node_details, and they change when the world is reloaded."
        )
    return node


def get_readable_node(ref):
    """Like get_node(), and also accepts 'id:<n>' (the id reported by get_scene_tree,
    get_nodes_by_type and get_node_details).

    For read-only tools only. An id reaches nodes that have no DEF name (devices,
    jointParameters), which is the point. Modifying tools keep DEF-only addressing: ids are not
    stable across world reloads, so a stale id could silently edit the wrong node.
    """
    node_id = parse_node_id_ref(ref)
    return get_node_by_id(node_id) if node_id is not None else get_node(ref)


JOINT_PARAM_TYPES = {  # joint type -> {axis: parameters node type}
    "HingeJoint": {1: "HingeJointParameters"},
    "Hinge2Joint": {1: "HingeJointParameters", 2: "JointParameters"},
    "SliderJoint": {1: "JointParameters"},
}


def iter_joints():
    return (n for n in iter_all_nodes() if n.getTypeName() in JOINT_PARAM_TYPES)


def _joint_params_field(joint, axis):
    types = JOINT_PARAM_TYPES.get(joint.getTypeName())
    if types is None:
        raise ValueError(
            f"{joint.getTypeName()} is not a HingeJoint / Hinge2Joint / SliderJoint")
    if axis not in types:
        raise ValueError(f"{joint.getTypeName()} has no axis {axis}")
    field = joint.getField("jointParameters" if axis == 1 else "jointParameters2")
    if field is None:
        raise ValueError(f"{joint.getTypeName()} has no jointParameters field for axis {axis}")
    return field, types[axis]


def get_joint_position(joint, axis=1):
    """Current `position` (rad or m) of a joint axis; 0.0 when no parameters node exists."""
    field, _ = _joint_params_field(joint, axis)
    params = field.getSFNode()
    if params is None:
        return 0.0
    pos = params.getField("position")
    return float(pos.getSFFloat()) if pos is not None else 0.0


def set_joint_position(joint, position, axis=1):
    """Write the joint's `position` field (a Supervisor write moves the endPoint)."""
    field, params_type = _joint_params_field(joint, axis)
    params = field.getSFNode()
    if params is None:
        field.importSFNodeFromString(f"{params_type} {{ position {fmt(position)} }}")
        return
    pos = params.getField("position")
    if pos is None:
        raise ValueError("jointParameters has no position field")
    pos.setSFFloat(float(position))


# ---------- joint description (read-only) ----------

_MISSING = object()
_JOINT_PARAM_FIELDS = (
    "axis", "anchor", "position", "minStop", "maxStop",
    "springConstant", "dampingConstant", "staticFriction",
)
_MOTOR_FIELDS = {
    "RotationalMotor": ("maxTorque", "maxVelocity", "minPosition", "maxPosition"),
    "LinearMotor": ("maxForce", "maxVelocity", "minPosition", "maxPosition"),
}
_SENSOR_TYPES = ("PositionSensor",)
_DEVICE_FIELD_BY_AXIS = {1: "device", 2: "device2"}


def _read_field(node, name):
    """The value of a field, `_MISSING` when the node has no such field."""
    field = node.getField(name)
    if field is None:
        return _MISSING
    try:
        return field_value(field)
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error": str(exc)}


def describe_device(device, axis):
    type_name = device.getTypeName()
    info = {
        "id": device.getId(),
        "def": device.getDef() or None,
        "type": type_name,
        "axis_index": axis,
    }
    for key in ("name",) + _MOTOR_FIELDS.get(type_name, ()):
        value = _read_field(device, key)
        if value is not _MISSING:
            info[key] = value
    return info


def describe_joint(joint):
    """Read-only description of a HingeJoint / Hinge2Joint / SliderJoint.

    Returns the joint parameters of each axis (axis, anchor, stops, ...), every device on the
    joint (motors, sensors, brakes: their `name`, which is what Robot.getDevice() looks up, and
    motor limits), and `warnings` for things that make a joint ambiguous or incomplete: a missing
    jointParameters or endPoint, several motors or sensors on one axis, empty or duplicate device
    names.
    """
    joint_type = joint.getTypeName()
    param_types = JOINT_PARAM_TYPES.get(joint_type)
    if param_types is None:
        raise ValueError(f"{joint_type} is not a HingeJoint / Hinge2Joint / SliderJoint")

    warnings = []
    axes = []
    devices = []
    seen_names = {}

    for axis, params_type in param_types.items():
        params_name = "jointParameters" if axis == 1 else "jointParameters2"
        field = joint.getField(params_name)
        params = field.getSFNode() if field is not None else None
        entry = {"axis_index": axis, "field": params_name, "expected_type": params_type, "node": brief_node(params)}
        if params is None:
            entry["values"] = None
            warnings.append(f"{params_name} is null: Webots default joint parameters apply, nothing was read")
        else:
            values = {}
            for key in _JOINT_PARAM_FIELDS:
                value = _read_field(params, key)
                if value is not _MISSING:
                    values[key] = value
            entry["values"] = values
        axes.append(entry)

        device_field = joint.getField(_DEVICE_FIELD_BY_AXIS[axis])
        axis_devices = []
        if device_field is not None:
            for i in range(device_field.getCount()):
                device = device_field.getMFNode(i)
                if device is not None:
                    axis_devices.append(describe_device(device, axis))
        devices.extend(axis_devices)

        for kind, matches in (
            ("motor", [d for d in axis_devices if d["type"] in _MOTOR_FIELDS]),
            ("sensor", [d for d in axis_devices if d["type"] in _SENSOR_TYPES]),
        ):
            if len(matches) > 1:
                names = ", ".join(repr(d.get("name")) for d in matches)
                warnings.append(
                    f"{joint_type} axis {axis} has {len(matches)} {kind}s ({names}); "
                    f"anything that picks the {kind} automatically takes the first one"
                )

    for device in devices:
        name = device.get("name")
        if name == "":
            warnings.append(
                f"{device['type']} (id {device['id']}) has an empty name, so Robot.getDevice() cannot find it"
            )
        elif isinstance(name, str):
            seen_names.setdefault(name, []).append(device["type"])
    for name, types in seen_names.items():
        if len(types) > 1:
            warnings.append(f"device name {name!r} is used by {len(types)} devices on this joint ({', '.join(types)})")

    end_field = joint.getField("endPoint")
    end_point = end_field.getSFNode() if end_field is not None else None
    if end_point is None:
        warnings.append("endPoint is null: the joint has nothing attached")

    return {
        "joint": brief_node(joint),
        "joint_type": joint_type,
        "axes": axes,
        "devices": devices,
        "summary": {
            "motor_names": [d.get("name") for d in devices if d["type"] in _MOTOR_FIELDS],
            "sensor_names": [d.get("name") for d in devices if d["type"] in _SENSOR_TYPES],
        },
        "end_point": brief_node(end_point),
        "warnings": warnings,
    }


def has_physics_node(node):
    field = node.getField("physics")
    return field is not None and field.getSFNode() is not None


def physics_role(node):
    """dynamic | static_collider | no_collision | dynamic_without_boundingObject.

    None for nodes that are not Solid-like. A static ground is a Solid with a
    boundingObject and NO Physics node: `physics = null` is correct for it.
    """
    bounding = node.getField("boundingObject")

    if bounding is None:
        return None

    has_bounding = bounding.getSFNode() is not None

    if has_physics_node(node):
        return "dynamic" if has_bounding else "dynamic_without_boundingObject"

    return "static_collider" if has_bounding else "no_collision"


def safe_node_state(node, include_orientation=True):
    state = {
        "id": node.getId(),
        "def": node.getDef() or None,
        "type": node.getTypeName(),
    }

    # Pose-like nodes (Pose/Transform/Solid/Robot/devices...) have translation.
    if node.getField("translation") is not None:
        try:
            state["position"] = list(node.getPosition())
        except Exception:
            pass

        if include_orientation:
            try:
                state["orientation"] = list(node.getOrientation())
            except Exception:
                pass

    # Solid-like nodes have a physics field; velocity is NaN without Physics.
    if node.getField("physics") is not None:
        try:
            state["velocity"] = list(node.getVelocity())
        except Exception:
            pass

    return state


# ============================================================
# Field reading / writing
# ============================================================

SF_GETTERS = {
    "SFBool": "getSFBool",
    "SFInt32": "getSFInt32",
    "SFFloat": "getSFFloat",
    "SFString": "getSFString",
    "SFVec2f": "getSFVec2f",
    "SFVec3f": "getSFVec3f",
    "SFRotation": "getSFRotation",
    "SFColor": "getSFColor",
}

MF_GETTERS = {
    "MFBool": "getMFBool",
    "MFInt32": "getMFInt32",
    "MFFloat": "getMFFloat",
    "MFString": "getMFString",
    "MFVec2f": "getMFVec2f",
    "MFVec3f": "getMFVec3f",
    "MFRotation": "getMFRotation",
    "MFColor": "getMFColor",
}


def brief_node(node):
    if node is None:
        return None
    return {
        "id": node.getId(),
        "def": node.getDef() or None,
        "type": node.getTypeName(),
    }


def field_value(field):
    """Read a field into a JSON-compatible value (NaN handled by sanitize)."""
    if field is None:
        return None

    type_name = field.getTypeName()

    if type_name in SF_GETTERS:
        value = getattr(field, SF_GETTERS[type_name])()
        return list(value) if isinstance(value, (list, tuple)) else value

    if type_name == "SFNode":
        return brief_node(field.getSFNode())

    if type_name == "MFNode":
        nodes = [brief_node(field.getMFNode(i)) for i in range(field.getCount())]
        return {"count": len(nodes), "nodes": nodes}

    if type_name in MF_GETTERS:
        getter = MF_GETTERS[type_name]
        return [
            (lambda v: list(v) if isinstance(v, (list, tuple)) else v)(
                getattr(field, getter)(i)
            )
            for i in range(field.getCount())
        ]

    return {"unsupported_type": type_name}


def field_summary(node):
    result = {}
    try:
        count = node.getNumberOfFields()
    except Exception:
        return result

    for i in range(count):
        try:
            field = node.getFieldByIndex(i)
            if field is None:
                continue
            ftype = field.getTypeName()
            item = {"type": ftype}
            if ftype.startswith("MF"):
                item["count"] = field.getCount()
            result[field.getName()] = item
        except Exception:
            log.debug("Unable to inspect field %s", i, exc_info=True)

    return result


# ---------- writers ----------

def conv_bool(v, n):
    if not isinstance(v, bool):
        raise ValueError(f"{n} must be a boolean")
    return v


def conv_int(v, n):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{n} must be an integer")
    return v


def conv_float(v, n):
    return require_finite_number(v, n)


def conv_string(v, n):
    if not isinstance(v, str):
        raise ValueError(f"{n} must be a string")
    return v


def conv_vec(length):
    return lambda v, n: vector(v, length, n)


SF_SPECS = {
    "SFBool": ("setSFBool", conv_bool),
    "SFInt32": ("setSFInt32", conv_int),
    "SFFloat": ("setSFFloat", conv_float),
    "SFString": ("setSFString", conv_string),
    "SFVec2f": ("setSFVec2f", conv_vec(2)),
    "SFVec3f": ("setSFVec3f", conv_vec(3)),
    "SFRotation": ("setSFRotation", require_rotation),
    "SFColor": ("setSFColor", require_color),
}

# type -> (setter, inserter, element converter)
MF_SPECS = {
    "MFBool": ("setMFBool", "insertMFBool", conv_bool),
    "MFInt32": ("setMFInt32", "insertMFInt32", conv_int),
    "MFFloat": ("setMFFloat", "insertMFFloat", conv_float),
    "MFString": ("setMFString", "insertMFString", conv_string),
    "MFVec2f": ("setMFVec2f", "insertMFVec2f", conv_vec(2)),
    "MFVec3f": ("setMFVec3f", "insertMFVec3f", conv_vec(3)),
    "MFRotation": ("setMFRotation", "insertMFRotation", require_rotation),
    "MFColor": ("setMFColor", "insertMFColor", require_color),
}


def set_mf_field(field, ftype, value, name):
    """The Webots API has no setCount(): resize with removeMF / insertMF."""
    setter, inserter, conv = MF_SPECS[ftype]

    if not isinstance(value, list):
        raise ValueError(f"{name}: {ftype} value must be a list")

    items = [conv(v, f"{name}[{i}]") for i, v in enumerate(value)]
    set_fn = getattr(field, setter)
    insert_fn = getattr(field, inserter)

    count = field.getCount()

    while count > len(items):
        field.removeMF(count - 1)
        count -= 1

    for i, item in enumerate(items):
        if i < count:
            set_fn(i, item)
        else:
            insert_fn(-1, item)


def apply_field_value(field, value, name):
    ftype = field.getTypeName()

    if ftype in SF_SPECS:
        setter, conv = SF_SPECS[ftype]
        getattr(field, setter)(conv(value, name))
    elif ftype in MF_SPECS:
        set_mf_field(field, ftype, value, name)
    else:
        raise ValueError(
            f"Field type {ftype} is not supported by set_node_field; "
            "use import_into_field for SFNode/MFNode fields"
        )

    return ftype


# ============================================================
# VRML import helpers
# ============================================================

created_ids = set()  # root nodes created through this server

_VRML_HEAD = re.compile(
    r"^\s*(?:DEF\s+([A-Za-z_][A-Za-z0-9_]*)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\{"
)


def import_root_node(vrml):
    """Import ONE root node. Returns the list of newly added node states."""
    if not isinstance(vrml, str) or not vrml.strip():
        raise ValueError("vrml_string cannot be empty")

    if len(vrml) > MAX_VRML_LENGTH:
        raise ValueError(f"VRML exceeds {MAX_VRML_LENGTH} characters")

    text = re.sub(r"(?m)^\s*#.*$", "", vrml).strip()
    match = _VRML_HEAD.match(text)

    if match is None:
        raise ValueError(
            "VRML must be a single node such as 'DEF NAME Solid { ... }' "
            "(no header line, one root node per call)"
        )

    def_name, type_name = match.group(1), match.group(2)

    if type_name in SINGLETON_ROOT_TYPES \
            and find_root_node_by_type(type_name) is not None:
        raise ValueError(
            f"The world already has a {type_name}. Modify it with "
            f"set_node_field('type:{type_name}', ...) instead of adding another"
        )

    if def_name and supervisor.getFromDef(def_name) is not None:
        raise ValueError(
            f"DEF {def_name} already exists; choose another name or "
            "delete the old node first"
        )

    before = root_children.getCount()
    root_children.importMFNodeFromString(-1, text)
    after = root_children.getCount()

    if after <= before:
        raise RuntimeError(
            "No root node was added. Check the VRML syntax and the Webots "
            "console for import errors."
        )

    added = []
    for i in range(before, after):
        node = root_children.getMFNode(i)
        if node is not None:
            created_ids.add(node.getId())
            added.append(safe_node_state(node))

    return added


# ============================================================
# Scene traversal
# ============================================================

def iter_child_nodes(node):
    """Yield (field_name, child_node) for SFNode / MFNode fields."""
    try:
        count = node.getNumberOfFields()
    except Exception:
        return

    for i in range(count):
        try:
            field = node.getFieldByIndex(i)
            if field is None:
                continue

            ftype = field.getTypeName()

            if ftype == "SFNode":
                child = field.getSFNode()
                if child is not None:
                    yield field.getName(), child

            elif ftype == "MFNode":
                for j in range(field.getCount()):
                    child = field.getMFNode(j)
                    if child is not None:
                        yield field.getName(), child

        except Exception:
            log.debug("Failed to traverse field %s", i, exc_info=True)


def collect_scene_tree(max_depth=MAX_TREE_DEPTH, include_fields=False):
    nodes = []
    visited = set()
    seen_defs = set()
    duplicate_defs = []
    truncated = False
    cut_by_depth = 0
    cut_by_limit = False

    def visit(node, parent_id, field_name, depth):
        nonlocal truncated, cut_by_depth, cut_by_limit

        node_id = node.getId()

        if node_id in visited:
            return

        if len(nodes) >= MAX_TREE_NODES:
            truncated = cut_by_limit = True
            return
        if depth > max_depth:
            truncated = True
            cut_by_depth += 1
            return

        visited.add(node_id)

        def_name = node.getDef() or None
        if def_name:
            if def_name in seen_defs:
                duplicate_defs.append(def_name)
            seen_defs.add(def_name)

        entry = {
            **safe_node_state(node),
            "parent_id": parent_id,
            "parent_field": field_name,
            "depth": depth,
        }

        if include_fields:
            entry["fields"] = field_summary(node)

        nodes.append(entry)

        for fname, child in iter_child_nodes(node):
            visit(child, node_id, fname, depth + 1)

    visit(root_node, None, None, 0)

    result = {
        "status": "success",
        "node_count": len(nodes),
        "truncated": truncated,
        "max_depth": max_depth,
        "duplicate_defs": sorted(set(duplicate_defs)),
        "nodes": nodes,
    }
    if cut_by_depth:
        result["truncated_by_depth"] = cut_by_depth
        result["hint"] = (
            f"{cut_by_depth} node(s) deeper than max_depth={max_depth} were left out "
            "(joint chains such as link -> joint -> endPoint nest deeply). "
            f"Pass a larger max_depth (up to {MAX_TREE_DEPTH})."
        )
    if cut_by_limit:
        result["truncated_by_node_limit"] = MAX_TREE_NODES
    return result


def find_nodes_by_type(type_name):
    found = []
    visited = set()

    def visit(node):
        node_id = node.getId()
        if node_id in visited:
            return
        visited.add(node_id)

        if node.getTypeName() == type_name:
            found.append(safe_node_state(node))

        for _, child in iter_child_nodes(node):
            visit(child)

    visit(root_node)
    return found


def iter_all_nodes():
    visited = set()
    stack = [root_node]

    while stack:
        node = stack.pop()
        node_id = node.getId()

        if node_id in visited:
            continue

        visited.add(node_id)
        yield node

        for _, child in iter_child_nodes(node):
            stack.append(child)


