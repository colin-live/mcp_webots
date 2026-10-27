"""Scene query tools."""
import asyncio

from ..registry import tool

from ..config import MAX_TREE_DEPTH
from ..physics import physics_health
from ..runtime import TIME_STEP, call_main, require_finite_number, require_identifier, require_int_range, tool_error
from ..scene import collect_scene_tree, field_value, find_nodes_by_type, get_node, physics_role, safe_node_state
from ..snapshots import contact_tracking

# ------------------------------------------------------------
# [查询]
# ------------------------------------------------------------

@tool
async def get_scene_tree(max_depth: int = 4, include_fields: bool = False) -> str:
    """[查询] 递归查询场景树：节点类型、DEF、父子关系、位姿。

    max_depth: 递归深度（0-20），默认 4，Robot 子树很大时调小。
    include_fields: 为 True 时附带每个节点的字段名和类型。
    """
    try:
        depth = require_int_range(max_depth, "max_depth", 0, MAX_TREE_DEPTH)
        return await call_main(
            "get_scene_tree",
            lambda: collect_scene_tree(depth, bool(include_fields)),
        )
    except Exception as exc:
        return tool_error("get_scene_tree", exc)


@tool
async def get_node_details(def_name: str) -> str:
    """[查询] 按 DEF 名称（或 'type:WorldInfo' 等）查询节点状态和全部字段值。"""

    def operation():
        node = get_node(def_name)
        fields = {}

        for i in range(node.getNumberOfFields()):
            field = node.getFieldByIndex(i)
            if field is None:
                continue
            try:
                fields[field.getName()] = {
                    "type": field.getTypeName(),
                    "value": field_value(field),
                }
            except Exception as exc:
                fields[field.getName()] = {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }

        return {
            "status": "success",
            "node": {**safe_node_state(node), "physics_role": physics_role(node)},
            "fields": fields,
        }

    return await call_main("get_node_details", operation)


@tool
async def get_node_state(def_name: str) -> str:
    """[查询] 查询节点当前位置、姿态（旋转矩阵）和速度（有 Physics 时）。"""
    return await call_main(
        "get_node_state",
        lambda: {"status": "success", "state": safe_node_state(get_node(def_name))},
    )


@tool
async def get_nodes_by_type(type_name: str) -> str:
    """[查询] 按 Webots 节点类型查找所有节点，不要求有 DEF 名称。"""

    def operation():
        name = require_identifier(type_name.strip(), "type_name")
        nodes = find_nodes_by_type(name)
        return {"status": "success", "type": name, "count": len(nodes), "nodes": nodes}

    return await call_main("get_nodes_by_type", operation)


@tool
async def get_contact_points(def_name: str, include_descendants: bool = False) -> str:
    """[查询] 查询节点当前的接触点（世界坐标）及对方节点 id。

    首次调用会开启该节点的接触点跟踪，结果可能为空；
    仿真前进一步后再调用即可。节点需要 boundingObject 和 Physics。
    """

    def operation():
        node = get_node(def_name)
        first_time = node.getId() not in contact_tracking

        if first_time:
            node.enableContactPointsTracking(TIME_STEP, bool(include_descendants))
            contact_tracking.add(node.getId())

        points = [
            {"point": list(p.point), "node_id": p.node_id}
            for p in node.getContactPoints(bool(include_descendants))
        ]

        return {
            "status": "success",
            "def": def_name,
            "contact_count": len(points),
            "contacts": points,
            "note": (
                "Contact tracking was just enabled; call again after the "
                "next step." if first_time else None
            ),
        }

    return await call_main("get_contact_points", operation)


@tool
async def check_physics_health(
    max_abs_position: float = 1000.0,
    max_speed: float = 50.0,
) -> str:
    """[查询] 物理健康检查：把每个 Solid 分类为 dynamic / static_collider / no_collision，
    并标出异常：动态体没有 boundingObject、没有任何静态碰撞体、跑出世界很远的失控物体、
    速度过高（可能穿透薄地面）。实验结果异常（物体一直下落、穿地）时先调用它。
    注意 static_collider 的 physics 为 null 是正确配置。"""

    def operation():
        return physics_health(
            require_finite_number(max_abs_position, "max_abs_position"),
            require_finite_number(max_speed, "max_speed"),
        )

    return await call_main("check_physics_health", operation)


@tool
async def validate_scene() -> str:
    """[查询] 结构检查：场景树可遍历性、节点数量、重复 DEF、截断。不验证物理行为。"""

    def operation():
        tree = collect_scene_tree()
        return {
            "status": "success",
            "node_count": tree["node_count"],
            "truncated": tree["truncated"],
            "duplicate_defs": tree["duplicate_defs"],
            "checks": {
                "duplicate_defs": "warning" if tree["duplicate_defs"] else "ok",
                "tree_truncated": "warning" if tree["truncated"] else "ok",
            },
            "note": (
                "Structural inspection only. It does not prove that geometry, "
                "lighting or physical behavior are valid."
            ),
        }

    return await call_main("validate_scene", operation)


