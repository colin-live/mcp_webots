"""Output tools (screenshot, world save)."""
import asyncio

from ..registry import tool

import re
from .. import imaging, runtime
from ..config import SAVE_DIR, SCREENSHOT_DIR
from ..runtime import call_main, require_int_range, result_json, submit_command, supervisor, tool_error
from ..scene import find_root_node_by_type

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


@tool
async def save_world(filename: str = "") -> str:
    """[输出] 保存当前世界。filename 为空时覆盖当前世界文件；
    否则保存为 <SAVE_DIR>/<filename>（只接受不含目录的 .wbt 文件名）。"""
    try:
        if not filename:
            def operation():
                supervisor.worldSave()
                return {"status": "success",
                        "message": "worldSave() invoked for the current world file."}
            return await call_main("save_world", operation)

        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.wbt", filename):
            raise ValueError("filename must be a plain name ending in .wbt")

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
                return result_json({"status": "success", "saved_to": str(path)})
            await asyncio.sleep(0.1)

        raise RuntimeError(f"Webots save returned but {path} was not created")

    except Exception as exc:
        return tool_error("save_world", exc)


