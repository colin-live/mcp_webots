"""Node modification tools."""
import asyncio

from ..registry import tool

import re
from ..config import MAX_VRML_LENGTH
from ..runtime import call_main, fmt, require_finite_number, require_int_range, require_rotation, vector
from ..scene import (
    apply_field_value,
    field_value,
    get_joint_position,
    get_node,
    has_physics_node,
    set_joint_position as set_joint_position_value,
)
from typing import Any

# ------------------------------------------------------------
# [修改]
# ------------------------------------------------------------

@tool
async def set_node_field(def_name: str, field_name: str, value: Any) -> str:
    """[修改] 修改节点字段并回读。def_name 可用 'type:WorldInfo' 等。

    支持 SFBool/SFInt32/SFFloat/SFString/SFVec2f/SFVec3f/SFRotation/SFColor 以及
    对应的 MF 类型（MF 传列表，例如 MFColor: [[1,1,1],[0.5,0.7,1]]）。
    SFNode/MFNode 请用 import_into_field。
    """

    def operation():
        if not isinstance(field_name, str) or not field_name:
            raise ValueError("field_name cannot be empty")

        node = get_node(def_name)
        field = node.getField(field_name)

        if field is None:
            raise ValueError(f"Field not found: {field_name} on {def_name}")

        ftype = apply_field_value(field, value, field_name)

        return {
            "status": "success",
            "def": def_name,
            "field": field_name,
            "type": ftype,
            "value": field_value(field),
            "message": "Field updated and read back.",
        }

    return await call_main("set_node_field", operation)


@tool
async def set_node_pose(
    def_name: str,
    translation: list[float] | None = None,
    rotation: list[float] | None = None,
    reset_physics: bool = True,
) -> str:
    """[修改] 设置节点的 translation / rotation（相对父节点）。
    注意：直接写 translation 不会清零速度，高速物体会继续飞行甚至穿透地面；
    reset_physics=True（默认）时本工具先重置物理、再写位姿、再把速度清零。
    新位姿在下一个仿真步后体现在 get_node_state。"""

    def operation():
        node = get_node(def_name)
        changed = []
        dynamic = has_physics_node(node)

        # Order matters: reset physics BEFORE writing the pose. Writing the
        # pose keeps the old velocity, and resetting afterwards did not bring
        # a fallen body back in testing.
        if reset_physics and dynamic and (
            translation is not None or rotation is not None
        ):
            node.resetPhysics()

        if translation is not None:
            f = node.getField("translation")
            if f is None:
                raise ValueError(f"{def_name} has no translation field")
            f.setSFVec3f(vector(translation, 3, "translation"))
            changed.append("translation")

        if rotation is not None:
            f = node.getField("rotation")
            if f is None:
                raise ValueError(f"{def_name} has no rotation field")
            f.setSFRotation(require_rotation(rotation, "rotation"))
            changed.append("rotation")

        if reset_physics and dynamic and changed:
            node.setVelocity([0.0] * 6)

        return {"status": "success", "def": def_name, "changed_fields": changed,
                "physics_reset": bool(reset_physics and dynamic and changed)}

    return await call_main("set_node_pose", operation)


def _scan_vrml_roots(text: str):
    """Scan a VRML fragment; return ``(root_node_type_names, syntax_error_or_None)``.

    Only checks structure (balanced braces/brackets, every root `{` preceded by a
    type name); it is not a full VRML parser. Strings and `#` comments are skipped.
    """
    roots: list[str] = []
    tokens: list[str] = []
    stack: list[str] = []
    pairs = {"}": "{", "]": "["}
    pending = ""
    tok = ""
    i, n = 0, len(text)

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    while i < n:
        c = text[i]
        if c == "#":
            if tok and not stack:
                tokens.append(tok)
            tok = ""
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == '"':
            if tok and not stack:
                tokens.append(tok)
            tok = ""
            start = i
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                i += 1
            if i >= n:
                return roots, f"unterminated string starting on line {line_of(start)}"
            i += 1
            continue
        if c in "{[]}":
            if tok and not stack:
                tokens.append(tok)
            tok = ""
            if c == "{":
                if not stack:
                    if not tokens:
                        return roots, f"line {line_of(i)}: '{{' without a node type name"
                    pending = tokens[-1]
                    tokens.clear()
                stack.append(c)
            elif c == "[":
                stack.append(c)
            else:
                if not stack or stack[-1] != pairs[c]:
                    return roots, f"line {line_of(i)}: unmatched '{c}'"
                stack.pop()
                if c == "}" and not stack:
                    roots.append(pending)
            i += 1
            continue
        if c.isspace() or c == ",":
            if tok and not stack:
                tokens.append(tok)
            tok = ""
            i += 1
            continue
        tok += c
        i += 1

    if tok and not stack:
        tokens.append(tok)
    if stack:
        return roots, f"unclosed '{stack[-1]}'"
    return roots, None


@tool
async def import_into_field(def_name: str, field_name: str, vrml_string: str) -> str:
    """[修改] 向已有节点的 SFNode / MFNode 字段导入 VRML 子节点，
    例如给 Solid 添加 physics（SFNode，会替换原值）或追加 children / boundingObject。

    导入失败（语法错误、Webots 拒绝）时返回 error，不再误报 success。
    注意 HingeJoint 的子刚体字段是 `endPoint Solid { ... }`（不是 start/end），
    且 endPoint 的值必须写节点类型名。成功时返回 imported_types 供核对。"""

    def operation():
        if not isinstance(vrml_string, str) or not vrml_string.strip():
            raise ValueError("vrml_string cannot be empty")
        if len(vrml_string) > MAX_VRML_LENGTH:
            raise ValueError(f"VRML exceeds {MAX_VRML_LENGTH} characters")

        node = get_node(def_name)
        field = node.getField(field_name)

        if field is None:
            raise ValueError(f"Field not found: {field_name} on {def_name}")

        ftype = field.getTypeName()
        if ftype not in ("SFNode", "MFNode"):
            raise ValueError(f"{field_name} is {ftype}, not SFNode/MFNode")

        # Webots reports VRML parse errors only on its own console and the
        # import call itself never raises, so validate before and verify after.
        roots, syntax_error = _scan_vrml_roots(vrml_string)
        if syntax_error:
            raise ValueError(
                f"VRML syntax error, nothing was imported: {syntax_error}. "
                "Every field value that is a node needs its type name, e.g. "
                "`endPoint Solid { ... }` (HingeJoint has no start/end fields)."
            )
        if not roots:
            raise ValueError("No root node found in vrml_string; expected e.g. `Shape { ... }`")

        if ftype == "SFNode":
            if len(roots) != 1:
                raise ValueError(f"SFNode field {field_name} takes exactly 1 root node, got {len(roots)}")
            before_node = field.getSFNode()
            before_id = before_node.getId() if before_node is not None else None
            field.importSFNodeFromString(vrml_string)
            after_node = field.getSFNode()
            after_id = after_node.getId() if after_node is not None else None
            if after_id is None or after_id == before_id:
                raise ValueError(
                    f"Import into {def_name}.{field_name} had no effect: Webots rejected the VRML "
                    "(see the Webots console for the parser error)."
                )
            imported = [after_node.getTypeName()]
        else:
            before = field.getCount()
            field.importMFNodeFromString(-1, vrml_string)
            added = field.getCount() - before
            if added < len(roots):
                raise ValueError(
                    f"Import into {def_name}.{field_name} incomplete: expected {len(roots)} root node(s) "
                    f"{roots}, but only {added} were added (count {before} -> {before + added}). "
                    "Webots rejected the VRML; see the Webots console for the parser error."
                )
            imported = [field.getMFNode(before + i).getTypeName() for i in range(added)]

        result = {
            "status": "success",
            "def": def_name,
            "field": field_name,
            "imported_types": imported,
            "value": field_value(field),
        }
        mismatched = [(want, got) for want, got in zip(roots, imported) if want != got]
        if mismatched:
            result["warnings"] = [f"requested {want} but the scene contains {got}" for want, got in mismatched]
        return result

    return await call_main("import_into_field", operation)


@tool
async def set_world_physics(
    gravity: float | None = None,
    erp: float | None = None,
    cfm: float | None = None,
) -> str:
    """[修改] 修改 WorldInfo 的物理参数。gravity 为重力加速度大小（默认约 9.81）。"""

    def operation():
        info = get_node("type:WorldInfo")
        changed = []

        if gravity is not None:
            g = require_finite_number(gravity, "gravity")
            field = info.getField("gravity")
            if field is None:
                raise ValueError("WorldInfo has no gravity field")
            if field.getTypeName() == "SFVec3f":
                field.setSFVec3f([0.0, 0.0, -abs(g)])  # ENU: -z
            else:
                field.setSFFloat(g)
            changed.append("gravity")

        for name, value in (("ERP", erp), ("CFM", cfm)):
            if value is not None:
                v = require_finite_number(value, name)
                if v < 0:
                    raise ValueError(f"{name} must be >= 0")
                info.getField(name).setSFFloat(v)
                changed.append(name)

        return {"status": "success", "changed_fields": changed}

    return await call_main("set_world_physics", operation)


@tool
async def set_contact_properties(
    material1: str = "default",
    material2: str = "default",
    coulomb_friction: float = 1.0,
    bounce: float = 0.0,
    bounce_velocity: float = 0.01,
) -> str:
    """[修改] 设置两种材质之间的接触属性（摩擦、弹性），写入 WorldInfo.contactProperties。
    材质名对应 Solid.contactMaterial（默认 "default"）；同一对材质会替换旧设置。
    bounce 为 0-1 的恢复系数，需要碰撞速度大于 bounce_velocity 才会反弹。"""

    def operation():
        for m in (material1, material2):
            if not isinstance(m, str) or not re.fullmatch(r"[A-Za-z0-9_ .-]+", m):
                raise ValueError("material names may only use letters, digits, _ . - and space")

        friction = require_finite_number(coulomb_friction, "coulomb_friction")
        bounce_value = require_finite_number(bounce, "bounce")
        bounce_vel = require_finite_number(bounce_velocity, "bounce_velocity")

        if friction < 0 or not 0.0 <= bounce_value <= 1.0 or bounce_vel < 0:
            raise ValueError(
                "coulomb_friction >= 0, 0 <= bounce <= 1, bounce_velocity >= 0"
            )

        field = get_node("type:WorldInfo").getField("contactProperties")
        if field is None:
            raise ValueError("WorldInfo has no contactProperties field")

        wanted = {material1, material2}

        for i in reversed(range(field.getCount())):
            existing = field.getMFNode(i)
            if existing is None:
                continue
            m1 = existing.getField("material1").getSFString()
            m2 = existing.getField("material2").getSFString()
            if {m1, m2} == wanted:
                field.removeMF(i)

        field.importMFNodeFromString(
            -1,
            f'ContactProperties {{ material1 "{material1}" material2 "{material2}" '
            f"coulombFriction [ {fmt(friction)} ] bounce {fmt(bounce_value)} "
            f"bounceVelocity {fmt(bounce_vel)} }}",
        )

        return {
            "status": "success",
            "contact_properties_count": field.getCount(),
            "pair": [material1, material2],
        }

    return await call_main("set_contact_properties", operation)


@tool
async def set_joint_position(def_name: str, position: float, axis: int = 1) -> str:
    """[修改] 直接设置关节位置（HingeJoint / Hinge2Joint / SliderJoint，单位 rad 或 m），不经过电机，
    等价于把 jointParameters.position 写成目标值；没有 jointParameters 时会自动创建。
    def_name 可以是 PROTO 内部关节的点路径，如 "ARM2DOF.JOINT1"。axis: 1 或 2（Hinge2Joint 的第二根轴）。
    这是“摆姿态/复位”工具，不是电机控制；需要电机跟踪目标请在机器人自己的控制器里调用 Motor.setPosition。"""

    def operation():
        require_int_range(axis, "axis", 1, 2)
        value = require_finite_number(position, "position")
        node = get_node(def_name)
        before = get_joint_position(node, axis)
        set_joint_position_value(node, value, axis)
        return {
            "status": "success",
            "def": def_name,
            "type": node.getTypeName(),
            "axis": axis,
            "previous_position": before,
            "position": value,
        }

    return await call_main("set_joint_position", operation)
