# 需求：按节点 id 只读查询，与 `get_joint_info`

状态：**已实现并验证**（2026-11-08，未提交）
来源：Janus B10（协同仿真 MCP）的绑定自查需求。Janus 的 adapter 通过 `Robot.getDevice(name)` 绑定电机和传感器，绑定前 agent 需要用 Webots MCP 核对关节上的 device 名称、axis、stop 和电机上限；这些信息目前读不到。

## 1. 背景与实测现状

在真实 Webots（R2025b，server 2.2.0）里对一个典型的机械臂关节（`DEF ARM_JOINT HingeJoint`，电机和传感器都没有 DEF）实测：

| 想知道的事 | 现状 | 原因 |
|---|---|---|
| 关节下有哪些 device、什么类型 | 能：`get_node_details("ARM_JOINT")` 返回 `device` 子节点的 `id` 和类型 | — |
| device 的 `name`（`getDevice` 用它查找） | **不能** | 电机、传感器没有 DEF，而 `get_node_details` 只接受 DEF、点路径和 `type:<根节点类型>` |
| `HingeJointParameters` 的 axis、minStop、maxStop | **不能** | 同上，没有 DEF |
| 电机的 `maxTorque`、`maxVelocity` | **不能** | 同上 |
| 用 `get_nodes_by_type("RotationalMotor")` 找到电机 | 只能拿到 `id` / `def` / `type`，没有字段值 | 该工具只做定位 |
| 用 `get_motors(robot_def)` | 只能访问 supervisor **自己的 Robot** | Webots 的 Motor API 限制，机械臂这类其它机器人会被拒绝 |
| 用 `get_node_details("id:19")` | 报 `DEF name must match [A-Za-z_][A-Za-z0-9_]*` | 不支持 id |

关键观察：数据本身不缺。对有 DEF 的节点，`get_node_details` 已经能返回 `SFString`、`SFVec3f`、`SFFloat` 等字段的值。**缺的只是定位没有 DEF 的节点的方式**，而场景树、`get_nodes_by_type` 和 `get_node_details` 本来就都会返回节点 `id`，`query.py` 里也已经在用 `supervisor.getFromId`。

## 2. 需求

### R1：只读查询工具接受 `id:<n>` 引用

- `get_node_details`、`get_node_state` 的 `def_name` 参数除了 DEF 名、`PARENT.INNER` 点路径、`type:<NodeType>` 之外，接受 `id:<n>`，`n` 来自 `get_scene_tree`、`get_nodes_by_type`、`get_node_details` 返回的节点 `id`。
- 格式：`id:` 后跟正整数，上限 2^31−1，允许冒号后有空白；其它写法（`id:`、`id:abc`、`id:0`、`id:-3`、`id:+5`、`id:1.5`、非 ASCII 数字、超上限）一律报 `A node id reference must look like 'id:<positive integer>'`。
- 找不到节点（含 `getFromId` 抛异常）：报 `No node with id N. Ids come from get_scene_tree, get_nodes_by_type and get_node_details, and they change when the world is reloaded.`
- **修改类工具保持只认 DEF**（`set_node_field` 等，它们走 `get_node`）。理由：id 在世界重载后会变，一个过期的 id 可能悄悄改到别的节点；而读取过期 id 最坏是读到别的节点，不会破坏场景。
- 现有 DEF、点路径、`type:` 的行为和错误信息不变；参数名 `def_name` 不变，保持兼容。

### R2：新增查询工具 `get_joint_info(def_name)`

只读描述一个 `HingeJoint` / `Hinge2Joint` / `SliderJoint`（`def_name` 也接受 `id:<n>`），一次返回绑定自查需要的全部信息：

- `joint`、`joint_type`：关节节点与类型；
- `axes[]`：每个轴的 `jointParameters`（Hinge2Joint 还有 `jointParameters2`）——节点引用，以及 `axis`、`anchor`、`position`、`minStop`、`maxStop`、`springConstant`、`dampingConstant`、`staticFriction` 的值（节点上没有的字段不输出，例如 `JointParameters` 没有 `anchor`）；
- `devices[]`：关节上全部 device（`device` 对应轴 1，`device2` 对应轴 2），每个含 `id`、`def`、`type`、`axis_index`、`name`；电机另含上限（`RotationalMotor`：`maxTorque`；`LinearMotor`：`maxForce`；都有 `maxVelocity`、`minPosition`、`maxPosition`）；返回的 `id` 可直接用于 `get_node_details("id:<n>")` 查看全部字段；
- `summary`：`motor_names`、`sensor_names`，方便直接和 `project.yaml` 里的 `motor` / `sensor` 比对；
- `end_point`：`endPoint` 节点引用；
- `warnings[]`：只指出事实，不下结论：
  - `jointParameters` 为空（“Webots 使用默认关节参数，未读取任何值”，不猜默认值）；
  - `endPoint` 为空；
  - 同一个轴上有多个电机或多个位置传感器（“按‘自动取第一个’的程序会选第一个”）；
  - device 名称为空（`Robot.getDevice()` 找不到）；
  - 同一关节上 device 名称重复。
- 不是上述三种关节时报错 `X is not a HingeJoint / Hinge2Joint / SliderJoint`。
- 工具属于 `query` 工具集，按现有按需加载机制通过 `tool_catalog` / `tool_describe` / `tool_call` 访问，不进默认列表。

### 非目标

- 不支持按 id 修改节点；
- 不枚举 Robot 直接挂载（不在关节上）的 device——这类 device 用 `get_nodes_by_type` 取 id，再用 R1 的 `id:` 读取字段即可；
- 不判断“绑定是否正确”：`get_joint_info` 只呈现事实，核对由调用方（agent）完成；
- 不改 Rust 宿主：工具清单由 supervisor 通过 `hello` 上报，新增 Python 工具无需重编宿主。

## 3. 实现

| 文件 | 变更 |
|---|---|
| `supervisor/webots_mcp/scene.py` | 新增 `parse_node_id_ref`、`get_node_by_id`、`get_readable_node`（R1）；`describe_joint`、`describe_device`、`_read_field`（R2）。`get_node` 未改 |
| `supervisor/webots_mcp/tools/query.py` | `get_node_details`、`get_node_state` 改用 `get_readable_node`，更新说明；新增 `get_joint_info` |
| `supervisor/webots_mcp/tools/guide.py` | 工作流指南的“查询”分类加入 `get_joint_info`，并说明 `id:<n>` 与修改类工具仍只认 DEF |
| `README.md`、`README.zh-CN.md` | 查询工具表加入 `get_joint_info`；Supervisor 工具数 43 → 44，总数 61 → 62 |
| `tests/test_node_id_and_joint_info.py` | 新增，51 项检查（见下） |
| `tests/e2e.py` | 新增 4 项检查：`tool_catalog` 含 `get_joint_info`、`tool_describe`、端到端的 id 校验、修改类工具仍只认 DEF；另外让测试主体里的异常不再被吞掉（见 §5） |
| `tests/fake/controller.py` | 附带修复：补上 `Node` 类（见 §5） |

未改动：`SERVER_VERSION`（仍为 2.2.0，是否升到 2.3.0 由维护者决定）、`docs/tools.md`（未跟踪文件，且编码已损坏，没有动）。

## 4. 验证

**单元测试（无需 Webots）**：7 个测试文件全部通过，共 125 项检查（`test_node_id_and_joint_info.py` 51 项）。新增测试覆盖：

- `id:` 解析的合法、非法写法；未知 id、`getFromId` 抛异常；
- `get_readable_node` 对 id、DEF、`type:` 三种引用；
- `get_node`（修改类工具使用）和 `set_node_field` 仍拒绝 `id:`；
- `get_node_details` / `get_node_state` 按 id 读取没有 DEF 的电机和节点；
- `get_joint_info`：HingeJoint 正常、SliderJoint 且 `jointParameters` 为空、同轴多电机多传感器、空名和重名、`endPoint` 为空、Hinge2Joint 的 `device2` 与 `jointParameters2`、非关节节点、未知 DEF、以 id 引用关节。

**端到端（真实 Rust 宿主 + 真实 supervisor 代码 + 假 Webots）**：整个文件完整跑完，41 项通过；新增的 4 项全部通过；`FAILED: 2`，这 2 项是已有的过期断言，见 §5。

**真实 Webots（R2025b）**，Janus 的 `B_tuned` 世界，supervisor 加载的是本仓库源码：

- `tool_catalog(query)` 列出 `get_joint_info`，`get_node_details` 的说明已更新；
- `get_joint_info("ARM_JOINT")` 返回：`HingeJoint`；axis `[0, 0, 1]`；`dampingConstant 0.0005`；电机 `arm_motor`（`maxTorque 5.0`、`maxVelocity 10.0`）；位置传感器 `arm_sensor`；`endPoint` 为 `ARM_LINK`；`warnings` 为空——与世界文件一致；
- `get_node_details("id:19")` 读出没有 DEF 的电机的全部字段（`name: "arm_motor"`、`maxTorque: 5.0`……）；
- `get_node_details("id:99999")` 返回“No node with id 99999 …”；
- `set_node_field("id:19", …)` 被拒绝（`DEF name must match …`），场景未被修改。

## 5. 附带发现（与本需求无关，已有问题）

1. **测试桩缺 `Node`**：`tools/motors.py` 开头 `from controller import Node`，但 `tests/fake/controller.py` 没有 `Node`，导致 `test_contact_points`、`test_joints_and_snapshots`、`test_screenshot` 三个文件在导入阶段就失败（电机工具加入后没有更新测试桩）。已补上 `Node.ROTATIONAL_MOTOR` / `Node.LINEAR_MOTOR`，这三个文件现在通过。
2. **e2e 有 2 项过期断言**：`native + project_manager tools listed before Webots starts` 和 `real supervisor tools exposed` 仍在期待“全部工具都在 `tools/list` 里”，而按需加载之后只列核心工具加 `tool_catalog` / `tool_describe` / `tool_call`。在干净的 `HEAD` 副本上同样失败，和本次改动无关，这里没有改。建议另行更新：断言应改为核心工具集合，其余工具通过 `tool_catalog` 检查。

3. **`e2e.py` 会吞掉测试主体里的异常**：`try: … finally:` 的 `finally` 末尾是 `sys.exit(…)`，测试主体抛出的异常会被它替换掉，既没有 traceback，后面的检查也都不再执行，只看到一个偏小的 `FAILED` 数。本次开发中就因此漏掉过一个参数冲突的错误（后面约 30 项检查被悄悄跳过）。已在 `finally` 前加 `except Exception: traceback.print_exc(); failed += 1`，异常现在会打印并计为失败。

## 6. 已知限制

- id 在世界重载后会变，也不跨会话稳定；调用方应在同一次查询流程里先取 id 再用。
- `get_joint_info` 只覆盖挂在关节 `device` / `device2` 上的 device；PROTO 内部的关节需要用点路径（`INSTANCE.INNER`）或 `id:`。
- 电机上限字段按 Webots 的 `RotationalMotor` / `LinearMotor` 读取；自定义 PROTO 如果改写了这些字段的暴露方式，读不到的字段会被省略，不会报错。
- 对 `Brake` 等其它 device 只给出 `id`、`def`、`type`、`name`。
