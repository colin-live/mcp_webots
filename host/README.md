# webots-mcp-host

长驻主进程：对 agent 提供 **MCP（stdio）**，负责 **Webots 进程与仿真时钟**，并通过 **IPC** 与唯一的 Supervisor 通信，把 Supervisor 的工具动态暴露出去。

```
agent ──MCP/stdio──▶ webots-mcp-host (Rust) ──TCP 127.0.0.1:47800, NDJSON──▶ Supervisor (Python, 在 Webots 内)
                       │  启动/停止 Webots 进程                                 场景 / 物体 / 仿真数据工具
                       │  暂停/继续/单步/重置 策略
                       ▼
                    webots.exe --mode=pause --batch <world.wbt>
```

## 构建与运行

```
cargo build --release
set WEBOTS_MCP_WORLD=D:\...\worlds\your.wbt     # 可选，sim_start 的默认世界
set WEBOTS_HOME=C:\Program Files\Webots          # 或 WEBOTS_BIN 指向 webots.exe
target\release\webots-mcp-host.exe
```

MCP 客户端配置里把它当作一个 stdio 服务器即可。日志写 stderr（`WEBOTS_MCP_LOG=debug` 调整级别），**stdout 只放协议消息**。

| 环境变量 / 参数 | 含义 | 默认 |
|---|---|---|
| `WEBOTS_MCP_IPC` / `--ipc-addr` | 主进程监听、并告诉 Webots 的地址 | `127.0.0.1:47800` |
| `WEBOTS_MCP_TOKEN` | 设置后 supervisor 的 hello 必须带同样的 token | 无 |
| `WEBOTS_BIN` / `WEBOTS_HOME` | Webots 可执行文件 | 自动探测 |
| `WEBOTS_MCP_WORLD` / `--world` | 默认世界 | 无 |
| `WEBOTS_MCP_STATE_DIR` | 工具清单缓存目录 | `%LOCALAPPDATA%\webots-mcp-host` |
| `WEBOTS_MCP_WEBOTS_ARGS` | 额外的 Webots 命令行参数 | 空 |
| `WEBOTS_MCP_AUTO_RESUME=0` / `--no-auto-resume` | run_simulation 遇到暂停时不自动恢复 | 自动恢复 |

## supervisor 的工具如何暴露

1. Python 侧的 `@tool` 装饰器只登记函数，并从类型注解生成 JSON Schema（无第三方依赖）。
2. Supervisor 连上后发 `hello`，里面带完整 `tools[]` 清单（name / description / inputSchema）。
3. 主进程把它与自己的 `sim_*` 工具合并成 `tools/list`；清单变化时发 `notifications/tools/list_changed`，并缓存到 `supervisor_tools.json`，所以 Webots 还没启动时也能看到工具。
4. `tools/call` 命中 supervisor 工具时，主进程转成 `tool.call` 发给 Supervisor，按 id 关联回复；Supervisor 断开时所有在途请求立即失败（不会挂到超时）。
5. 新增 / 修改 supervisor 工具不需要改 Rust。冷启动前可用 `python export_manifest.py <state_dir>\supervisor_tools.json` 预生成清单。

## 谁管什么

- **主进程**：进程生命周期（`sim_start/stop/restart`）、时钟策略（`sim_pause/resume/step/reset/status`）、超时、`run_simulation` 遇暂停时自动恢复并在结束后重新暂停。
- **Supervisor**：只执行时钟原语（`sim.control`），其余都是场景/物体/数据工具。暂停期间主循环调用 `step(0)`，所以暂停时场景工具照常可用；阻塞的 `step(dt)` 永远不会在暂停时被调用。
- `set_simulation_speed` 已由 `sim_resume(mode)` 取代（在 `registry.HOST_OWNED` 中隐藏）。

## IPC 协议 v1（每行一个 JSON，JSON-RPC 2.0 形状）

```
supervisor → host  request  hello       {protocol:1, token, pid, world_path, state, mode, sim_time, tools[]}
supervisor → host  notify   state       {state, mode, sim_time, ...}
host → supervisor  request  tool.call   {name, arguments}  → {text}
host → supervisor  request  sim.control {op: status|pause|resume|step|reset_physics|reset|reload|quit, ...}
host → supervisor  request  ping
```

`project_manager` 之后可以复用同一套 `hello + 清单 + tool.call`，区别只是由主进程 spawn、走子进程 stdio。

## 需要在真实 Webots 上确认的几点

1. `simulationReset()` 之后 Webots 里的节点句柄是否仍有效（代码已重新获取 root 句柄，并清掉 `created_ids` 等缓存）。
2. `sim_step` 的"精确 N 步"：依赖仅有 supervisor 一个同步控制器在推进；有其他 `synchronization=TRUE` 的机器人时仍然成立，但 `FALSE` 的不保证。
3. 若 `webots.exe` 只是启动器、拉起真正主程序后立即退出：把 `WEBOTS_BIN` 指向真正的主程序。主进程对退出码 0 的提前退出会继续等待 supervisor 连接。
4. 主进程被强杀时 Webots 子进程不会随之退出（Windows 需要 Job Object，尚未实现）；正常关闭 stdin / Ctrl-C 会先 `sim_stop`。

## project_manager（独立进程）

主进程启动时 spawn 一个 Python 子进程（`controllers\mcp_server\project_manager.py`），走子进程 stdin/stdout，协议与 supervisor 相同（`hello` 带工具清单 + `tool.call`）。它不依赖 Webots，Webots 没启动也能用；崩溃会自动重启（退避 0.5s→10s）。

启用：设置 `WEBOTS_MCP_SUPERVISOR_DIR` 指向 `controllers\mcp_server`（或 `WEBOTS_MCP_PM_SCRIPT` 直接指脚本）。

| 环境变量 / 参数 | 含义 | 默认 |
|---|---|---|
| `WEBOTS_MCP_SUPERVISOR_DIR` / `--supervisor-dir` | 共享 supervisor 代码目录，也是 project_manager 所在目录 | 无（不启用） |
| `WEBOTS_MCP_PM_SCRIPT` / `--pm-script` | project_manager.py 的路径 | `<supervisor dir>\project_manager.py` |
| `WEBOTS_MCP_PYTHON` | 运行它的 Python | Windows `python`，其他 `python3` |
| `WEBOTS_PROJECTS_DIR` / `--projects-dir` | 项目根目录 | 脚本上溯三级（`...\webots_projects`） |
| `WEBOTS_MCP_WORLD_VERSION` | 新世界的 `#VRML_SIM` 版本 | 取自已有世界 → `WEBOTS_HOME\resources\version.txt` → R2025a |
| `WEBOTS_HOME` | 用于按名字查找 Webots 自带机器人 PROTO | 无 |

工具（都在 `tools/list` 里，与 supervisor 工具、`sim_*` 一起出现）：

| 工具 | 作用 |
|---|---|
| `project_create` | 一步建好可运行项目：目录、project.json、世界（ENU、背景、光照、地面）、supervisor 机器人和控制器；返回 `world_path` |
| `world_add_robot` | 把机器人嵌入 `.wbt`：自动补 EXTERNPROTO、追加节点，可同时写控制器源码 |
| `world_remove_node` | 按 DEF 删除根级节点 |
| `supervisor_bind` | 给已有项目/世界绑定 supervisor（`link` 启动器 / `copy` 复制代码），幂等 |
| `project_list` / `project_info` / `project_check` / `project_delete` | 列出 / 概况 / 静态检查 / 删除（需重复项目名确认） |
| `project_read_file` / `project_write_file` | 项目内文本文件读写，带越界保护 |

典型流程：`project_create` → `world_add_robot` → `sim_start(world=<world_path>)` → 构建/实验工具 → `save_world`。

supervisor 绑定默认是 **link**：项目里只放一个几行的启动器，真正的代码始终是 `controllers\mcp_server` 那一份，改一次所有项目生效。启动器会把 `WEBOTS_MCP_SAVE_DIR` 默认设为 `<项目>\data`。
