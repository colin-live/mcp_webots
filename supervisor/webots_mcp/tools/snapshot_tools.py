"""Snapshot tools."""
import asyncio

from ..registry import tool

from ..runtime import call_main, require_def_name, require_identifier, supervisor
from ..scene import get_node, has_physics_node
from ..snapshots import default_snapshot_defs, load_all_snapshots, load_snapshot_file, persist_snapshot, snapshots

# ------------------------------------------------------------
# [复位]
# ------------------------------------------------------------

def _valid_name(name):
    try:
        require_identifier(name, "snapshot name")
        return True
    except ValueError:
        return False


@tool
async def save_snapshot(name: str, def_names: list[str] | None = None, persist: bool = True) -> str:
    """[复位] 记录节点的 translation/rotation 作为快照，用于重复实验。
    def_names 省略时记录所有带 DEF 且有位姿字段的根节点。
    persist=true（默认）时同时写入 <SAVE_DIR>/snapshots/<name>.json：世界重载、sim_restart 甚至新会话后
    list_snapshots / restore_snapshot 仍能用它作为实验基准（同名文件会被覆盖）。"""

    def operation():
        require_identifier(name, "snapshot name")

        names = (
            [require_def_name(n) for n in def_names]
            if def_names else default_snapshot_defs()
        )

        if not names:
            raise ValueError("No nodes to snapshot; pass def_names")
        if len(names) > 200:
            raise ValueError("At most 200 nodes per snapshot")

        data = {}
        for def_name in names:
            node = get_node(def_name)
            t = node.getField("translation")
            r = node.getField("rotation")
            if t is None or r is None:
                raise ValueError(f"{def_name} has no translation/rotation fields")
            data[def_name] = {
                "translation": list(t.getSFVec3f()),
                "rotation": list(r.getSFRotation()),
            }

        snapshots[name] = data
        result = {"status": "success", "name": name, "node_count": len(data),
                  "nodes": sorted(data)}
        if persist:
            result["persisted_to"] = str(persist_snapshot(name, data))
        return result

    return await call_main("save_snapshot", operation)


@tool
async def restore_snapshot(name: str) -> str:
    """[复位] 把快照中的节点恢复到记录的位姿并重置其物理状态（速度清零）。
    快照之后新建的节点不受影响（可用 delete_node / clear_created_nodes 清理）。"""

    def operation():
        data = snapshots.get(name)
        if data is None:
            data = load_snapshot_file(name) if _valid_name(name) else None
        if data is None:
            raise ValueError(
                f"Snapshot not found: {name}. Existing: {sorted(snapshots)}"
            )

        restored, missing = [], []

        for def_name, saved in data.items():
            node = supervisor.getFromDef(def_name)
            if node is None:
                missing.append(def_name)
                continue
            dynamic = has_physics_node(node)
            if dynamic:
                node.resetPhysics()  # reset BEFORE writing the pose
            node.getField("translation").setSFVec3f(saved["translation"])
            node.getField("rotation").setSFRotation(saved["rotation"])
            if dynamic:
                node.setVelocity([0.0] * 6)
            restored.append(def_name)

        return {"status": "success", "name": name, "restored": restored,
                "missing": missing}

    return await call_main("restore_snapshot", operation)


@tool
async def list_snapshots() -> str:
    """[复位] 列出已保存的快照及其包含的节点（内存 + <SAVE_DIR>/snapshots/*.json）。"""

    def operation():
        load_all_snapshots()
        return {
            "status": "success",
            "snapshots": {k: sorted(v) for k, v in snapshots.items()},
        }

    return await call_main("list_snapshots", operation)


