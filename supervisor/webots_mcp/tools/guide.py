"""Workflow guide tool."""
import asyncio

from ..registry import tool

from ..config import SERVER_VERSION
from ..registry import TOOL_NAMES
from ..runtime import result_json

# Tools that live outside this controller (see host/src/sim.rs and project_manager.py).
HOST_TOOLS = [
    "sim_start", "sim_stop", "sim_restart", "sim_pause", "sim_resume",
    "sim_step", "sim_reset", "sim_status",
]
PROJECT_TOOLS = [
    "project_list", "project_create", "project_info", "project_check",
    "project_delete", "project_read_file", "project_write_file",
    "world_add_robot", "world_remove_node", "supervisor_bind",
]

# ------------------------------------------------------------
# [指南]
# ------------------------------------------------------------

@tool
async def get_workflow_guide() -> str:
    """[指南] 返回工具分类和推荐的 建模→实验→分析→修正 工作流。新会话先调用它。"""
    return result_json({
        "status": "success",
        "server_version": SERVER_VERSION,
        "registered_tools": TOOL_NAMES,
        "host_tools": HOST_TOOLS,
        "project_tools": PROJECT_TOOLS,
        "tool_sources": (
            "registered_tools: 本控制器（Supervisor）提供，仅在 Webots 运行且 supervisor 已连接时可用；"
            "host_tools: Rust 宿主提供的时钟/生命周期工具，随时可用（新会话先 sim_start）；"
            "project_tools: 项目管理进程提供，Webots 未启动也可用。"
        ),
        "categories": {
            "时钟（宿主）": HOST_TOOLS,
            "项目（project manager）": PROJECT_TOOLS,
            "查询": [
                "get_scene_tree", "get_node_details", "get_node_state",
                "get_nodes_by_type", "get_contact_points", "validate_scene",
                "check_physics_health",
            ],
            "构建": [
                "create_primitive", "create_component", "create_components",
                "add_light", "set_world_appearance", "set_viewpoint",
                "delete_node", "clear_created_nodes",
            ],
            "修改": [
                "set_node_field", "set_node_pose", "import_into_field",
                "set_world_physics", "set_contact_properties", "set_joint_position",
            ],
            "物理": [
                "set_node_velocity", "apply_force", "apply_torque",
                "reset_node_physics",
            ],
            "实验": [
                "get_simulation_info",
                "run_simulation", "start_simulation",
                "get_simulation_status", "get_simulation_result",
                "list_simulations", "reset_simulation",
            ],
            "复位": ["save_snapshot", "restore_snapshot", "list_snapshots"],
            "输出": ["capture_screenshot", "save_world"],
        },
        "workflow": [
            "1. get_scene_tree 了解当前场景（根节点无 DEF 时用 'type:WorldInfo' 等引用）",
            "2. 用 create_primitive / create_component / add_light 搭建场景，"
            "给每个要观测的节点起 DEF 名；需要碰撞和动力学时必须有 boundingObject，"
            "动态物体还需要 Physics",
            "3. save_snapshot 记录初始状态",
            "4. run_simulation(duration, target_defs) 运行实验并拿到 summary "
            "（位移、最大速度、是否静止、静止用时）",
            "5. 必要时 get_simulation_result 取轨迹帧，capture_screenshot 看画面，"
            "get_contact_points 看接触",
            "6. 用 set_node_field / set_node_pose / set_contact_properties / "
            "set_world_physics 修正，restore_snapshot 回到初始状态后再次 run_simulation",
            "7. 满意后 save_world 保存",
        ],
        "notes": [
            "世界暂停时场景/物体/查询类工具照常可用（supervisor 暂停期间用 step(0) 处理命令），"
            "但仿真时间不前进，run_simulation / start_simulation 需要仿真运行（宿主默认会自动恢复）",
            "仿真的启动/暂停/继续/单步/重置/重启由宿主工具负责：sim_start、sim_pause、sim_resume、"
            "sim_step、sim_reset、sim_restart、sim_stop、sim_status",
            "sim_reset(kind='simulation') 恢复为已保存的世界文件：运行时创建且没有 save_world 过的节点会被删除，已写进 .wbt 的节点仍在",
            "Solid 默认没有碰撞和物理：缺少 boundingObject 不会碰撞，缺少 Physics 不会运动",
            "静态地面/墙 = 有 boundingObject、physics 为 null（这是正确的）；"
            "动态物体 = 有 boundingObject + Physics",
            "直接写 translation 会保留原速度：高速物体一步会飞过几十米而穿透地面，"
            "请用 set_node_pose（先重置物理、再写位姿、再清零速度）",
            "地面请用足够厚的 Box（>= 0.2 m）；实验结果异常先调用 check_physics_health",
            "apply_force/apply_torque 只作用一个物理步；持续推动请用 set_node_velocity",
            "Webots 世界重载会重启本控制器：场景中未保存的改动、快照和任务都会丢失",
        ],
    })


