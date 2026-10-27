"""Output tools (screenshot, world save)."""
import asyncio

from ..registry import tool

import re
from ..config import SAVE_DIR, SCREENSHOT_DIR
from ..runtime import call_main, require_int_range, result_json, submit_command, supervisor, tool_error

# ------------------------------------------------------------
# [输出]
# ------------------------------------------------------------

@tool
async def capture_screenshot(filename: str = "screenshot.png", quality: int = 90) -> str:
    """[输出] 保存 3D 视图截图到 <SAVE_DIR>/screenshots/<filename>（png/jpg），返回文件路径。
    建议在 real_time 模式下使用；画面内容取决于当前 Viewpoint（可用 set_viewpoint 调整）。"""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(png|jpg|jpeg)", filename, re.IGNORECASE):
            raise ValueError("filename must look like name.png / name.jpg (no directories)")

        q = require_int_range(quality, "quality", 1, 100)

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / filename
        path.unlink(missing_ok=True)

        await asyncio.to_thread(
            submit_command, lambda: supervisor.exportImage(str(path), q)
        )

        for _ in range(50):  # the file appears after the next simulation step
            if path.is_file() and path.stat().st_size > 0:
                return result_json({
                    "status": "success",
                    "path": str(path),
                    "bytes": path.stat().st_size,
                })
            await asyncio.sleep(0.1)

        raise RuntimeError(
            "Screenshot file was not created within 5 s; is the world running "
            "with a visible 3D view?"
        )

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


