"""Simulation experiment tools."""
import asyncio

from ..registry import tool

import time
from ..config import SERVER_VERSION
from ..runtime import TIME_STEP, call_main, command_queue, require_finite_number, require_int_range, result_json, root_children, submit_command, supervisor, tool_error
from ..simulation import finish_task, get_task, resolve_task, simulation_result_data, simulation_status_data, simulation_tasks, start_task
from .. import simulation
from ..snapshots import snapshots
from controller import Supervisor

# ------------------------------------------------------------
# [实验]
# ------------------------------------------------------------

@tool
async def get_simulation_info() -> str:
    """[实验] 返回仿真时间、基础时间步、运行模式、当前观测任务和待处理命令数。"""

    def operation():
        mode = supervisor.simulationGetMode()
        names = {}

        for label in ("PAUSE", "REAL_TIME", "FAST", "RUN"):
            value = getattr(Supervisor, f"SIMULATION_MODE_{label}", None)
            if value is not None:
                names[value] = label.lower()

        return {
            "status": "success",
            "server_version": SERVER_VERSION,
            "simulation_time": supervisor.getTime(),
            "basic_time_step_ms": TIME_STEP,
            "mode": names.get(mode, mode),
            "active_simulation_id": simulation.active_simulation_id,
            "pending_commands": command_queue.qsize(),
            "root_node_count": root_children.getCount(),
            "snapshots": sorted(snapshots),
        }

    return await call_main("get_simulation_info", operation)


@tool
async def set_simulation_speed(mode: str) -> str:
    """[实验] 设置仿真速度：'real_time'（实时，画面完整，适合截图）或
    'fast'（尽可能快，适合长时间实验）。不提供 pause：暂停会阻塞本控制器。"""

    def operation():
        if mode == "real_time":
            value = getattr(
                Supervisor, "SIMULATION_MODE_REAL_TIME",
                getattr(Supervisor, "SIMULATION_MODE_RUN", None),
            )
        elif mode == "fast":
            value = getattr(Supervisor, "SIMULATION_MODE_FAST", None)
        else:
            raise ValueError("mode must be 'real_time' or 'fast'")

        if value is None:
            raise RuntimeError(f"Mode {mode} is not supported by this Webots version")

        supervisor.simulationSetMode(value)
        return {"status": "success", "mode": mode}

    return await call_main("set_simulation_speed", operation)


@tool
async def start_simulation(
    duration: float,
    target_defs: list[str],
    sample_interval: float = 0.05,
    stop_when_settled: bool = False,
    settle_speed: float = 0.01,
    settle_time: float = 0.5,
    include_orientation: bool = False,
    fall_z: float | None = -100.0,
) -> str:
    """[实验] 异步启动一次观测并立即返回 simulation_id；之后用
    get_simulation_status / get_simulation_result 查询。需要一次调用完成时用 run_simulation。

    duration: 观测时长（仿真秒）。 target_defs: 要跟踪的节点 DEF 列表（Solid/Robot/Pose）。
    sample_interval: 采样间隔（仿真秒，不小于基础时间步）。
    stop_when_settled: 所有目标速度低于 settle_speed (m/s) 并持续 settle_time 秒后提前结束。
    fall_z: 任何目标 z 坐标低于该值就提前结束并报告（默认 -100，防止掉出世界后空跑；
    传 null 关闭；假定世界为 ENU，z 向上）。
    世界不会被重启，观测的是当前正在运行的仿真。
    """
    return await call_main(
        "start_simulation",
        lambda: start_task(
            duration, sample_interval, target_defs,
            stop_when_settled, settle_speed, settle_time, include_orientation, fall_z,
        ),
    )


@tool
async def run_simulation(
    duration: float,
    target_defs: list[str],
    sample_interval: float = 0.05,
    stop_when_settled: bool = False,
    settle_speed: float = 0.01,
    settle_time: float = 0.5,
    include_orientation: bool = False,
    max_samples: int = 50,
    wait_timeout: float = 120.0,
    fall_z: float | None = -100.0,
) -> str:
    """[实验] 一次调用完成：启动观测 → 等待结束 → 返回 summary 和抽样轨迹。

    summary 对每个目标给出：起止位置、位移、路径长度、最大速度、
    各轴最小/最大位置、是否静止、静止用时 (time_to_rest)。
    max_samples: 返回的轨迹帧数（沿整段实验均匀抽样）。
    wait_timeout: 最长等待的真实秒数；超时则返回 status=running 的结果，
    之后用 get_simulation_result 继续取。长实验请先 set_simulation_speed('fast')。
    """
    try:
        wait = require_finite_number(wait_timeout, "wait_timeout")
        samples = require_int_range(max_samples, "max_samples", 1, 2000)

        started = await asyncio.to_thread(
            submit_command,
            lambda: start_task(
                duration, sample_interval, target_defs,
                stop_when_settled, settle_speed, settle_time, include_orientation, fall_z,
            ),
        )
        sim_id = started["simulation"]["simulation_id"]

        deadline = time.monotonic() + wait
        timed_out = False

        while True:
            status = await asyncio.to_thread(
                submit_command,
                lambda: simulation_status_data(get_task(sim_id)),
            )
            if status["status"] != "running":
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            await asyncio.sleep(0.25)

        result = await asyncio.to_thread(
            submit_command,
            lambda: simulation_result_data(sim_id, samples, False),
        )
        result["timed_out"] = timed_out

        if timed_out:
            result["note"] = (
                "Still running after wait_timeout; call get_simulation_result "
                "with this simulation_id later."
            )

        return result_json(result)

    except Exception as exc:
        return tool_error("run_simulation", exc)


@tool
async def get_simulation_status(simulation_id: str = "") -> str:
    """[实验] 查询观测任务状态和进度。simulation_id 可省略（空串）：
    默认查询正在运行的任务，没有则查询最近一次任务。"""
    return await call_main(
        "get_simulation_status",
        lambda: {"status": "success",
                 "simulation": simulation_status_data(resolve_task(simulation_id))},
    )


@tool
async def get_simulation_result(
    simulation_id: str = "",
    max_samples: int = 50,
    summary_only: bool = False,
) -> str:
    """[实验] 获取观测结果：summary（统计）+ 沿整段实验均匀抽样的轨迹帧。
    summary_only=True 时只返回统计，省 token。
    simulation_id 可省略：默认取正在运行的任务，没有则取最近一次任务。"""

    def operation():
        n = require_int_range(max_samples, "max_samples", 1, 2000)
        return simulation_result_data(simulation_id, n, bool(summary_only))

    return await call_main("get_simulation_result", operation)


@tool
async def list_simulations() -> str:
    """[实验] 列出保留的观测任务（最近 10 个）及其状态。"""
    return await call_main(
        "list_simulations",
        lambda: {
            "status": "success",
            "active_simulation_id": simulation.active_simulation_id,
            "simulations": [
                simulation_status_data(t) for t in simulation_tasks.values()
            ],
        },
    )


@tool
async def reset_simulation(simulation_id: str = "") -> str:
    """[实验] 取消观测任务并清除其采样记录。不会重置 Webots 世界或物理状态
    （要回到初始状态请用 restore_snapshot）。simulation_id 可省略：取消正在运行的任务。"""

    def operation():
        task = resolve_task(simulation_id)
        was_running = task["status"] == "running"

        finish_task(
            task, "cancelled",
            "Observation cancelled and samples cleared.", supervisor.getTime(),
        )
        task["samples"].clear()

        return {
            "status": "success",
            "simulation_id": task["simulation_id"],
            "was_running": was_running,
            "samples_cleared": True,
            "world_reset": False,
        }

    return await call_main("reset_simulation", operation)


