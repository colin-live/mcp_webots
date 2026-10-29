//! MCP server over stdio (newline-delimited JSON-RPC 2.0).
//!
//! stdout carries protocol messages only; all logging goes to stderr.
//! The tool list is dynamic: native `sim_*` tools + whatever the connected
//! Supervisor announced in its `hello` (cached on disk between runs).
//!
//! To keep the client's context small only a core set of tools is listed by
//! default (plus the toolsets named in `WEBOTS_MCP_TOOLSETS`, or everything for
//! `all`). Every other tool stays reachable through three meta tools:
//! `tool_catalog` (discover), `tool_describe` (schema) and `tool_call` (invoke).
//! Hidden tools can also be called directly by name.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{broadcast, mpsc};

use crate::ipc::SupervisorLink;
use crate::sim::{Outcome, SimController};

const SUPPORTED_VERSIONS: [&str; 4] = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

const INSTRUCTIONS: &str = "Webots 仿真控制。先 sim_start 启动并加载世界（默认暂停，暂停时也能搭建场景），\
再用查询/构建/修改/物理类工具编辑场景，sim_resume / sim_step 推进仿真，run_simulation 做实验，\
sim_reset / sim_restart 回到初始状态。工具列表会在 supervisor 连接后更新。\
为节省上下文，默认只列出常用工具；其余工具（物理、关节电机、快照、截图、项目管理等）先用 tool_catalog 查看，\
tool_describe 查参数，再用 tool_call 调用。";

/// Always listed, whatever `WEBOTS_MCP_TOOLSETS` says.
const CORE_TOOLS: &[&str] = &[
    "sim_start",
    "sim_stop",
    "sim_pause",
    "sim_resume",
    "sim_step",
    "sim_status",
    "project_create",
    "get_scene_tree",
    "get_node_details",
    "create_primitive",
    "create_component",
    "set_node_field",
    "run_simulation",
];

const META_TOOLS: &[&str] = &["tool_catalog", "tool_describe", "tool_call"];

/// Which tools `tools/list` shows.
#[derive(Debug, Clone, PartialEq)]
enum Exposure {
    /// Every tool (old behaviour).
    All,
    /// Core tools plus the named toolsets.
    Core(HashSet<String>),
}

impl Exposure {
    /// Parse `WEBOTS_MCP_TOOLSETS`: comma separated toolset names, or `all`.
    fn parse(raw: Option<&str>) -> Exposure {
        let mut sets = HashSet::new();
        for part in raw.unwrap_or("").split(',') {
            let part = part.trim().to_ascii_lowercase();
            if part.is_empty() {
                continue;
            }
            if part == "all" {
                return Exposure::All;
            }
            sets.insert(part);
        }
        Exposure::Core(sets)
    }

    fn shows(&self, name: &str, toolset: &str) -> bool {
        match self {
            Exposure::All => true,
            Exposure::Core(sets) => CORE_TOOLS.contains(&name) || sets.contains(toolset),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Origin {
    Native,
    Supervisor,
    ProjectManager,
}

/// One tool known to the host, wherever it is implemented.
struct Entry {
    name: String,
    toolset: String,
    tool: Value,
}

impl Entry {
    fn description(&self) -> &str {
        self.tool.get("description").and_then(Value::as_str).unwrap_or("")
    }
}

/// `[查询] ...` -> toolset used when the provider did not announce one.
fn category_toolset(description: &str) -> &'static str {
    let category = description
        .trim_start()
        .strip_prefix('[')
        .and_then(|rest| rest.split_once(']'))
        .map(|(c, _)| c.trim());
    match category {
        Some("查询") => "query",
        Some("构建") => "build",
        Some("修改") => "modify",
        Some("物理") => "physics",
        Some("实验") => "experiment",
        Some("复位") => "snapshots",
        Some("输出") => "output",
        Some("指南") => "guide",
        Some("时钟") => "clock",
        Some("项目") | Some("世界") | Some("绑定") => "project",
        _ => "other",
    }
}

fn toolset_for(origin: Origin, remote: &Value, description: &str) -> String {
    match origin {
        Origin::Native => "clock".to_owned(),
        Origin::ProjectManager => "project".to_owned(),
        Origin::Supervisor => remote
            .get("toolset")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| category_toolset(description).to_owned()),
    }
}

/// One short line for `tool_catalog`: description without the `[分类]` prefix,
/// cut at the first sentence and at 70 characters.
fn summarize(description: &str) -> String {
    let body = description.trim_start();
    let body = match body.strip_prefix('[') {
        Some(rest) => rest.split_once(']').map(|(_, r)| r).unwrap_or(rest),
        None => body,
    };
    let first_line = body.trim().lines().next().unwrap_or("");
    let sentence = first_line.split('。').next().unwrap_or(first_line).trim();
    let mut out: String = sentence.chars().take(70).collect();
    if sentence.chars().count() > 70 {
        out.push('…');
    }
    out
}

/// Names listed in `required` that `args` does not contain.
fn missing_required(schema: &Value, args: &Value) -> Vec<String> {
    let Some(required) = schema.get("required").and_then(Value::as_array) else { return Vec::new() };
    required
        .iter()
        .filter_map(Value::as_str)
        .filter(|key| args.get(*key).is_none())
        .map(str::to_owned)
        .collect()
}

fn meta_tools() -> Vec<Value> {
    vec![
        json!({
            "name": "tool_catalog",
            "description": "列出所有可用工具，包括默认没有列出的。不带参数：返回各工具集及其工具名；\
                            toolset=<名字>：返回该工具集里每个工具的一句话说明。查参数用 tool_describe，调用用 tool_call。",
            "inputSchema": {"type": "object", "properties": {
                "toolset": {"type": "string", "description": "工具集名，例如 physics、motors、snapshots、project"}
            }}
        }),
        json!({
            "name": "tool_describe",
            "description": "返回某个工具的完整说明和参数 schema（包括默认没有列出的工具）。",
            "inputSchema": {"type": "object", "properties": {
                "name": {"type": "string", "description": "工具名"}
            }, "required": ["name"]}
        }),
        json!({
            "name": "tool_call",
            "description": "调用任意工具（包括默认没有列出的），行为与直接调用相同。先用 tool_describe 确认参数。",
            "inputSchema": {"type": "object", "properties": {
                "name": {"type": "string", "description": "工具名"},
                "arguments": {"type": "object", "description": "传给该工具的参数"}
            }, "required": ["name"]}
        }),
    ]
}

type RpcError = (i64, String);

struct Server {
    sim: Arc<SimController>,
    link: Arc<SupervisorLink>,
    pm: Arc<SupervisorLink>,
    out: mpsc::UnboundedSender<String>,
    initialized: AtomicBool,
    exposure: Exposure,
}

pub async fn run(sim: Arc<SimController>, link: Arc<SupervisorLink>, pm: Arc<SupervisorLink>) -> Result<()> {
    let (out, mut out_rx) = mpsc::unbounded_channel::<String>();

    // Single writer keeps messages whole and ordered.
    let writer = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(line) = out_rx.recv().await {
            let ok = stdout.write_all(line.as_bytes()).await.is_ok()
                && stdout.write_all(b"\n").await.is_ok()
                && stdout.flush().await.is_ok();
            if !ok {
                break;
            }
        }
    });

    let exposure = Exposure::parse(std::env::var("WEBOTS_MCP_TOOLSETS").ok().as_deref());
    tracing::info!("tool exposure: {exposure:?}");
    let server = Arc::new(Server {
        sim,
        link: link.clone(),
        pm: pm.clone(),
        out,
        initialized: AtomicBool::new(false),
        exposure,
    });

    // Tell the client when either provider changed the tool list.
    for source in [link.clone(), pm.clone()] {
        let server = server.clone();
        let mut changed = source.subscribe_tools();
        tokio::spawn(async move {
            loop {
                match changed.recv().await {
                    Ok(()) | Err(broadcast::error::RecvError::Lagged(_)) => {
                        if server.initialized.load(Ordering::Relaxed) {
                            server.send(json!({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}));
                        }
                    }
                    Err(broadcast::error::RecvError::Closed) => break,
                }
            }
        });
    }

    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(&line) {
            Ok(msg) => {
                // One task per message: a long run_simulation must not block ping.
                tokio::spawn(server.clone().handle(msg));
            }
            Err(e) => {
                tracing::warn!("invalid JSON on stdin: {e}");
                server.send(json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "parse error"}}));
            }
        }
    }

    // Client closed stdin: let queued replies drain, then stop.
    drop(server);
    let _ = tokio::time::timeout(std::time::Duration::from_millis(300), writer).await;
    Ok(())
}

impl Server {
    fn send(&self, value: Value) {
        let _ = self.out.send(value.to_string());
    }

    async fn handle(self: Arc<Self>, msg: Value) {
        let method = msg.get("method").and_then(Value::as_str).map(str::to_owned);
        let id = msg.get("id").cloned();

        match (method, id) {
            (Some(method), Some(id)) => {
                let reply = match self.dispatch(&method, msg.get("params")).await {
                    Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
                    Err((code, message)) => json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}),
                };
                self.send(reply);
            }
            (Some(method), None) => {
                if method == "notifications/initialized" {
                    self.initialized.store(true, Ordering::Relaxed);
                }
            }
            _ => {} // responses to requests we never send
        }
    }

    async fn dispatch(&self, method: &str, params: Option<&Value>) -> Result<Value, RpcError> {
        match method {
            "initialize" => {
                let wanted = params.and_then(|p| p.get("protocolVersion")).and_then(Value::as_str).unwrap_or("");
                let version = if SUPPORTED_VERSIONS.contains(&wanted) { wanted } else { SUPPORTED_VERSIONS[1] };
                Ok(json!({
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": true}},
                    "serverInfo": {"name": "webots-mcp-host", "version": env!("CARGO_PKG_VERSION")},
                    "instructions": INSTRUCTIONS,
                }))
            }
            "ping" => Ok(json!({})),
            "tools/list" => Ok(json!({"tools": self.list_tools()})),
            "tools/call" => self.call_tool(params).await,
            _ => Err((-32601, format!("method not found: {method}"))),
        }
    }

    /// Every tool the host can serve, listed or not.
    fn catalog(&self) -> Vec<Entry> {
        let mut entries: Vec<Entry> = Vec::new();
        for tool in SimController::native_tools() {
            let Some(name) = tool.get("name").and_then(Value::as_str).map(str::to_owned) else { continue };
            let description = tool.get("description").and_then(Value::as_str).unwrap_or("");
            let toolset = toolset_for(Origin::Native, &tool, description);
            entries.push(Entry { name, toolset, tool });
        }
        // Order of precedence on a name clash: host > supervisor > project manager.
        for (source, origin, label) in [
            (&self.link, Origin::Supervisor, "supervisor"),
            (&self.pm, Origin::ProjectManager, "project_manager"),
        ] {
            for remote in source.tools().iter() {
                let Some(name) = remote.get("name").and_then(Value::as_str) else { continue };
                if entries.iter().any(|e| e.name == name) || META_TOOLS.contains(&name) {
                    tracing::warn!("{label} tool {name} clashes with an earlier tool; hidden");
                    continue;
                }
                let description = remote.get("description").and_then(Value::as_str).unwrap_or("");
                let toolset = toolset_for(origin, remote, description);
                let tool = json!({
                    "name": name,
                    "description": remote.get("description").cloned().unwrap_or(Value::Null),
                    "inputSchema": remote.get("inputSchema").cloned().unwrap_or_else(|| json!({"type": "object"})),
                });
                entries.push(Entry { name: name.to_owned(), toolset, tool });
            }
        }
        entries
    }

    fn list_tools(&self) -> Vec<Value> {
        let mut tools: Vec<Value> = self
            .catalog()
            .into_iter()
            .filter(|e| self.exposure.shows(&e.name, &e.toolset))
            .map(|e| e.tool)
            .collect();
        tools.extend(meta_tools());
        tools
    }

    async fn call_tool(&self, params: Option<&Value>) -> Result<Value, RpcError> {
        let params = params.ok_or((-32602, "missing params".to_owned()))?;
        let name = params.get("name").and_then(Value::as_str).ok_or((-32602, "missing tool name".to_owned()))?;
        let args = match params.get("arguments") {
            None | Some(Value::Null) => json!({}),
            Some(v @ Value::Object(_)) => v.clone(),
            Some(_) => return Err((-32602, "arguments must be an object".to_owned())),
        };

        let outcome = match name {
            "tool_catalog" => self.meta_catalog(&args),
            "tool_describe" => self.meta_describe(&args),
            "tool_call" => self.meta_call(&args).await,
            _ => self.call_named(name, args).await?,
        };

        Ok(json!({"content": [{"type": "text", "text": outcome.text}], "isError": outcome.is_error}))
    }

    /// Route a call to whoever implements `name` (listed or not).
    async fn call_named(&self, name: &str, args: Value) -> Result<Outcome, RpcError> {
        if let Some(outcome) = self.sim.call_native(name, &args).await {
            Ok(outcome)
        } else if has_tool(&self.link, name) {
            Ok(self.sim.call_remote(name, args).await)
        } else if has_tool(&self.pm, name) {
            Ok(crate::sim::call_plain(&self.pm, name, args, Duration::from_secs(60)).await)
        } else {
            Err((-32602, format!("Unknown tool: {name}")))
        }
    }

    // ------------------------------------------------------------
    // Meta tools: discover / describe / call tools that are not listed
    // ------------------------------------------------------------

    fn meta_catalog(&self, args: &Value) -> Outcome {
        let entries = self.catalog();
        let wanted = args.get("toolset").and_then(Value::as_str).map(|s| s.trim().to_ascii_lowercase());
        match wanted.filter(|s| !s.is_empty()) {
            None => {
                let mut groups: BTreeMap<&str, Vec<&Entry>> = BTreeMap::new();
                for e in &entries {
                    groups.entry(e.toolset.as_str()).or_default().push(e);
                }
                let toolsets: Vec<Value> = groups
                    .iter()
                    .map(|(toolset, items)| {
                        json!({
                            "toolset": toolset,
                            "count": items.len(),
                            "tools": items.iter().map(|e| e.name.as_str()).collect::<Vec<_>>(),
                        })
                    })
                    .collect();
                Outcome::ok(json!({
                    "toolsets": toolsets,
                    "always_listed": CORE_TOOLS,
                    "hint": "toolset=<名字> 查看一句话说明；tool_describe 查参数；tool_call 调用。",
                }))
            }
            Some(toolset) => {
                let items: Vec<Value> = entries
                    .iter()
                    .filter(|e| e.toolset == toolset)
                    .map(|e| {
                        json!({
                            "name": e.name,
                            "summary": summarize(e.description()),
                            "listed": self.exposure.shows(&e.name, &e.toolset),
                        })
                    })
                    .collect();
                if items.is_empty() {
                    let known: BTreeSet<&str> = entries.iter().map(|e| e.toolset.as_str()).collect();
                    let hint = format!("可用工具集：{}", known.into_iter().collect::<Vec<_>>().join(", "));
                    Outcome::err("UnknownToolset", format!("没有名为 {toolset} 的工具集。"), Some(hint.as_str()))
                } else {
                    Outcome::ok(json!({"toolset": toolset, "tools": items}))
                }
            }
        }
    }

    fn unknown_tool(&self, name: &str, entries: &[Entry]) -> Outcome {
        let needle = name.to_ascii_lowercase();
        let similar: Vec<&str> = entries
            .iter()
            .filter(|e| !needle.is_empty() && (e.name.contains(&needle) || needle.contains(&e.name)))
            .map(|e| e.name.as_str())
            .take(8)
            .collect();
        let hint = if similar.is_empty() {
            "用 tool_catalog 查看所有工具。".to_owned()
        } else {
            format!("相近的工具：{}", similar.join(", "))
        };
        Outcome::err("UnknownTool", format!("没有名为 {name} 的工具。"), Some(hint.as_str()))
    }

    fn meta_describe(&self, args: &Value) -> Outcome {
        let Some(name) = args.get("name").and_then(Value::as_str) else {
            return Outcome::err("InvalidArguments", "缺少参数 name。", None);
        };
        let entries = self.catalog();
        match entries.iter().find(|e| e.name == name) {
            Some(e) => Outcome::ok(json!({
                "name": e.name,
                "toolset": e.toolset,
                "listed": self.exposure.shows(&e.name, &e.toolset),
                "description": e.description(),
                "inputSchema": e.tool.get("inputSchema").cloned().unwrap_or_else(|| json!({"type": "object"})),
            })),
            None => self.unknown_tool(name, &entries),
        }
    }

    async fn meta_call(&self, args: &Value) -> Outcome {
        let Some(name) = args.get("name").and_then(Value::as_str) else {
            return Outcome::err("InvalidArguments", "缺少参数 name。", None);
        };
        if META_TOOLS.contains(&name) {
            return Outcome::err("InvalidArguments", format!("{name} 可以直接调用，不需要经过 tool_call。"), None);
        }
        let inner = match args.get("arguments") {
            None | Some(Value::Null) => json!({}),
            Some(v @ Value::Object(_)) => v.clone(),
            Some(_) => return Outcome::err("InvalidArguments", "arguments 必须是对象。", None),
        };

        let entries = self.catalog();
        let Some(entry) = entries.iter().find(|e| e.name == name) else {
            return self.unknown_tool(name, &entries);
        };
        let schema = entry.tool.get("inputSchema").cloned().unwrap_or_else(|| json!({"type": "object"}));
        let missing = missing_required(&schema, &inner);
        if !missing.is_empty() {
            return Outcome::err(
                "InvalidArguments",
                format!("{name} 缺少必填参数：{}。", missing.join(", ")),
                Some("用 tool_describe 查看参数。"),
            );
        }
        match self.call_named(name, inner).await {
            Ok(outcome) => outcome,
            Err((_, message)) => Outcome::err("UnknownTool", message, Some("用 tool_catalog 查看所有工具。")),
        }
    }
}

fn has_tool(link: &SupervisorLink, name: &str) -> bool {
    link.tools().iter().any(|t| t.get("name").and_then(Value::as_str) == Some(name))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(names: &[&str]) -> HashSet<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn exposure_defaults_to_core_only() {
        let e = Exposure::parse(None);
        assert_eq!(e, Exposure::Core(HashSet::new()));
        assert!(e.shows("sim_start", "clock"));
        assert!(!e.shows("apply_force", "physics"));
        assert!(!e.shows("sim_reset", "clock"));
    }

    #[test]
    fn exposure_adds_named_toolsets_case_insensitively() {
        let e = Exposure::parse(Some(" Physics, motors ,,"));
        assert_eq!(e, Exposure::Core(set(&["physics", "motors"])));
        assert!(e.shows("apply_force", "physics"));
        assert!(e.shows("get_motors", "motors"));
        assert!(!e.shows("save_snapshot", "snapshots"));
    }

    #[test]
    fn exposure_all_wins() {
        let e = Exposure::parse(Some("physics, ALL"));
        assert_eq!(e, Exposure::All);
        assert!(e.shows("anything", "whatever"));
    }

    #[test]
    fn toolset_comes_from_provider_then_category() {
        let with = json!({"name": "get_motors", "toolset": "motors"});
        assert_eq!(toolset_for(Origin::Supervisor, &with, "[修改] x"), "motors");
        let without = json!({"name": "get_scene_tree"});
        assert_eq!(toolset_for(Origin::Supervisor, &without, "[查询] x"), "query");
        assert_eq!(toolset_for(Origin::Supervisor, &without, "no category"), "other");
        assert_eq!(toolset_for(Origin::Native, &without, "[时钟] x"), "clock");
        assert_eq!(toolset_for(Origin::ProjectManager, &with, "[项目] x"), "project");
    }

    #[test]
    fn summary_drops_category_and_first_sentence_only() {
        assert_eq!(summarize("[物理] 对 Solid 施加力 (N)，只作用一个物理步。第二句。"), "对 Solid 施加力 (N)，只作用一个物理步");
        assert_eq!(summarize("no prefix\nsecond line"), "no prefix");
        let long = format!("[查询] {}", "字".repeat(100));
        let s = summarize(&long);
        assert_eq!(s.chars().count(), 71);
        assert!(s.ends_with('…'));
    }

    #[test]
    fn required_arguments_are_checked() {
        let schema = json!({"type": "object", "required": ["def_name", "position"]});
        assert_eq!(missing_required(&schema, &json!({"def_name": "J1"})), vec!["position".to_owned()]);
        assert!(missing_required(&schema, &json!({"def_name": "J1", "position": 0})).is_empty());
        assert!(missing_required(&json!({"type": "object"}), &json!({})).is_empty());
    }

    #[test]
    fn meta_tools_are_well_formed_and_not_core() {
        let names: Vec<String> = meta_tools().iter().filter_map(|t| t["name"].as_str().map(str::to_owned)).collect();
        assert_eq!(names, META_TOOLS);
        for t in meta_tools() {
            assert_eq!(t["inputSchema"]["type"], "object");
        }
        assert!(CORE_TOOLS.iter().all(|c| !META_TOOLS.contains(c)));
    }
}
