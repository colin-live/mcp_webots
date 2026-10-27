#!/usr/bin/env python3
"""project_manager: standalone process that manages Webots projects on disk.

Spawned by the Rust host, which talks to it over this process' stdin/stdout
with the same protocol the Supervisor uses (hello + tool manifest, tool.call).
It never imports the Webots API and works before Webots has ever started.

    stdout  protocol messages only (logging goes to stderr)

Tools: project_list / project_create / project_info / project_check /
project_delete / project_read_file / project_write_file / world_add_robot /
world_remove_node / supervisor_bind.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from webots_mcp import registry  # noqa: E402  (also configures logging -> stderr)
from webots_mcp import proto_inline  # noqa: E402
from webots_mcp.registry import tool  # noqa: E402

PROTOCOL = 1
SUPERVISOR_CONTROLLER = "mcp_server"
SUPERVISOR_DEF = "MCP_SUPERVISOR"

# Where the shared supervisor controller (mcp_server.py + webots_mcp/) lives.
SUPERVISOR_DIR = Path(os.environ.get("WEBOTS_MCP_SUPERVISOR_DIR") or HERE).expanduser().resolve()


def _default_root():
    try:
        return HERE.parents[2]  # <root>/<project>/controllers/mcp_server
    except IndexError:
        return Path.cwd() / "webots_projects"


PROJECTS_ROOT = Path(os.environ.get("WEBOTS_MCP_PROJECTS_DIR") or _default_root()).expanduser().resolve()

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
DEF_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------

def dump(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def ok(**kw):
    return dump({"status": "success", **kw})


def fail(kind, message, **kw):
    return dump({"status": "error", "error_type": kind, "message": message, **kw})


class Problem(Exception):
    def __init__(self, kind, message, **extra):
        super().__init__(message)
        self.kind, self.extra = kind, extra


def guarded(fn):
    """Turn Problem / unexpected errors into the standard error JSON."""
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Problem as p:
            return fail(p.kind, str(p), **p.extra)
        except Exception as exc:  # noqa: BLE001
            return fail(type(exc).__name__, str(exc))
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__annotations__ = fn.__annotations__
    wrapper.__wrapped__ = fn
    return wrapper


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_name(name, what="project_name"):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Problem("InvalidName", f"{what} must be 1-80 chars of letters, digits, '.', '_' or '-', starting with a letter/digit.")
    return name


def project_dir(name):
    path = (PROJECTS_ROOT / check_name(name)).resolve()
    if path.parent != PROJECTS_ROOT:
        raise Problem("InvalidName", "Project path escapes the projects root.")
    return path


def existing_project(name):
    path = project_dir(name)
    if not path.is_dir():
        raise Problem("ProjectNotFound", f"Project does not exist: {path}", hint="project_list shows the available projects.")
    return path


def is_project(path):
    return path.is_dir() and not path.is_symlink() and ((path / "project.json").is_file() or (path / "worlds").is_dir())


def inside(base, rel):
    if not isinstance(rel, str) or not rel.strip():
        raise Problem("InvalidPath", "A non-empty relative path is required.")
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise Problem("InvalidPath", f"Path escapes the project: {rel}")
    return target


def read_manifest(project):
    try:
        data = json.loads((project / "project.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_manifest(project, manifest):
    manifest["updated_at"] = now_iso()
    (project / "project.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def project_files(project):
    found = []
    for base, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink() and d != "__pycache__"]
        found.extend(Path(base) / f for f in files)
    return sorted(found, key=lambda p: p.relative_to(project).as_posix().lower())


def world_file(project, world_name):
    check_name(world_name, "world_name")
    path = project / "worlds" / f"{world_name}.wbt"
    if not path.is_file():
        worlds = sorted(p.stem for p in (project / "worlds").glob("*.wbt")) if (project / "worlds").is_dir() else []
        raise Problem("WorldNotFound", f"World not found: {path}", available_worlds=worlds)
    return path


def read_text(path):
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# ------------------------------------------------------------
# world text editing
# ------------------------------------------------------------

HEADER_RE = re.compile(r"#VRML_SIM\s+(R\d{4}[ab])")


def detect_version():
    """Header version for new worlds: env, an existing world, Webots install, default."""
    if os.environ.get("WEBOTS_MCP_WORLD_VERSION"):
        return os.environ["WEBOTS_MCP_WORLD_VERSION"]
    try:
        for world in sorted(PROJECTS_ROOT.glob("*/worlds/*.wbt")):
            m = HEADER_RE.search(read_text(world)[:200])
            if m:
                return m.group(1)
    except OSError:
        pass
    home = os.environ.get("WEBOTS_HOME")
    if home:
        try:
            m = re.search(r"R\d{4}[ab]", (Path(home) / "resources" / "version.txt").read_text())
            if m:
                return m.group(0)
        except OSError:
            pass
    return "R2025a"


def supervisor_node(controller=SUPERVISOR_CONTROLLER):
    return (f'DEF {SUPERVISOR_DEF} Robot {{\n  name "mcp_supervisor"\n  controller "{controller}"\n'
            f'  supervisor TRUE\n}}\n')


def world_template(version, with_supervisor):
    base = "webots://projects/objects/backgrounds/protos/"
    text = (
        f"#VRML_SIM {version} utf8\n\n"
        f'EXTERNPROTO "{base}TexturedBackground.proto"\n'
        f'EXTERNPROTO "{base}TexturedBackgroundLight.proto"\n\n'
        'WorldInfo {\n  basicTimeStep 16\n  coordinateSystem "ENU"\n}\n'
        "Viewpoint {\n  orientation 0 1 0 0.6\n  position -3 0 2\n}\n"
        "TexturedBackground {\n}\nTexturedBackgroundLight {\n}\n"
        "DEF FLOOR Solid {\n  translation 0 0 -0.1\n  children [\n    Shape {\n"
        "      appearance PBRAppearance { baseColor 0.75 0.75 0.75 roughness 0.8 metalness 0 }\n"
        "      geometry Box { size 4 4 0.2 }\n    }\n  ]\n"
        '  name "floor"\n  boundingObject Box { size 4 4 0.2 }\n}\n'
    )
    if with_supervisor:
        text += supervisor_node()
    return text


def insert_externproto(text, url):
    line = f'EXTERNPROTO "{url}"'
    if line in text:
        return text
    matches = list(re.finditer(r"(?m)^EXTERNPROTO .*$", text))
    if matches:
        end = matches[-1].end()
        return text[:end] + "\n" + line + text[end:]
    nl = text.find("\n")
    if nl < 0:
        return text + "\n\n" + line + "\n"
    return text[:nl + 1] + "\n" + line + "\n" + text[nl + 1:]


def find_root_def(text, name):
    """(start, end) of a root-level `DEF name Type { ... }`, or None."""
    m = re.search(rf"(?m)^DEF\s+{re.escape(name)}\s+[A-Za-z_]\w*\s*\{{", text)
    if not m:
        return None
    i, depth, in_str = m.end(), 1, False
    while i < len(text) and depth:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return (m.start(), i) if depth == 0 else None


def has_supervisor(text, controller=SUPERVISOR_CONTROLLER):
    return f'controller "{controller}"' in text and re.search(r"supervisor\s+TRUE", text) is not None


def resolve_proto(robot, proto_url, project, worlds_dir):
    """-> (proto type name, EXTERNPROTO url)."""
    if proto_url:
        stem = Path(proto_url.split("?")[0]).stem
        return stem, proto_url
    name = robot[:-6] if robot.lower().endswith(".proto") else robot
    if "/" in robot or "\\" in robot:
        target = inside(project, robot)
        if not target.is_file():
            raise Problem("ProtoNotFound", f"PROTO file not found in the project: {robot}")
        return target.stem, os.path.relpath(target, worlds_dir).replace("\\", "/")
    check_name(name, "robot")
    local = project / "protos" / f"{name}.proto"
    if local.is_file():
        return name, os.path.relpath(local, worlds_dir).replace("\\", "/")
    home = os.environ.get("WEBOTS_HOME")
    if home:
        hits = sorted((Path(home) / "projects" / "robots").glob(f"**/protos/{name}.proto"))
        if hits:
            rel = hits[0].relative_to(home).as_posix()
            return name, f"webots://{rel}"
    raise Problem(
        "ProtoNotFound", f"Cannot find a PROTO named {name!r}.",
        hint="Pass proto_url (webots:// or https://...), put <name>.proto in the project's protos/ folder, or set WEBOTS_HOME so the Webots robot library can be searched.")


def proto_source_file(url, worlds_dir):
    """Local .proto file behind an EXTERNPROTO url from resolve_proto, or None if remote."""
    if url.startswith("webots://"):
        home = os.environ.get("WEBOTS_HOME")
        if not home:
            return None
        return Path(home) / url[len("webots://"):]
    if re.match(r"^https?://", url):
        return None
    return (worlds_dir / url).resolve()


# ------------------------------------------------------------
# supervisor binding
# ------------------------------------------------------------

def install_supervisor(project, controller=SUPERVISOR_CONTROLLER, mode="link"):
    if mode not in ("link", "copy"):
        raise Problem("InvalidArguments", "mode must be 'link' or 'copy'.")
    check_name(controller, "controller_name")
    target = project / "controllers" / controller
    if target.resolve() == SUPERVISOR_DIR:
        return {"mode": "in_place", "controller_dir": str(target), "note": "this project already contains the shared supervisor controller"}
    target.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        launcher = (
            '"""Launcher generated by project_manager: runs the shared supervisor controller."""\n'
            "import os\nimport runpy\nimport sys\nfrom pathlib import Path\n\n"
            f"SUPERVISOR_DIR = {json.dumps(str(SUPERVISOR_DIR))}\n"
            "sys.path.insert(0, SUPERVISOR_DIR)\n"
            '# <project>/controllers/<name>/<name>.py -> save worlds and screenshots under <project>/data\n'
            'os.environ.setdefault("WEBOTS_MCP_SAVE_DIR", str(Path(__file__).resolve().parents[2] / "data"))\n'
            'runpy.run_path(os.path.join(SUPERVISOR_DIR, "mcp_server.py"), run_name="__main__")\n'
        )
        write_text(target / f"{controller}.py", launcher)
        return {"mode": "link", "controller_dir": str(target), "supervisor_dir": str(SUPERVISOR_DIR)}
    src = SUPERVISOR_DIR / "mcp_server.py"
    if not src.is_file():
        raise Problem("SupervisorNotFound", f"mcp_server.py not found in {SUPERVISOR_DIR}")
    shutil.copyfile(src, target / f"{controller}.py")
    shutil.copytree(SUPERVISOR_DIR / "webots_mcp", target / "webots_mcp", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.bak"))
    return {"mode": "copy", "controller_dir": str(target)}


# ------------------------------------------------------------
# tools: projects
# ------------------------------------------------------------

@tool
@guarded
async def project_list() -> str:
    """[项目] 列出项目根目录下的 Webots 项目（含 worlds/ 或 project.json 的目录）。"""
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(PROJECTS_ROOT.iterdir(), key=lambda x: x.name.lower()):
        if is_project(p):
            worlds = sorted(w.stem for w in (p / "worlds").glob("*.wbt")) if (p / "worlds").is_dir() else []
            items.append({"project_name": p.name, "path": str(p), "worlds": worlds,
                          "description": read_manifest(p).get("description", "")})
    return ok(projects_root=str(PROJECTS_ROOT), count=len(items), projects=items)


@tool
@guarded
async def project_create(project_name: str, description: str = "", world_name: str = "main",
                         with_supervisor: bool = True, supervisor_mode: str = "link",
                         overwrite: bool = False) -> str:
    """[项目] 一步创建可运行的 Webots 项目：标准目录、project.json、带背景/光照/地面的世界，
    并（默认）嵌入 supervisor 机器人、绑定 supervisor 控制器，返回的 world_path 可直接传给 sim_start。
    supervisor_mode: link=启动器指向共享的 supervisor 代码（推荐，只有一份代码）；copy=把代码复制进项目。
    世界为 ENU（z 向上），地面是厚 0.2 m 的静态 Box，DEF 为 FLOOR。"""
    dest = project_dir(project_name)
    check_name(world_name, "world_name")
    if dest.exists():
        if not overwrite:
            raise Problem("ProjectExists", f"Project already exists: {dest}", hint="Use overwrite=true or another name.")
        if dest.parent != PROJECTS_ROOT or dest == PROJECTS_ROOT:
            raise Problem("InvalidName", "Refusing to overwrite a path outside the projects root.")
        shutil.rmtree(dest)
    for folder in ("worlds", "protos", "controllers", "plugins", "textures", "meshes", "data"):
        (dest / folder).mkdir(parents=True)
    version = detect_version()
    world_rel = f"worlds/{world_name}.wbt"
    write_text(dest / world_rel, world_template(version, with_supervisor))
    binding = install_supervisor(dest, mode=supervisor_mode) if with_supervisor else None
    write_manifest(dest, {
        "schema_version": 2, "name": project_name, "description": description, "created_at": now_iso(),
        "entry_world": world_rel, "world_version": version,
        "supervisor_controller": SUPERVISOR_CONTROLLER if with_supervisor else None,
    })
    write_text(dest / "README.md", f"# {project_name}\n\n{description.strip()}\n")
    return ok(project_name=project_name, project_path=str(dest), world_path=str(dest / world_rel),
              world_version=version, supervisor=binding,
              next_step="sim_start(world=<world_path>) 启动仿真；暂停状态下即可用构建类工具搭场景。")


@tool
@guarded
async def project_info(project_name: str, tree_depth: int = 2) -> str:
    """[项目] 项目概况：世界、PROTO、控制器、supervisor 绑定状态、manifest，以及 tree_depth 层目录树（0 表示不返回）。"""
    project = existing_project(project_name)
    files = project_files(project)
    rels = [f.relative_to(project) for f in files]
    worlds = []
    for w in sorted((project / "worlds").glob("*.wbt")) if (project / "worlds").is_dir() else []:
        text = read_text(w)
        worlds.append({"world_name": w.stem, "world_path": str(w), "version": (HEADER_RE.search(text[:200]) or [None, None])[1],
                       "supervisor_bound": has_supervisor(text)})
    info = {
        "project_name": project_name, "project_path": str(project), "worlds": worlds,
        "protos": [r.as_posix() for r in rels if r.suffix.lower() == ".proto"],
        "controllers": sorted({r.parts[1] for r in rels if len(r.parts) >= 3 and r.parts[0] == "controllers"}),
        "file_count": len(files), "manifest": read_manifest(project),
    }
    if isinstance(tree_depth, int) and tree_depth > 0:
        info["tree"] = sorted(r.as_posix() for r in rels if len(r.parts) <= tree_depth)
    return ok(**info)


@tool
@guarded
async def project_check(project_name: str) -> str:
    """[项目] 静态检查：目录、世界头、EXTERNPROTO 能否解析、控制器目录是否存在、supervisor 是否绑定。
    不运行 Webots，不验证物理或控制器运行时行为。"""
    project = existing_project(project_name)
    issues = []

    def add(sev, code, path, msg):
        issues.append({"severity": sev, "code": code, "path": path, "message": msg})

    worlds = sorted((project / "worlds").glob("*.wbt")) if (project / "worlds").is_dir() else []
    if not worlds:
        add("error", "NO_WORLD", "worlds/", "No .wbt world found. Use project_create or write one with project_write_file.")
    controllers = {p.name for p in (project / "controllers").iterdir() if p.is_dir()} if (project / "controllers").is_dir() else set()
    home = os.environ.get("WEBOTS_HOME")
    for w in worlds:
        rel, text = w.relative_to(project).as_posix(), read_text(w)
        if not HEADER_RE.search(text[:200]):
            add("warning", "MISSING_HEADER", rel, "World does not start with a #VRML_SIM header.")
        for url in re.findall(r'(?m)^EXTERNPROTO\s+"([^"]+)"', text):
            if url.startswith(("http://", "https://")):
                continue
            if url.startswith("webots://"):
                if home and not (Path(home) / url[len("webots://"):]).exists():
                    add("error", "MISSING_PROTO", rel, f"{url} not found under WEBOTS_HOME.")
            elif not (w.parent / url).resolve().is_file():
                add("error", "MISSING_PROTO", rel, f"EXTERNPROTO file not found: {url}")
        for ctrl in set(re.findall(r'controller\s+"([^"]+)"', text)):
            if ctrl not in ("<none>", "<generic>", "<extern>") and ctrl not in controllers:
                add("error", "MISSING_CONTROLLER", rel, f"controller {ctrl!r} has no folder under controllers/.")
        if not has_supervisor(text):
            add("warning", "SUPERVISOR_NOT_BOUND", rel, "No Robot with supervisor TRUE and controller 'mcp_server'; use supervisor_bind.")
    errors = sum(i["severity"] == "error" for i in issues)
    return ok(project_name=project_name, integrity="failed" if errors else ("warning" if issues else "passed"),
              worlds=[w.stem for w in worlds], issues=issues,
              limitations="Static check only; Webots grammar, physics and controller runtime are not validated.")


@tool
@guarded
async def project_delete(project_name: str, confirm_project_name: str) -> str:
    """[项目] 删除整个项目目录。必须重复一遍项目名确认；不可恢复。"""
    if project_name != confirm_project_name:
        raise Problem("ConfirmationFailed", "confirm_project_name must exactly match project_name.")
    dest = existing_project(project_name)
    if dest.parent != PROJECTS_ROOT:
        raise Problem("InvalidName", "Refusing to delete outside the projects root.")
    shutil.rmtree(dest)
    return ok(deleted_project=project_name, deleted_path=str(dest))


@tool
@guarded
async def project_read_file(project_name: str, relative_path: str, max_chars: int = 30000) -> str:
    """[项目] 读取项目内的 UTF-8 文本文件（世界、PROTO、控制器等），带路径越界保护和长度上限（1-100000）。"""
    project = existing_project(project_name)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 100000:
        raise Problem("InvalidArguments", "max_chars must be an integer between 1 and 100000.")
    target = inside(project, relative_path)
    if not target.is_file():
        raise Problem("FileNotFound", f"File not found: {relative_path}")
    text = read_text(target)
    return ok(relative_path=target.relative_to(project).as_posix(), content=text[:max_chars],
              truncated=len(text) > max_chars, total_chars=len(text))


@tool
@guarded
async def project_write_file(project_name: str, relative_path: str, content: str, overwrite: bool = True) -> str:
    """[项目] 写入项目内的 UTF-8 文本文件（控制器、PROTO、配置），自动创建父目录，内容上限 1,000,000 字符。
    改世界里的节点请优先用 world_add_robot / world_remove_node，而不是整文件重写。"""
    project = existing_project(project_name)
    if len(content) > 1_000_000:
        raise Problem("TooLarge", "content exceeds 1,000,000 characters.")
    target = inside(project, relative_path)
    if target.exists() and not overwrite:
        raise Problem("FileExists", f"File already exists: {relative_path}")
    write_text(target, content)
    manifest = read_manifest(project)
    if manifest:
        write_manifest(project, manifest)
    return ok(relative_path=target.relative_to(project).as_posix(), size_bytes=target.stat().st_size)


# ------------------------------------------------------------
# tools: worlds
# ------------------------------------------------------------

@tool
@guarded
async def world_add_robot(project_name: str, robot: str, world_name: str = "main", def_name: str = "",
                          translation: list[float] | None = None, rotation: list[float] | None = None,
                          controller: str = "", controller_source: str = "", fields: str = "",
                          proto_url: str = "", inline: bool = False) -> str:
    """[世界] 把机器人嵌入世界文件：自动补 EXTERNPROTO 声明并追加节点（改的是 .wbt 文件，不是运行中的场景）。
    robot: PROTO 名（先找项目 protos/<名>.proto，再找 WEBOTS_HOME/projects/robots 下的库）、项目内 .proto 相对路径，
    或配合 proto_url 给 webots:// / https:// 地址。def_name 省略则用 robot 名转大写。
    controller: 控制器名；给出 controller_source（Python 源码）时会写入 controllers/<名>/<名>.py。
    fields: 追加到节点里的原始 VRML 字段行，例如 'supervisor FALSE'。translation/rotation 同 Webots（ENU，z 向上）。
    注意：PROTO 实例内部声明的 DEF 默认对 Supervisor 不可见（get_node_state("TCP") 会报 Node not found）。
    两种办法：用点路径访问实例内部 DEF，如 "ARM2DOF.TCP"（返回值 inner_defs 列出可用名字）；
    或设 inline=true，把 PROTO 主体展开写进世界，内部 DEF 变成普通 DEF（不支持含 %< > % 模板代码的 PROTO，也不支持远程 https PROTO）。
    修改世界文件后需要 sim_restart 或 sim_reset(reload) 才会生效。"""
    project = existing_project(project_name)
    path = world_file(project, world_name)
    text = read_text(path)
    proto, url = resolve_proto(robot, proto_url, project, path.parent)
    def_name = def_name or re.sub(r"\W", "_", proto).upper()
    if not DEF_RE.fullmatch(def_name):
        raise Problem("InvalidName", "def_name must match [A-Za-z_][A-Za-z0-9_]*.")
    if re.search(rf"\bDEF\s+{re.escape(def_name)}\b", text):
        raise Problem("DefExists", f"DEF {def_name} already exists in the world.", hint="Pick another def_name or world_remove_node first.")
    lines = []
    overrides = {}
    if translation is not None:
        if len(translation) != 3:
            raise Problem("InvalidArguments", "translation needs 3 numbers.")
        overrides["translation"] = " ".join(repr(float(v)) for v in translation)
    if rotation is not None:
        if len(rotation) != 4:
            raise Problem("InvalidArguments", "rotation needs 4 numbers [ax, ay, az, angle].")
        overrides["rotation"] = " ".join(repr(float(v)) for v in rotation)
    warnings = []
    if controller:
        check_name(controller, "controller")
        overrides["controller"] = f'"{controller}"'
        ctrl_dir = project / "controllers" / controller
        if controller_source:
            write_text(ctrl_dir / f"{controller}.py", controller_source)
        elif not ctrl_dir.is_dir():
            warnings.append(f"controllers/{controller}/ does not exist yet; pass controller_source or write it with project_write_file.")
    extra_lines = [l.strip() for l in fields.strip().splitlines() if l.strip()] if fields.strip() else []

    src_file = proto_source_file(url, path.parent)
    parsed = None
    if src_file is not None and src_file.is_file():
        try:
            parsed = proto_inline.parse_proto(read_text(src_file))
        except proto_inline.InlineError as exc:
            if inline:
                raise Problem(exc.kind, str(exc), hint="Use the dotted DEF path (e.g. 'ARM.TCP') instead, or flatten the PROTO into a plain node by hand with project_write_file.")

    if inline:
        if parsed is None:
            raise Problem("InlineUnsupported", f"Cannot read the PROTO source for {proto!r} (remote url, or WEBOTS_HOME not set).",
                          hint="Put the .proto in the project's protos/ folder (or set WEBOTS_HOME) and retry.")
        try:
            body = proto_inline.expand_body(parsed)
            node_inner_defs = proto_inline.inner_defs(body)
            for line in extra_lines:
                key, _, value = line.partition(" ")
                overrides[key] = value.strip()
            node = proto_inline.build_node(body, def_name, overrides)
        except proto_inline.InlineError as exc:
            raise Problem(exc.kind, str(exc))
        clashes = [d for d in node_inner_defs if re.search(rf"\bDEF\s+{re.escape(d)}\b", text)]
        if clashes:
            raise Problem("DefExists", f"DEFs inside the PROTO already exist in the world: {sorted(set(clashes))}.",
                          hint="Remove the existing nodes, or use the default (non-inline) mode and address inner DEFs as '<def_name>.<inner>'.")
        for ext in proto_inline.rebase_externprotos(parsed["externprotos"], str(src_file.parent), str(path.parent)):
            text = insert_externproto(text, ext)
        text = text.rstrip("\n") + "\n" + node
        write_text(path, text)
        return ok(project_name=project_name, world_path=str(path), def_name=def_name, proto=proto, inline=True,
                  inner_defs=node_inner_defs, warnings=warnings,
                  note="PROTO body written into the world; inner DEFs are plain DEFs (visible to the Supervisor). "
                       "World file changed on disk; restart or reload the world for it to take effect.")

    for key, value in overrides.items():
        lines.append(f"{key} {value}")
    lines.extend(extra_lines)
    node = f"DEF {def_name} {proto} {{\n" + "".join(f"  {l}\n" for l in lines) + "}\n"
    text = insert_externproto(text, url)
    text = text.rstrip("\n") + "\n" + node
    write_text(path, text)
    extra = {}
    if parsed is not None:
        inner = proto_inline.inner_defs(parsed["body"])
        if inner:
            extra = {"inner_defs": inner,
                     "inner_def_note": f"These DEFs live inside the PROTO instance: address them as '{def_name}.<name>' "
                                       f"(e.g. '{def_name}.{inner[0]}'), or re-add the robot with inline=true to make them plain DEFs."}
    return ok(project_name=project_name, world_path=str(path), def_name=def_name, proto=proto, proto_url=url,
              warnings=warnings, note="World file changed on disk; restart or reload the world for it to take effect.", **extra)


@tool
@guarded
async def world_remove_node(project_name: str, def_name: str, world_name: str = "main") -> str:
    """[世界] 从世界文件里删除一个根级 `DEF 名 类型 { ... }` 节点（匹配花括号，忽略字符串和注释）。
    只改 .wbt 文件；不会清理不再使用的 EXTERNPROTO 行。"""
    project = existing_project(project_name)
    path = world_file(project, world_name)
    if not DEF_RE.fullmatch(def_name or ""):
        raise Problem("InvalidName", "def_name must match [A-Za-z_][A-Za-z0-9_]*.")
    text = read_text(path)
    span = find_root_def(text, def_name)
    if span is None:
        raise Problem("NodeNotFound", f"No root-level node with DEF {def_name} in {path.name}.")
    start, end = span
    if text[end:end + 1] == "\n":
        end += 1
    write_text(path, text[:start] + text[end:])
    return ok(world_path=str(path), removed_def=def_name, removed_chars=end - start)


@tool
@guarded
async def supervisor_bind(project_name: str, world_name: str = "main", mode: str = "link",
                          controller_name: str = SUPERVISOR_CONTROLLER) -> str:
    """[绑定] 给已有项目/世界绑定 supervisor：世界里没有 supervisor 机器人就追加一个（DEF MCP_SUPERVISOR），
    并在 controllers/<controller_name>/ 安装控制器。mode=link 写一个指向共享 supervisor 代码的启动器；
    mode=copy 复制代码进项目。项目本身就是共享 supervisor 所在位置时不会覆盖任何文件。"""
    project = existing_project(project_name)
    path = world_file(project, world_name)
    text = read_text(path)
    added = False
    if not has_supervisor(text, controller_name):
        if re.search(rf"\bDEF\s+{SUPERVISOR_DEF}\b", text):
            raise Problem("DefExists", f"DEF {SUPERVISOR_DEF} exists but is not a supervisor with controller {controller_name!r}.",
                          hint="Use controller_name matching the existing robot's controller, or edit/remove that node first (world_remove_node).")
        write_text(path, text.rstrip("\n") + "\n" + supervisor_node(controller_name))
        added = True
    binding = install_supervisor(project, controller_name, mode)
    manifest = read_manifest(project)
    if manifest:
        manifest["supervisor_controller"] = controller_name
        write_manifest(project, manifest)
    return ok(world_path=str(path), supervisor_robot_added=added, binding=binding)


# ------------------------------------------------------------
# stdio provider (same protocol as the Supervisor's IPC client)
# ------------------------------------------------------------

_out_lock = threading.Lock()


def send(message):
    data = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def log(text):
    print(f"[project_manager] {text}", file=sys.stderr, flush=True)


async def handle_call(msg_id, params):
    try:
        text = await registry.call_tool(params.get("name"), params.get("arguments"))
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"text": text}})
    except registry.ToolError as exc:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": str(exc)}})
    except Exception as exc:  # noqa: BLE001
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}})


async def serve():
    loop = asyncio.get_running_loop()
    inbox = asyncio.Queue()

    def reader():
        for raw in sys.stdin.buffer:
            loop.call_soon_threadsafe(inbox.put_nowait, raw)
        loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=reader, name="pm-stdin", daemon=True).start()
    send({"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {
        "protocol": PROTOCOL, "role": "project_manager", "pid": os.getpid(),
        "projects_root": str(PROJECTS_ROOT), "supervisor_dir": str(SUPERVISOR_DIR),
        "tools": registry.export_manifest()}})
    log(f"ready: {len(registry.TOOL_NAMES)} tools, projects root {PROJECTS_ROOT}")

    tasks = set()
    while True:
        raw = await inbox.get()
        if raw is None:
            break
        try:
            msg = json.loads(raw)
        except ValueError:
            log(f"ignoring malformed line: {raw[:200]!r}")
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if method is None:
            if "error" in msg:
                log(f"host rejected hello: {msg['error']}")
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"pong": True}})
        elif method == "tool.call":
            task = asyncio.create_task(handle_call(msg_id, msg.get("params") or {}))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        elif msg_id is not None:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method: {method}"}})
    if tasks:
        await asyncio.wait(tasks, timeout=5)


if __name__ == "__main__":
    asyncio.run(serve())
