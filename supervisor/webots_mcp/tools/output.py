"""Output tools (screenshot, world save)."""
import asyncio
from pathlib import Path

from ..registry import tool

import re
from .. import imaging, runtime
from ..config import SAVE_DIR, SCREENSHOT_DIR
from ..runtime import call_main, require_int_range, result_json, submit_command, supervisor, tool_error
from ..scene import (
    JOINT_PARAM_TYPES,
    find_root_node_by_type,
    get_joint_position,
    iter_joints,
    set_joint_position,
)

# ------------------------------------------------------------
# [输出]
# ------------------------------------------------------------

# What the previous screenshot looked like, to notice a frame that did not refresh.
_last_capture = {"md5": None, "sig": None}


def _scene_signature():
    """(sim time, viewpoint pose): changes whenever a fresh frame should look different."""
    sig = [round(supervisor.getTime(), 6)]
    vp = find_root_node_by_type("Viewpoint")
    if vp is not None:
        for name in ("position", "orientation"):
            field = vp.getField(name)
            if field is not None:
                value = field.getSFVec3f() if name == "position" else field.getSFRotation()
                sig.append(tuple(round(float(v), 6) for v in value))
    return tuple(sig)


@tool
async def capture_screenshot(filename: str = "screenshot.jpg", quality: int = 90, scale: float = 1.0,
                             compress_level: int = 9, render_steps: int = 0) -> str:
    """[输出] 保存 3D 视图截图到 <SAVE_DIR>/screenshots/<filename>（jpg/png，默认 jpg），返回文件路径、字节数和 md5。
    quality: JPEG 质量 1-100（PNG 忽略）。scale: 缩放比例 (0,1]，小于 1 需要 Python 环境装有 Pillow，否则返回 warning。
    compress_level: PNG 重新压缩等级 0-9（Webots 导出的 PNG 几乎不压缩，单张约 7.9 MB；默认 9，只改压缩不改像素）。
    暂停状态下 Webots 不会重绘：改完 Viewpoint 或场景后直接截图可能拿到旧画面。
    结果里 stale=true 表示检测到画面没有更新（场景或视角变了但图像字节与上一张相同）；
    可以先 sim_resume(real_time) 再截图，或传 render_steps=N（仅暂停时有效，先精确推进 N 步再截图，仿真时间会前进）。"""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(png|jpg|jpeg)", filename, re.IGNORECASE):
            raise ValueError("filename must look like name.jpg / name.png (no directories)")

        q = require_int_range(quality, "quality", 1, 100)
        level = require_int_range(compress_level, "compress_level", 0, 9)
        steps = require_int_range(render_steps, "render_steps", 0, 1000)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 < scale <= 1:
            raise ValueError("scale must be a number in (0, 1]")

        warnings = []
        clk = runtime.clock
        paused = clk is not None and clk.snapshot()["state"] == "paused"

        if steps and paused:
            fut = clk.submit("step", {"count": steps, "mode": "fast"})
            await asyncio.wait_for(asyncio.wrap_future(fut), timeout=60)
        elif steps:
            warnings.append("render_steps ignored: the simulation is not paused (it renders while running).")

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / filename
        path.unlink(missing_ok=True)

        def export():
            supervisor.exportImage(str(path), q)
            return _scene_signature()

        sig = await asyncio.to_thread(submit_command, export)

        for _ in range(50):  # the file appears after the next simulation step
            if path.is_file() and path.stat().st_size > 0:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(
                "Screenshot file was not created within 5 s; is the world running "
                "with a visible 3D view?"
            )
        await asyncio.sleep(0.05)  # let Webots finish writing

        raw_bytes = path.stat().st_size
        digest = await asyncio.to_thread(imaging.md5_file, path)
        stale = (_last_capture["md5"] == digest and _last_capture["sig"] is not None
                 and _last_capture["sig"] != sig)
        _last_capture.update(md5=digest, sig=sig)
        if stale:
            warnings.append(
                "The image is byte-identical to the previous screenshot although the scene or "
                "viewpoint changed: Webots did not redraw while paused. Call sim_resume(real_time) "
                "first, or pass render_steps=1 (advances simulation time)."
            )

        result = {"status": "success", "path": str(path), "md5": digest, "stale": stale,
                  "simulation_state": "paused" if paused else "running"}

        if scale < 1:
            done, info = await asyncio.to_thread(imaging.downscale, path, float(scale), q)
            if done:
                result["scaled_to"] = list(info)
            else:
                warnings.append(f"scale ignored: {info}.")
        if path.suffix.lower() == ".png":
            await asyncio.to_thread(imaging.recompress_png, path, level)

        result["bytes"] = path.stat().st_size
        if result["bytes"] != raw_bytes:
            result["exported_bytes"] = raw_bytes
        if warnings:
            result["warnings"] = warnings
        return result_json(result)

    except Exception as exc:
        return tool_error("capture_screenshot", exc)


_JOINT_POS_RE = re.compile(r"(?:HingeJointParameters|JointParameters)\s*\{[^{}]*?\bposition\s+([-+0-9.eE]+)")


def _nonzero_saved_joints(path):
    """How many joint `position` values in the saved .wbt are not 0 (None if unreadable)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(1 for m in _JOINT_POS_RE.finditer(text) if abs(float(m.group(1))) > 1e-9)


async def _step_boundary():
    """Wait until the main loop has run at least one more iteration (scene updates applied)."""
    await asyncio.to_thread(submit_command, lambda: None)


@tool
async def save_world(filename: str = "", reset_joints: bool = False) -> str:
    """[输出] 保存当前世界。filename 为空时覆盖当前世界文件；
    否则保存为 <SAVE_DIR>/<filename>（只接受不含目录的 .wbt 文件名）。
    Webots 会把此刻的关节角（HingeJointParameters.position）和连杆姿态写进 .wbt，重新加载就是“弯着”的初始姿态。
    reset_joints=true：保存前临时把所有关节位置置 0（假定 0 是想要的初始姿态），保存后恢复原来的关节角，
    运行中的场景不受影响；返回里的 saved_nonzero_joints 是对保存结果的复查（应为 0）。
    也可以先用 set_joint_position 手动摆好姿态再保存。"""
    try:
        if filename and not re.fullmatch(r"[A-Za-z0-9_.-]+\.wbt", filename):
            raise ValueError("filename must be a plain name ending in .wbt")

        saved_joints = []   # (joint node, axis, previous position)
        warnings = []

        if reset_joints:
            def zero_joints():
                for joint in iter_joints():
                    for axis in JOINT_PARAM_TYPES[joint.getTypeName()]:
                        before = get_joint_position(joint, axis)
                        if before != 0.0:
                            saved_joints.append((joint, axis, before))
                            set_joint_position(joint, 0.0, axis)
                return len(saved_joints)

            await asyncio.to_thread(submit_command, zero_joints)
            if saved_joints:
                await _step_boundary()   # let Webots apply the new joint positions

        try:
            if not filename:
                def operation():
                    supervisor.worldSave()
                    return supervisor.getWorldPath()

                world_path = await asyncio.to_thread(submit_command, operation)
                await _step_boundary()
                result = {"status": "success",
                          "message": "worldSave() invoked for the current world file."}
                path = Path(world_path) if world_path else None
            else:
                SAVE_DIR.mkdir(parents=True, exist_ok=True)
                path = SAVE_DIR / filename

                def operation():
                    ok = supervisor.worldSave(str(path))
                    if ok is False or ok == -1:
                        raise RuntimeError("Webots reported that saving the world failed")
                    return str(path)

                await asyncio.to_thread(submit_command, operation)

                for _ in range(30):  # saving may complete at the end of the step
                    if path.is_file():
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError(f"Webots save returned but {path} was not created")
                result = {"status": "success", "saved_to": str(path)}
        finally:
            if saved_joints:
                def restore():
                    for joint, axis, before in saved_joints:
                        set_joint_position(joint, before, axis)

                try:
                    await asyncio.to_thread(submit_command, restore)
                except Exception as exc:  # never hide the save result behind a restore failure
                    warnings.append(f"could not restore the original joint positions: {exc}")

        if reset_joints:
            result["joints_reset"] = len(saved_joints)
            nonzero = _nonzero_saved_joints(path) if path is not None else None
            result["saved_nonzero_joints"] = nonzero
            if nonzero:
                warnings.append(
                    f"{nonzero} joint position(s) in the saved file are still non-zero "
                    "(Webots may not have applied the reset before saving; call sim_step count=1 "
                    "while paused and save again, or use set_joint_position first)."
                )
        if warnings:
            result["warnings"] = warnings
        return result_json(result)

    except Exception as exc:
        return tool_error("save_world", exc)
