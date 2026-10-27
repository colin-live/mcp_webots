"""Scene building tools."""
import asyncio

from ..registry import tool

from ..config import PROTECTED_ROOT_TYPES
from ..runtime import call_main, fmt, fmt_vec, require_color, require_def_name, require_finite_number, require_rotation, root_children, vector
from ..scene import brief_node, created_ids, find_root_node_by_type, get_node, import_root_node, safe_node_state, set_mf_field
from ..snapshots import contact_tracking

# ------------------------------------------------------------
# [构建]
# ------------------------------------------------------------

@tool
async def create_primitive(
    def_name: str,
    primitive_type: str = "",
    translation: list[float] | None = None,
    size: list[float] | None = None,
    rotation: list[float] | None = None,
    color: list[float] | None = None,
    mass: float = 0.0,
    collision: bool = True,
    shape: str = "",
) -> str:
    """[构建] 创建 Box / Sphere / Cylinder / Capsule 的 Solid。
    必填：def_name（DEF 名）和 primitive_type（也接受别名 shape，不区分大小写）。

    size: Box=[x,y,z]；Sphere=[radius]；Cylinder/Capsule=[radius,height]
          （多余元素忽略，Cylinder/Capsule 的轴沿局部 y，需要竖直时用 rotation）。
    rotation: 轴角 [ax,ay,az,angle]。 color: RGB，0-1。
    mass: 0 表示静态碰撞体（如地面/墙：有 boundingObject，physics 为 null，这是正确的）；
          >0 表示动态物体（带 Physics 节点，会受重力运动）。
    collision: 是否生成 boundingObject；动态物体必须为 True。
    世界为 ENU（z 向上）。
    """

    def operation():
        kind = (primitive_type or shape or "").strip().capitalize()

        if kind not in ("Box", "Sphere", "Cylinder", "Capsule"):
            raise ValueError(
                "primitive_type (alias: shape) is required and must be "
                "Box, Sphere, Cylinder, or Capsule"
            )

        require_def_name(def_name)
        pos = vector(translation or [0.0, 0.0, 0.0], 3, "translation")
        dims = [require_finite_number(v, f"size[{i}]")
                for i, v in enumerate(size or [0.1, 0.1, 0.1])]
        rot = require_rotation(rotation or [0, 0, 1, 0], "rotation")
        rgb = require_color(color or [0.7, 0.7, 0.7], "color")
        mass_value = require_finite_number(mass, "mass")

        if mass_value < 0:
            raise ValueError("mass must be >= 0")
        if mass_value > 0 and not collision:
            raise ValueError("A dynamic body (mass > 0) needs collision=True")

        def need(count, what):
            if len(dims) < count or any(v <= 0 for v in dims[:count]):
                raise ValueError(f"{kind} needs {what} (all > 0)")

        if kind == "Box":
            need(3, "size=[x, y, z]")
            geometry = f"Box {{ size {fmt_vec(dims[:3])} }}"
        elif kind == "Sphere":
            need(1, "size=[radius]")
            geometry = f"Sphere {{ radius {fmt(dims[0])} }}"
        elif kind == "Cylinder":
            need(2, "size=[radius, height]")
            geometry = f"Cylinder {{ radius {fmt(dims[0])} height {fmt(dims[1])} }}"
        else:
            need(2, "size=[radius, height]")
            geometry = (
                f"Capsule {{ radius {fmt(dims[0])} height {fmt(dims[1])} "
                "subdivision 12 }"
            )

        lines = [
            f"translation {fmt_vec(pos)}",
            f"rotation {fmt_vec(rot)}",
            "children [ Shape { appearance PBRAppearance { "
            f"baseColor {fmt_vec(rgb)} roughness 0.6 metalness 0 }} "
            f"geometry {geometry} }} ]",
            f'name "{def_name}"',
        ]

        if collision:
            lines.append(f"boundingObject {geometry}")
        if mass_value > 0:
            lines.append(f"physics Physics {{ density -1 mass {fmt(mass_value)} }}")

        vrml = f"DEF {def_name} Solid {{\n  " + "\n  ".join(lines) + "\n}"
        added = import_root_node(vrml)

        return {
            "status": "success",
            "primitive_type": kind,
            "node": added[0] if added else None,
            "dynamic": mass_value > 0,
            "collision": bool(collision),
        }

    return await call_main("create_primitive", operation)


@tool
async def create_component(vrml_string: str) -> str:
    """[构建] 将一个 VRML 根节点导入场景，例如 'DEF BOX Solid { ... }'。

    一次一个根节点，不要带 '#VRML_SIM' 头；用 DEF 命名以便后续引用。
    Background / WorldInfo / Viewpoint 已存在时会拒绝，请改用 set_node_field。
    不能直接使用未声明的 PROTO（如 TexturedBackground）。
    """

    def operation():
        added = import_root_node(vrml_string)
        return {
            "status": "success",
            "added_count": len(added),
            "nodes": added,
            "message": "VRML imported.",
        }

    return await call_main("create_component", operation)


@tool
async def create_components(vrml_strings: list[str]) -> str:
    """[构建] 批量导入多个独立的 VRML 根节点。
    每个字符串必须包含一个完整根节点，可以包含任意数量的嵌套 DEF 节点。
    逐项处理，失败时继续，并返回每项的执行结果。
    """

    def operation():
        if not isinstance(vrml_strings, list) or not vrml_strings:
            raise ValueError("vrml_strings must be a non-empty list")

        if len(vrml_strings) > 50:
            raise ValueError("At most 50 nodes per call")

        results = []
        imported = []

        for i, vrml in enumerate(vrml_strings):
            try:
                nodes = import_root_node(vrml)
                imported.extend(nodes)

                results.append({
                    "index": i,
                    "status": "success",
                    "added_count": len(nodes),
                    "nodes": nodes,
                })

            except Exception as exc:
                results.append({
                    "index": i,
                    "status": "failed",
                    "error": str(exc),
                })

        failed_count = sum(
            1 for item in results
            if item["status"] == "failed"
        )

        return {
            "status": (
                "success" if failed_count == 0
                else "partial_success" if imported
                else "failed"
            ),
            "requested_count": len(vrml_strings),
            "added_count": len(imported),
            "failed_count": failed_count,
            "nodes": imported,
            "results": results,
        }

    return await call_main("create_components", operation)


@tool
async def add_light(
    def_name: str,
    light_type: str = "DirectionalLight",
    direction: list[float] | None = None,
    location: list[float] | None = None,
    intensity: float = 1.0,
    color: list[float] | None = None,
    cast_shadows: bool = True,
) -> str:
    """[构建] 添加光源。light_type: DirectionalLight（用 direction）、
    PointLight 或 SpotLight（用 location，SpotLight 还用 direction）。"""

    def operation():
        require_def_name(def_name)
        rgb = require_color(color or [1, 1, 1], "color")
        inten = require_finite_number(intensity, "intensity")
        shadows = "TRUE" if cast_shadows else "FALSE"

        common = (
            f"color {fmt_vec(rgb)} intensity {fmt(inten)} castShadows {shadows}"
        )

        if light_type == "DirectionalLight":
            d = vector(direction or [-0.4, -0.8, -0.4], 3, "direction")
            body = f"direction {fmt_vec(d)} {common}"
        elif light_type == "PointLight":
            loc = vector(location or [0, 0, 3], 3, "location")
            body = f"location {fmt_vec(loc)} {common}"
        elif light_type == "SpotLight":
            loc = vector(location or [0, 0, 3], 3, "location")
            d = vector(direction or [0, 0, -1], 3, "direction")
            body = f"location {fmt_vec(loc)} direction {fmt_vec(d)} {common}"
        else:
            raise ValueError(
                "light_type must be DirectionalLight, PointLight or SpotLight"
            )

        added = import_root_node(f"DEF {def_name} {light_type} {{ {body} }}")
        return {"status": "success", "node": added[0] if added else None}

    return await call_main("add_light", operation)


@tool
async def set_world_appearance(
    sky_color: list[float] | None = None,
    luminosity: float | None = None,
) -> str:
    """[构建] 设置背景：修改已有的根 Background 节点；没有则创建一个。
    sky_color: RGB（0-1）；luminosity: 背景亮度（>= 0）。"""

    def operation():
        rgb = require_color(sky_color, "sky_color") if sky_color is not None else None
        lum = None

        if luminosity is not None:
            lum = require_finite_number(luminosity, "luminosity")
            if lum < 0:
                raise ValueError("luminosity must be >= 0")

        node = find_root_node_by_type("Background")

        if node is None:
            fields = []
            if rgb is not None:
                fields.append(f"skyColor [ {fmt_vec(rgb)} ]")
            if lum is not None:
                fields.append(f"luminosity {fmt(lum)}")
            added = import_root_node(
                "DEF WORLD_BACKGROUND Background { " + " ".join(fields) + " }"
            )
            return {"status": "success", "created": True, "node": added[0]}

        changed = []

        if rgb is not None:
            set_mf_field(node.getField("skyColor"), "MFColor", [rgb], "skyColor")
            changed.append("skyColor")

        if lum is not None:
            node.getField("luminosity").setSFFloat(lum)
            changed.append("luminosity")

        return {
            "status": "success",
            "created": False,
            "changed_fields": changed,
            "node": brief_node(node),
        }

    return await call_main("set_world_appearance", operation)


@tool
async def set_viewpoint(
    position: list[float] | None = None,
    orientation: list[float] | None = None,
) -> str:
    """[构建] 设置 3D 视角（根 Viewpoint 节点）。orientation 为轴角 [ax,ay,az,angle]。
    不确定姿态时，可先在 GUI 中调好视角，再用 get_node_details('type:Viewpoint') 读取。"""

    def operation():
        node = get_node("type:Viewpoint")
        changed = []

        if position is not None:
            node.getField("position").setSFVec3f(vector(position, 3, "position"))
            changed.append("position")

        if orientation is not None:
            node.getField("orientation").setSFRotation(
                require_rotation(orientation, "orientation")
            )
            changed.append("orientation")

        return {"status": "success", "changed_fields": changed}

    return await call_main("set_viewpoint", operation)


@tool
async def delete_node(def_name: str) -> str:
    """[构建] 按 DEF 名称删除节点。WorldInfo 和 Viewpoint 受保护，不可删除。"""

    def operation():
        node = get_node(def_name)

        if node.getTypeName() in PROTECTED_ROOT_TYPES:
            raise ValueError(f"{node.getTypeName()} cannot be deleted")

        before = safe_node_state(node)
        created_ids.discard(node.getId())
        node.remove()

        return {
            "status": "success",
            "removed_node": before,
            "message": "Node removal was requested.",
        }

    return await call_main("delete_node", operation)


@tool
async def clear_created_nodes() -> str:
    """[构建] 删除本服务器创建的所有根节点（create_* / add_light 等），
    保留原始世界内容。用于从干净场景重新开始。"""

    def operation():
        targets = []

        for i in range(root_children.getCount()):
            node = root_children.getMFNode(i)
            if node is not None and node.getId() in created_ids:
                targets.append(node)

        removed = [brief_node(n) for n in targets]

        for node in targets:
            node.remove()

        created_ids.clear()
        contact_tracking.clear()

        return {"status": "success", "removed_count": len(removed), "removed": removed}

    return await call_main("clear_created_nodes", operation)
