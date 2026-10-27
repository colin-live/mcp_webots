//! Simulation management: the host-side policy.
//!
//! The host decides *when* the world runs (start, pause, resume, step, reset,
//! restart, stop) and keeps Webots alive; the Supervisor only executes the
//! primitive clock calls (`sim.control`) and serves scene/object/data tools.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::sync::Mutex;

use crate::config::Config;
use crate::ipc::{LinkError, SessionInfo, SupervisorLink};
use crate::proc::WebotsProc;

/// Result of a tool call, as MCP wants it.
pub struct Outcome {
    pub text: String,
    pub is_error: bool,
}

impl Outcome {
    pub fn ok(mut value: Value) -> Outcome {
        if let Some(obj) = value.as_object_mut() {
            obj.entry("status").or_insert_with(|| json!("success"));
        }
        Outcome { text: value.to_string(), is_error: false }
    }

    pub fn err(kind: &str, message: impl Into<String>, hint: Option<&str>) -> Outcome {
        let mut v = json!({"status": "error", "error_type": kind, "message": message.into()});
        if let Some(h) = hint {
            v["hint"] = json!(h);
        }
        Outcome { text: v.to_string(), is_error: true }
    }
}

fn link_err(e: &LinkError) -> Outcome {
    match e {
        LinkError::NotConnected => Outcome::err(
            "SupervisorNotConnected",
            "Webots 未运行，或 supervisor 控制器尚未连接。",
            Some("先调用 sim_start 启动 Webots 并加载世界。"),
        ),
        LinkError::Disconnected => Outcome::err(
            "SupervisorDisconnected",
            "supervisor 在回复前断开（Webots 退出或世界被重载）。",
            Some("调用 sim_status 查看状态；必要时 sim_start / sim_restart。"),
        ),
        LinkError::Timeout(_) => Outcome::err(
            "SupervisorTimeout",
            e.to_string(),
            Some("操作可能仍在执行：先用查询类工具确认状态再重试会修改场景的操作。"),
        ),
        LinkError::Remote { code, message } => match *code {
            -32601 => Outcome::err("UnknownTool", message.clone(), Some("调用 tools/list 刷新工具列表。")),
            -32602 => Outcome::err("InvalidArguments", message.clone(), None),
            _ => Outcome::err("SupervisorError", message.clone(), None),
        },
    }
}

/// Forward a tool call to a provider that has no clock policy (project manager).
pub async fn call_plain(link: &SupervisorLink, name: &str, args: Value, timeout: Duration) -> Outcome {
    match link.call("tool.call", json!({"name": name, "arguments": args}), timeout).await {
        Ok(v) => finish_remote(v.get("text").and_then(Value::as_str).unwrap_or("").to_owned(), false, false),
        Err(LinkError::NotConnected) | Err(LinkError::Disconnected) => Outcome::err(
            "ProviderUnavailable",
            "项目管理进程未连接（可能正在重启）。",
            Some("稍后重试；如果持续失败，查看主进程日志中 [project_manager] 行。"),
        ),
        Err(e) => link_err(&e),
    }
}

fn absolute(p: &Path) -> PathBuf {
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().map(|d| d.join(p)).unwrap_or_else(|_| p.to_path_buf())
    }
}

fn secs(args: &Value, key: &str, default: f64, lo: f64, hi: f64) -> Duration {
    let v = args.get(key).and_then(Value::as_f64).unwrap_or(default).clamp(lo, hi);
    Duration::from_secs_f64(v)
}

pub struct SimController {
    cfg: Arc<Config>,
    link: Arc<SupervisorLink>,
    proc: WebotsProc,
    last_world: Mutex<Option<PathBuf>>,
    /// Serializes start / stop / restart / reload.
    lifecycle: Mutex<()>,
}

impl SimController {
    pub fn new(cfg: Arc<Config>, link: Arc<SupervisorLink>) -> SimController {
        SimController {
            proc: WebotsProc::new(cfg.clone()),
            cfg,
            link,
            last_world: Mutex::new(None),
            lifecycle: Mutex::new(()),
        }
    }

    // ------------------------------------------------------------
    // Tool catalog (native part)
    // ------------------------------------------------------------

    pub fn native_tools() -> Vec<Value> {
        let mode_run = json!({"type": "string", "enum": ["real_time", "fast"]});
        let mode_start = json!({"type": "string", "enum": ["pause", "real_time", "fast"], "default": "pause"});
        vec![
            tool(
                "sim_start",
                "[时钟] 启动 Webots 并加载世界，等待 supervisor 连接。世界默认以暂停状态启动：暂停时场景工具照常可用，\
                 适合先搭建场景；mode 为 real_time/fast 时启动后立即运行。world 省略时用上一次的世界或 WEBOTS_MCP_WORLD。",
                json!({"type": "object", "properties": {
                    "world": {"type": "string", "description": "世界 .wbt 文件路径"},
                    "mode": mode_start,
                    "connect_timeout_s": {"type": "number", "default": 60}
                }}),
            ),
            tool(
                "sim_stop",
                "[时钟] 退出 Webots。默认先请 supervisor 正常退出，超时后强制结束进程；force=true 直接结束。未保存的场景改动会丢失，先 save_world。",
                json!({"type": "object", "properties": {"force": {"type": "boolean", "default": false}}}),
            ),
            tool(
                "sim_restart",
                "[时钟] 停止并重新启动 Webots（干净的初始状态，场景回到世界文件内容）。world/mode 同 sim_start，省略则沿用上一次。",
                json!({"type": "object", "properties": {
                    "world": {"type": "string"},
                    "mode": mode_start.clone()
                }}),
            ),
            tool(
                "sim_pause",
                "[时钟] 暂停仿真。暂停期间场景/物体/查询类工具仍可使用，但仿真时间不前进。",
                json!({"type": "object", "properties": {}}),
            ),
            tool(
                "sim_resume",
                "[时钟] 继续运行仿真。mode: real_time（实时，画面完整）或 fast（尽可能快）；省略则沿用上次的运行模式。",
                json!({"type": "object", "properties": {"mode": mode_run.clone()}}),
            ),
            tool(
                "sim_step",
                "[时钟] 从暂停状态精确前进 count 个基础时间步后自动暂停，返回前进后的状态。需要先 sim_pause。",
                json!({"type": "object", "properties": {
                    "count": {"type": "integer", "minimum": 1},
                    "mode": mode_run,
                    "timeout_s": {"type": "number", "default": 120}
                }, "required": ["count"]}),
            ),
            tool(
                "sim_reset",
                "[时钟] 重置仿真。kind: physics=只清零所有物理状态（时间和场景不变）；\
                 simulation=时间归零并恢复为已保存的世界（运行时创建的节点会被删除，先 save_world 才能保留）；\
                 reload=重载世界并重启 supervisor（快照和观测任务丢失）；restart=结束并重新启动 Webots。重置后仿真处于暂停状态。",
                json!({"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["physics", "simulation", "reload", "restart"]}
                }, "required": ["kind"]}),
            ),
            tool(
                "sim_status",
                "[时钟] 查看 Webots 进程、supervisor 连接和仿真时钟（state/mode/sim_time）。",
                json!({"type": "object", "properties": {}}),
            ),
        ]
    }

    pub async fn call_native(&self, name: &str, args: &Value) -> Option<Outcome> {
        Some(match name {
            "sim_start" => {
                let _guard = self.lifecycle.lock().await;
                self.start_locked(args).await
            }
            "sim_stop" => {
                let _guard = self.lifecycle.lock().await;
                let force = args.get("force").and_then(Value::as_bool).unwrap_or(false);
                Outcome::ok(self.stop_locked(force).await)
            }
            "sim_restart" => {
                let _guard = self.lifecycle.lock().await;
                self.restart_locked(args).await
            }
            "sim_pause" => self.clock_op("pause", json!({}), Duration::from_secs(10)).await,
            "sim_resume" => {
                let params = match args.get("mode") {
                    Some(m) => json!({"mode": m}),
                    None => json!({}),
                };
                self.clock_op("resume", params, Duration::from_secs(10)).await
            }
            "sim_step" => {
                let mut params = json!({"count": args.get("count").cloned().unwrap_or(Value::Null)});
                if let Some(m) = args.get("mode") {
                    params["mode"] = m.clone();
                }
                self.clock_op("step", params, secs(args, "timeout_s", 120.0, 1.0, 3600.0)).await
            }
            "sim_reset" => self.reset(args).await,
            "sim_status" => Outcome::ok(self.status_value().await),
            _ => return None,
        })
    }

    // ------------------------------------------------------------
    // Forwarding a Supervisor tool (with clock policy)
    // ------------------------------------------------------------

    pub async fn call_remote(&self, name: &str, args: Value) -> Outcome {
        if self.link.session().is_none() {
            return link_err(&LinkError::NotConnected);
        }

        // Observation tools only make progress while the world runs.
        let mut auto_resumed = false;
        if matches!(name, "run_simulation" | "start_simulation") {
            match self.control("status", json!({}), Duration::from_secs(5)).await {
                Err(o) => return o,
                Ok(status) => match status.get("state").and_then(Value::as_str) {
                    Some("paused") => {
                        if !self.cfg.auto_resume {
                            return Outcome::err(
                                "SimulationPaused",
                                format!("{name} 需要仿真在运行，但当前处于暂停。"),
                                Some("先调用 sim_resume（fast 或 real_time）。"),
                            );
                        }
                        // For the synchronous run_simulation, have the supervisor pause in the very
                        // step the task finishes; otherwise fast mode overshoots by seconds of sim time.
                        let resume = json!({"mode": "fast", "pause_on_task_done": name == "run_simulation"});
                        if let Err(o) = self.control("resume", resume, Duration::from_secs(10)).await {
                            return o;
                        }
                        auto_resumed = true;
                    }
                    Some("stepping") => {
                        return Outcome::err("SimulationBusy", "正在执行 sim_step，请等待其完成。", None);
                    }
                    _ => {}
                },
            }
        }

        let timeout = self.timeout_for(name, &args);
        let reply = self.link.call("tool.call", json!({"name": name, "arguments": args}), timeout).await;

        // run_simulation is synchronous: put the world back to paused afterwards.
        if auto_resumed && name == "run_simulation" {
            let _ = self.control("pause", json!({}), Duration::from_secs(10)).await;
        }

        match reply {
            Ok(v) => {
                let text = v.get("text").and_then(Value::as_str).unwrap_or("").to_owned();
                finish_remote(text, auto_resumed, name == "run_simulation")
            }
            Err(e) => link_err(&e),
        }
    }

    fn timeout_for(&self, name: &str, args: &Value) -> Duration {
        let base = self.cfg.call_timeout;
        if name == "run_simulation" {
            let wait = args.get("wait_timeout").and_then(Value::as_f64).unwrap_or(120.0).clamp(1.0, 3600.0);
            return Duration::from_secs_f64(wait + 30.0).max(base);
        }
        base
    }

    // ------------------------------------------------------------
    // Clock primitives
    // ------------------------------------------------------------

    async fn control(&self, op: &str, mut params: Value, timeout: Duration) -> Result<Value, Outcome> {
        if !params.is_object() {
            params = json!({});
        }
        params["op"] = json!(op);
        self.link.call("sim.control", params, timeout).await.map_err(|e| link_err(&e))
    }

    async fn clock_op(&self, op: &str, params: Value, timeout: Duration) -> Outcome {
        match self.control(op, params, timeout).await {
            Ok(status) => Outcome::ok(json!({"simulation": status})),
            Err(o) => o,
        }
    }

    async fn reset(&self, args: &Value) -> Outcome {
        match args.get("kind").and_then(Value::as_str) {
            Some("physics") => self.clock_op("reset_physics", json!({}), Duration::from_secs(10)).await,
            Some("simulation") => self.clock_op("reset", json!({}), Duration::from_secs(30)).await,
            Some("reload") => {
                let _guard = self.lifecycle.lock().await;
                let Some(before) = self.link.session().map(|s| s.session) else {
                    return link_err(&LinkError::NotConnected);
                };
                if let Err(o) = self.control("reload", json!({}), Duration::from_secs(10)).await {
                    return o;
                }
                match self.link.wait_session_after(before, Duration::from_secs(60)).await {
                    Some(info) => Outcome::ok(json!({"reloaded": true, "supervisor": session_json(&info),
                                                      "simulation": self.link.sim_state()})),
                    None => Outcome::err(
                        "ReloadTimeout",
                        "世界重载后 supervisor 60 秒内没有重新连接。",
                        Some("检查 Webots 控制台；必要时 sim_restart。"),
                    ),
                }
            }
            Some("restart") => {
                let _guard = self.lifecycle.lock().await;
                self.restart_locked(&json!({})).await
            }
            _ => Outcome::err("InvalidArguments", "kind 必须是 physics / simulation / reload / restart 之一。", None),
        }
    }

    // ------------------------------------------------------------
    // Lifecycle (callers hold `lifecycle`)
    // ------------------------------------------------------------

    async fn pick_world(&self, args: &Value) -> Result<PathBuf, Outcome> {
        let chosen = match args.get("world").and_then(Value::as_str).filter(|s| !s.trim().is_empty()) {
            Some(w) => Some(PathBuf::from(w)),
            None => match &self.cfg.default_world {
                Some(w) => Some(w.clone()),
                None => self.last_world.lock().await.clone(),
            },
        };
        let Some(world) = chosen else {
            return Err(Outcome::err(
                "WorldRequired",
                "没有指定世界文件。",
                Some("传入 world 参数，或设置环境变量 WEBOTS_MCP_WORLD。"),
            ));
        };
        let world = absolute(&world);
        if !world.is_file() {
            return Err(Outcome::err("WorldNotFound", format!("世界文件不存在: {}", world.display()), None));
        }
        if world.extension().and_then(|e| e.to_str()).map(|e| e.eq_ignore_ascii_case("wbt")) != Some(true) {
            return Err(Outcome::err("InvalidWorld", format!("不是 .wbt 文件: {}", world.display()), None));
        }
        Ok(world)
    }

    async fn start_locked(&self, args: &Value) -> Outcome {
        if self.proc.info().await.is_some() || self.link.session().is_some() {
            return Outcome::err(
                "AlreadyRunning",
                "Webots 已在运行（或已有 supervisor 连接）。",
                Some("用 sim_restart 重新开始，或先 sim_stop。"),
            );
        }
        let mode = args.get("mode").and_then(Value::as_str).unwrap_or("pause");
        if !matches!(mode, "pause" | "real_time" | "fast") {
            return Outcome::err("InvalidArguments", "mode 必须是 pause / real_time / fast。", None);
        }
        let world = match self.pick_world(args).await {
            Ok(w) => w,
            Err(o) => return o,
        };
        let timeout = secs(args, "connect_timeout_s", self.cfg.connect_timeout.as_secs_f64(), 5.0, 600.0);

        if let Err(e) = self.proc.start(&world).await {
            return Outcome::err("LaunchFailed", format!("{e:#}"), Some("设置 WEBOTS_BIN 或 WEBOTS_HOME 指向 Webots 安装位置。"));
        }
        *self.last_world.lock().await = Some(world.clone());

        match self.wait_for_supervisor(timeout).await {
            Ok(_) => {}
            Err(message) => {
                let _ = self.proc.kill().await;
                return Outcome::err(
                    "SupervisorTimeout",
                    message,
                    Some("确认世界里有 supervisor=TRUE 且 controller 为 mcp_server 的 Robot，并查看日志中 [webots] 行。"),
                );
            }
        }

        // The world ran in real time while the controller was starting (Webots only
        // launches controllers once the simulation runs); the Supervisor paused it
        // on connect.  Reset so every session starts from t=0, paused.
        if let Err(o) = self.control("reset", json!({}), Duration::from_secs(30)).await {
            return o;
        }

        if mode != "pause" {
            if let Err(o) = self.control("resume", json!({"mode": mode}), Duration::from_secs(10)).await {
                return o;
            }
        }
        let mut v = self.status_value().await;
        v["started"] = json!(true);
        Outcome::ok(v)
    }

    async fn wait_for_supervisor(&self, timeout: Duration) -> Result<SessionInfo, String> {
        let deadline = Instant::now() + timeout;
        let mut rx = self.link.subscribe_session();
        loop {
            let current = rx.borrow_and_update().clone();
            if let Some(info) = current {
                return Ok(info);
            }
            if self.proc.info().await.is_none() {
                // Exit code 0 may just be a launcher handing over to the real process.
                if let Some(exit) = self.proc.last_exit().await {
                    if !exit.success {
                        return Err(format!("Webots 在 supervisor 连接之前退出了（{}）。", exit.description));
                    }
                }
            }
            let now = Instant::now();
            if now >= deadline {
                return Err(format!("supervisor 在 {} 秒内没有连接。", timeout.as_secs()));
            }
            let _ = tokio::time::timeout((deadline - now).min(Duration::from_millis(500)), rx.changed()).await;
        }
    }

    async fn stop_locked(&self, force: bool) -> Value {
        let mut graceful = false;
        if !force && self.link.session().is_some() {
            let _ = self.link.call("sim.control", json!({"op": "quit"}), Duration::from_secs(3)).await;
            graceful = self.proc.wait_exit(Duration::from_secs(8)).await;
        }
        let mut killed = false;
        if self.proc.info().await.is_some() {
            let _ = self.proc.kill().await;
            killed = true;
        }
        // Give the link a moment to notice the supervisor is gone.
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.link.session().is_some() && Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        json!({"stopped": true, "graceful": graceful, "killed": killed})
    }

    async fn restart_locked(&self, args: &Value) -> Outcome {
        let _ = self.stop_locked(false).await;
        self.start_locked(args).await
    }

    pub async fn shutdown(&self) {
        let _guard = self.lifecycle.lock().await;
        if self.proc.info().await.is_some() || self.link.session().is_some() {
            let _ = self.stop_locked(false).await;
        }
    }

    // ------------------------------------------------------------
    // Status
    // ------------------------------------------------------------

    async fn status_value(&self) -> Value {
        let proc = self.proc.info().await;
        let session = self.link.session();
        let clock = if session.is_some() {
            self.link.call("sim.control", json!({"op": "status"}), Duration::from_secs(3)).await.ok()
        } else {
            None
        };
        let last_exit = self.proc.last_exit().await;
        json!({
            "webots": {
                "running": proc.is_some(),
                "pid": proc.as_ref().map(|p| p.pid),
                "world": proc.as_ref().map(|p| p.world.display().to_string()),
                "uptime_s": proc.as_ref().map(|p| p.uptime_s),
                "last_exit": last_exit.map(|e| e.description),
            },
            "supervisor": {"connected": session.is_some(), "info": session.as_ref().map(session_json)},
            "simulation": clock.or_else(|| self.link.sim_state()),
        })
    }
}

fn session_json(s: &SessionInfo) -> Value {
    json!({
        "session": s.session,
        "pid": s.pid,
        "world_path": s.world_path,
        "project_path": s.project_path,
        "server_version": s.server_version,
        "time_step_ms": s.time_step_ms,
    })
}

fn tool(name: &str, description: &str, schema: Value) -> Value {
    json!({"name": name, "description": description, "inputSchema": schema})
}

/// Flag tool-level errors and annotate results of an auto-resumed run.
fn finish_remote(text: String, auto_resumed: bool, rerun_paused: bool) -> Outcome {
    let parsed: Option<Value> = serde_json::from_str(&text).ok();
    let is_error = parsed
        .as_ref()
        .and_then(|v| v.get("status"))
        .and_then(Value::as_str)
        == Some("error");

    if auto_resumed {
        if let Some(mut v) = parsed {
            if let Some(obj) = v.as_object_mut() {
                obj.insert(
                    "host_note".into(),
                    json!(format!(
                        "仿真原本处于暂停，已自动以 fast 模式运行；现在{}。",
                        if rerun_paused { "已重新暂停" } else { "保持运行（用 sim_pause 暂停）" }
                    )),
                );
                return Outcome { text: v.to_string(), is_error };
            }
        }
    }
    Outcome { text, is_error }
}
