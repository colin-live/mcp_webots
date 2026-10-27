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

    def visit(node, parent_id, field_name, depth):
        nonlocal truncated

        node_id = node.getId()

        if node_id in visited:
            return

        if len(nodes) >= MAX_TREE_NODES or depth > max_depth:
            truncated = True
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

    return {
        "status": "success",
        "node_count": len(nodes),
        "truncated": truncated,
        "max_depth": max_depth,
        "duplicate_defs": sorted(set(duplicate_defs)),
        "nodes": nodes,
    }


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


