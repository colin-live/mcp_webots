"""Motor control tools (RotationalMotor/LinearMotor).

Webots' Motor API is a *controller* API: a controller can only reach the
devices of its own robot (`Robot.getDevice`). A scene `Node` obtained with
`getFromDef` has no `getMotor`, and a supervisor cannot drive the motors of
other robots. These tools therefore only work on the robot that runs the
MCP supervisor controller itself (use robot_def="self" or its DEF name).
"""
import asyncio

from controller import Node

from ..registry import tool

from ..runtime import call_main, require_finite_number, supervisor
from ..scene import get_node

_MOTOR_TYPES = {
    Node.ROTATIONAL_MOTOR: "RotationalMotor",
    Node.LINEAR_MOTOR: "LinearMotor",
}

_NOT_OWN_ROBOT = (
    "{robot} 不是 MCP supervisor 所在的机器人。Webots 的 Motor API 只能访问控制器自己 Robot 上的设备，"
    "Supervisor 无法驱动其它机器人的电机。请把电机放在 supervisor 机器人（controller 为 mcp_server）上，"
    "并用 robot_def=\"self\" 或它的 DEF 名称；其它机器人请用 set_joint_position 摆姿态，"
    "或在它自己的控制器里调用 Motor.setPosition。"
)


# ------------------------------------------------------------
# Helpers (main thread only)
# ------------------------------------------------------------


def _require_own_robot(robot_def: str) -> None:
    """Raise unless robot_def names the robot running this supervisor controller."""
    me = supervisor.getSelf()
    if me is None:
        raise ValueError("无法确定 supervisor 所在的机器人（getSelf 返回空）")

    if isinstance(robot_def, str) and robot_def.strip().lower() in ("self", "@self"):
        return

    node = get_node(robot_def)
    if node.getTypeName() != "Robot":
        raise ValueError(f"{robot_def} 不是 Robot 节点（当前是 {node.getTypeName()}）")
    if node.getId() != me.getId():
        raise ValueError(_NOT_OWN_ROBOT.format(robot=robot_def))


def _motor_type(device) -> str | None:
    try:
        return _MOTOR_TYPES.get(device.getNodeType())
    except Exception:
        return None


def _list_motors() -> list:
    motors = []
    for i in range(supervisor.getNumberOfDevices()):
        device = supervisor.getDeviceByIndex(i)
        if device is not None and _motor_type(device) is not None:
            motors.append(device)
    return motors


def _find_motor(robot_def: str, motor_name: str):
    _require_own_robot(robot_def)
    device = supervisor.getDevice(motor_name)
    if device is None or _motor_type(device) is None:
        available = [m.getName() for m in _list_motors()]
        raise ValueError(
            f"Robot {robot_def} 没有名为 {motor_name!r} 的 Motor。"
            f"可用电机: {available}（也可用 get_motors 查看）。"
        )
    return device


def _call(motor, method: str):
    """Call a no-arg getter if this Webots version has it; return (found, value)."""
    fn = getattr(motor, method, None)
    if fn is None:
        return False, None
    return True, fn()


# ------------------------------------------------------------
# [修改]
# ------------------------------------------------------------


@tool
async def get_motors(robot_def: str) -> str:
    """[修改] 列出 supervisor 所在机器人的所有电机（RotationalMotor/LinearMotor）。

    robot_def 传 "self" 或 supervisor 机器人的 DEF 名称。Webots 的 Motor API 只能访问
    控制器自己 Robot 上的设备，其它机器人会返回明确的错误，而不是空列表。
    返回电机名称、类型、位置/速度/力矩限制，可配合 set_motor_target 使用。
    """

    def operation():
        _require_own_robot(robot_def)

        motors = []
        for i, motor in enumerate(_list_motors()):
            info = {"index": i, "name": motor.getName(), "type": _motor_type(motor)}
            for method, key in (
                ("getMinPosition", "min_position"),
                ("getMaxPosition", "max_position"),
                ("getMaxVelocity", "max_velocity"),
                ("getMaxTorque", "max_torque"),
                ("getMaxForce", "max_force"),
            ):
                try:
                    found, value = _call(motor, method)
                except Exception:
                    continue
                if found:
                    info[key] = value
            motors.append(info)

        return {"status": "success", "robot": robot_def, "count": len(motors), "motors": motors}

    return await call_main("get_motors", operation)


@tool
async def get_motor_state(robot_def: str, motor_name: str) -> str:
    """[查询] 获取指定电机的状态信息（robot_def 同 get_motors，仅限 supervisor 自身机器人）。

    包括目标位置、目标速度、限制等；当前位置来自电机的 PositionSensor（需已启用采样）。
    """

    def operation():
        motor = _find_motor(robot_def, motor_name)

        state = {"status": "success", "robot": robot_def, "motor": motor_name, "type": _motor_type(motor)}

        for method, key in (
            ("getTargetPosition", "target_position"),
            ("getVelocity", "velocity"),
            ("getMinPosition", "min_position"),
            ("getMaxPosition", "max_position"),
            ("getMaxVelocity", "max_velocity"),
            ("getMaxForce", "max_force"),
            ("getMaxTorque", "max_torque"),
            ("getAvailableForce", "available_force"),
            ("getAvailableTorque", "available_torque"),
        ):
            try:
                found, value = _call(motor, method)
            except Exception as exc:
                state[f"{key}_error"] = str(exc)
                continue
            if found:
                state[key] = value

        # Motor has no getPosition; the current position comes from its sensor.
        sensor = motor.getPositionSensor() if hasattr(motor, "getPositionSensor") else None
        if sensor is None:
            state["position_sensor"] = None
        elif sensor.getSamplingPeriod() > 0:
            state["position_sensor"] = sensor.getName()
            state["position"] = sensor.getValue()
        else:
            state["position_sensor"] = sensor.getName()
            state["position_sensor_enabled"] = False

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

    仅限 supervisor 所在的机器人（robot_def 传 "self" 或其 DEF 名称）；其它机器人返回明确错误。
    至少需要指定 position 或 velocity 中的一个。
    - position: 目标位置（旋转电机单位 rad，直线电机单位 m）
    - velocity: 目标速度（旋转电机单位 rad/s，直线电机单位 m/s）
    - acceleration: 最大加速度（可选）
    - force/torque: 力/力矩（可选，视电机类型而定）

    这与 set_joint_position 不同：set_joint_position 直接修改 jointParameters.position（适合摆姿态/复位），
    而 set_motor_target 使用电机控制模型（适合实时调节、轨迹控制）。
    """

    def operation():
        if position is None and velocity is None:
            raise ValueError("至少要指定 position 或 velocity 中的一个")

        motor = _find_motor(robot_def, motor_name)
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
            "type": _motor_type(motor),
            "applied": applied,
        }

    return await call_main("set_motor_target", operation)


@tool
async def enable_motor_torque(robot_def: str, motor_name: str, enable: bool = True) -> str:
    """[修改] 启用或禁用电机扭矩（仅限 supervisor 所在的机器人）。

    在某些场景下可能需要禁用扭矩来手动摆动关节。
    """

    def operation():
        motor = _find_motor(robot_def, motor_name)

        # Webots has no enableTorque. Writing zero force/torque switches the motor to
        # force control, which makes the joint passive; calling setPosition again
        # (with the last target) hands control back to the position controller.
        if enable:
            motor.setPosition(motor.getTargetPosition())
        elif _motor_type(motor) == "LinearMotor":
            motor.setForce(0.0)
        else:
            motor.setTorque(0.0)

        return {
            "status": "success",
            "robot": robot_def,
            "motor": motor_name,
            "torque_enabled": bool(enable),
        }

    return await call_main("enable_motor_torque", operation)
