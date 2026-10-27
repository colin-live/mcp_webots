//! MCP server over stdio (newline-delimited JSON-RPC 2.0).
//!
//! stdout carries protocol messages only; all logging goes to stderr.
//! The tool list is dynamic: native `sim_*` tools + whatever the connected
//! Supervisor announced in its `hello` (cached on disk between runs).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{broadcast, mpsc};

use crate::ipc::SupervisorLink;
use crate::sim::SimController;

const SUPPORTED_VERSIONS: [&str; 4] = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

const INSTRUCTIONS: &str = "Webots 仿真控制。先 sim_start 启动并加载世界（默认暂停，暂停时也能搭建场景），\
再用查询/构建/修改/物理类工具编辑场景，sim_resume / sim_step 推进仿真，run_simulation 做实验，\
sim_reset / sim_restart 回到初始状态。工具列表会在 supervisor 连接后更新。";

type RpcError = (i64, String);

struct Server {
    sim: Arc<SimController>,
    link: Arc<SupervisorLink>,
    pm: Arc<SupervisorLink>,
    out: mpsc::UnboundedSender<String>,
    initialized: AtomicBool,
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

    let server = Arc::new(Server { sim, link: link.clone(), pm: pm.clone(), out, initialized: AtomicBool::new(false) });

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

    fn list_tools(&self) -> Vec<Value> {
        let mut tools = SimController::native_tools();
        let mut seen: Vec<String> = tools.iter().filter_map(|t| t["name"].as_str().map(str::to_owned)).collect();
        // Order of precedence on a name clash: host > supervisor > project manager.
        for (source, label) in [(&self.link, "supervisor"), (&self.pm, "project_manager")] {
            for remote in source.tools().iter() {
                let Some(name) = remote.get("name").and_then(Value::as_str) else { continue };
                if seen.iter().any(|n| n == name) {
                    tracing::warn!("{label} tool {name} clashes with an earlier tool; hidden");
                    continue;
                }
                seen.push(name.to_owned());
                tools.push(json!({
                    "name": name,
                    "description": remote.get("description").cloned().unwrap_or(Value::Null),
                    "inputSchema": remote.get("inputSchema").cloned().unwrap_or_else(|| json!({"type": "object"})),
                }));
            }
        }
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

        let outcome = if let Some(outcome) = self.sim.call_native(name, &args).await {
            outcome
        } else if has_tool(&self.link, name) {
            self.sim.call_remote(name, args).await
        } else if has_tool(&self.pm, name) {
            crate::sim::call_plain(&self.pm, name, args, Duration::from_secs(60)).await
        } else {
            return Err((-32602, format!("Unknown tool: {name}")));
        };

        Ok(json!({"content": [{"type": "text", "text": outcome.text}], "isError": outcome.is_error}))
    }
}

fn has_tool(link: &SupervisorLink, name: &str) -> bool {
    link.tools().iter().any(|t| t.get("name").and_then(Value::as_str) == Some(name))
}
