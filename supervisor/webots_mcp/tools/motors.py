"""Motor control tools (RotationalMotor/LinearMotor)."""
import asyncio

from ..registry import tool

from ..runtime import call_main, require_finite_number
from ..scene import get_node


# ------------------------------------------------------------
# [修改]
# ------------------------------------------------------------


@tool
async def get_motors(robot_def: str) -> str:
    """[修改] 列出 Robot 的所有电机（RotationalMotor/LinearMotor）。
    
    返回电机名称、类型、索引等信息。使用电机名称可配合 set_motor_target
    通过 Motor API 实时下发目标位置/速度（这与直接修改 jointParameters.position 不同）。
    """

    def operation():
        node = get_node(robot_def)
        if node.getTypeName() != "Robot":
            raise ValueError(f"{robot_def} 不是 Robot 节点（当前是 {node.getTypeName()}）")

        motors = []
        i = 0
        while True:
            try:
                motor = node.getMotor(i)
            except Exception:
                break
            if motor is None:
                break
            try:
                motors.append(
                    {
                        "index": i,
                        "name": motor.getName(),
                        "type": type(motor).__name__,
                    }
                )
            except Exception as exc:
                motors.append({"index": i, "error": str(exc)})
            i += 1
            if i > 256:  # safety bound
                break

        return {"status": "success", "robot": robot_def, "count": len(motors), "motors": motors}

    return await call_main("get_motors", operation)


@tool
async def get_motor_state(robot_def: str, motor_name: str) -> str:
    """[查询] 获取指定电机的状态信息。

    包括当前位置、目标位置、速度等（视 Webots 版本和电机类型而定）。
    """

    def operation():
        node = get_node(robot_def)
        if node.getTypeName() != "Robot":
            raise ValueError(f"{robot_def} 不是 Robot 节点（当前是 {node.getTypeName()}）")

        motor = node.getMotor(motor_name)
        if motor is None:
            raise ValueError(
                f"Robot {robot_def} 没有名为 {motor_name!r} 的 Motor。"
                "可用 get_motors 查看可用电机列表。"
            )

        state = {"status": "success", "robot": robot_def, "motor": motor_name, "type": type(motor).__name__}

        for method, key in (
            ("getPosition", "position"),
            ("getTargetPosition", "target_position"),
            ("getVelocity", "velocity"),
            ("getMaxVelocity", "max_velocity"),
            ("getMaxForce", "max_force"),
            ("getMaxTorque", "max_torque"),
            ("getAvailableForce", "available_force"),
            ("getAvailableTorque", "available_torque"),
        ):
            fn = getattr(motor, method, None)
            if fn is None:
                continue
            try:
                state[key] = fn()
            except Exception as exc:
                state[f"{key}_error"] = str(exc)

        return state

    return await call_main("get_motor_state", operation)


@tool
async def set_motor_target(
    robot_def: str,
    motor_name: str,
    position: float | None = None,
    velocity: float | None = None,
    acceleration: float | None = None,
    force: float | None = None,
    torque: float | None = None,
) -> str:
    """[修改] 通过 Motor API 实时设置电机目标（RotationalMotor/LinearMotor）。

    至少需要指定 position 或 velocity 中的一个。
    - position: 目标位置（旋转电机单位 rad，直线电机单位 m）
    - velocity: 目标速度（旋转电机单位 rad/s，直线电机单位 m/s）
    - acceleration: 最大加速度（可选）
    - force/torque: 力矩限制（可选，视电机类型而定）

    这与 set_joint_position 不同：set_joint_position 直接修改 jointParameters.position（适合摆姿态/复位），
    而 set_motor_target 使用电机控制模型（适合实时调节、轨迹控制）。
    """

    def operation():
        if position is None and velocity is None:
            raise ValueError("至少要指定 position 或 velocity 中的一个")

        node = get_node(robot_def)
        if node.getTypeName() != "Robot":
            raise ValueError(f"{robot_def} 不是 Robot 节点（当前是 {node.getTypeName()}）")

        motor = node.getMotor(motor_name)
        if motor is None:
            raise ValueError(
                f"Robot {robot_def} 没有名为 {motor_name!r} 的 Motor。"
                "可用 get_motors 查看可用电机列表。"
            )

        applied = {}

        if position is not None:
            pos = require_finite_number(position, "position")
            motor.setPosition(float(pos))
            applied["position"] = float(pos)

        if velocity is not None:
            vel = require_finite_number(velocity, "velocity")
            motor.setVelocity(float(vel))
            applied["velocity"] = float(vel)

        if acceleration is not None and hasattr(motor, "setAcceleration"):
            acc = require_finite_number(acceleration, "acceleration")
            motor.setAcceleration(float(acc))
            applied["acceleration"] = float(acc)

        if force is not None and hasattr(motor, "setForce"):
            f = require_finite_number(force, "force")
            motor.setForce(float(f))
            applied["force"] = float(f)

        if torque is not None and hasattr(motor, "setTorque"):
            t = require_finite_number(torque, "torque")
            motor.setTorque(float(t))
            applied["torque"] = float(t)

        return {
            "status": "success",
            "robot": robot_def,
            "motor": motor_name,
            "type": type(motor).__name__,
            "applied": applied,
        }

    return await call_main("set_motor_target", operation)


@tool
async def enable_motor_torque(robot_def: str, motor_name: str, enable: bool = True) -> str:
    """[修改] 启用或禁用电机扭矩。

    在某些场景下可能需要禁用扭矩来手动摆动关节。
    """

    def operation():
        node = get_node(robot_def)
        if node.getTypeName() != "Robot":
            raise ValueError(f"{robot_def} 不是 Robot 节点（当前是 {node.getTypeName()}）")

        motor = node.getMotor(motor_name)
        if motor is None:
            raise ValueError(
                f"Robot {robot_def} 没有名为 {motor_name!r} 的 Motor。"
                "可用 get_motors 查看可用电机列表。"
            )

        if hasattr(motor, "enableTorque"):
            motor.enableTorque(bool(enable))
        elif hasattr(motor, "setTorque") and not enable:
            motor.setTorque(0.0)
        else:
            raise ValueError("此电机不支持 enableTorque/setTorque")

        return {
            "status": "success",
            "robot": robot_def,
            "motor": motor_name,
            "torque_enabled": bool(enable),
        }

    return await call_main("enable_motor_torque", operation)
