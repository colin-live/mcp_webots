"""Physics control tools."""
import asyncio

from ..registry import tool

from ..runtime import call_main, is_finite_seq, vector
from ..scene import get_node, has_physics_node

# ------------------------------------------------------------
# [物理]
# ------------------------------------------------------------

def physics_node(def_name):
    node = get_node(def_name)
    if not has_physics_node(node):
        raise ValueError(
            f"{def_name} has no Physics node; create it with mass > 0 "
            "or add one via import_into_field"
        )
    return node


@tool
async def set_node_velocity(
    def_name: str,
    linear: list[float] | None = None,
    angular: list[float] | None = None,
) -> str:
    """[物理] 直接设置 Solid 的线速度 (m/s) 和角速度 (rad/s)，世界坐标。
    省略的一项保持当前值。需要 Physics 节点。"""

    def operation():
        node = physics_node(def_name)
        current = list(node.getVelocity())

        lin = vector(linear, 3, "linear") if linear is not None else current[:3]
        ang = vector(angular, 3, "angular") if angular is not None else current[3:]

        if not is_finite_seq(lin + ang, 6):
            raise ValueError("Current velocity is unavailable; pass both linear and angular")

        node.setVelocity(lin + ang)
        return {"status": "success", "def": def_name, "linear": lin, "angular": ang}

    return await call_main("set_node_velocity", operation)


@tool
async def apply_force(
    def_name: str,
    force: list[float],
    relative: bool = False,
    offset: list[float] | None = None,
) -> str:
    """[物理] 对 Solid 施加力 (N)，只作用一个物理步（相当于冲量 F·dt）。
    relative=True 时力在节点局部坐标系；offset 为作用点相对节点原点的位置。
    持续推动请用 set_node_velocity。"""

    def operation():
        node = physics_node(def_name)
        f = vector(force, 3, "force")

        if offset is not None:
            node.addForceWithOffset(f, vector(offset, 3, "offset"), bool(relative))
        else:
            node.addForce(f, bool(relative))

        return {"status": "success", "def": def_name, "force": f,
                "note": "Applied for one basic time step only."}

    return await call_main("apply_force", operation)


@tool
async def apply_torque(def_name: str, torque: list[float], relative: bool = False) -> str:
    """[物理] 对 Solid 施加力矩 (N·m)，只作用一个物理步。"""

    def operation():
        node = physics_node(def_name)
        t = vector(torque, 3, "torque")
        node.addTorque(t, bool(relative))
        return {"status": "success", "def": def_name, "torque": t,
                "note": "Applied for one basic time step only."}

    return await call_main("apply_torque", operation)


@tool
async def reset_node_physics(def_name: str) -> str:
    """[物理] 重置节点的物理状态（速度、受力清零）。"""

    def operation():
        get_node(def_name).resetPhysics()
        return {"status": "success",
                "message": f"Physics reset requested for {def_name}"}

    return await call_main("reset_node_physics", operation)


