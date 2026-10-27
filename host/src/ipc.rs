//! IPC server for the (single) Webots Supervisor.
//!
//! The host listens; the Supervisor connects (Webots starts it, not us).
//! Wire format: newline-delimited JSON, JSON-RPC 2.0 shapes.
//!
//!   supervisor -> host   request `hello`   {protocol, token, tools[], state, ...}
//!                        notify  `state`   {state, mode, sim_time, ...}
//!   host -> supervisor   request `tool.call`    {name, arguments} -> {text}
//!                        request `sim.control`  {op, ...}          -> clock status
//!                        request `ping`
//!
//! `hello` carries the Supervisor's tool manifest, which is how its MCP tools
//! become visible to the host without the host knowing any of them.

use std::collections::HashMap;
use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, mpsc, oneshot, watch};

pub const PROTOCOL: u64 = 1;

#[derive(Debug)]
pub enum LinkError {
    NotConnected,
    Disconnected,
    Timeout(Duration),
    Remote { code: i64, message: String },
}

impl fmt::Display for LinkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LinkError::NotConnected => write!(f, "the Webots supervisor is not connected"),
            LinkError::Disconnected => write!(f, "the supervisor disconnected before replying"),
            LinkError::Timeout(d) => write!(f, "no reply from the supervisor within {:.0}s", d.as_secs_f64()),
            LinkError::Remote { code, message } => write!(f, "supervisor error {code}: {message}"),
        }
    }
}

impl std::error::Error for LinkError {}

/// What the Supervisor told us about itself in `hello`.
#[derive(Debug, Clone, Default)]
pub struct SessionInfo {
    pub session: u64,
    pub pid: Option<u64>,
    pub world_path: Option<String>,
    pub project_path: Option<String>,
    pub server_version: Option<String>,
    pub time_step_ms: Option<u64>,
}

type Reply = Result<Value, LinkError>;

struct Conn {
    session: u64,
    tx: mpsc::UnboundedSender<String>,
    pending: HashMap<u64, oneshot::Sender<Reply>>,
}

pub struct SupervisorLink {
    /// Log label: "supervisor" or "project_manager".
    role: &'static str,
    token: Option<String>,
    next_id: AtomicU64,
    next_session: AtomicU64,
    conn: Mutex<Option<Conn>>,
    session_tx: watch::Sender<Option<SessionInfo>>,
    state_tx: watch::Sender<Option<Value>>,
    tools: Mutex<Arc<Vec<Value>>>,
    tools_changed: broadcast::Sender<()>,
}

impl SupervisorLink {
    pub fn new(role: &'static str, token: Option<String>) -> Arc<Self> {
        Arc::new(SupervisorLink {
            role,
            token,
            next_id: AtomicU64::new(1),
            next_session: AtomicU64::new(0),
            conn: Mutex::new(None),
            session_tx: watch::channel(None).0,
            state_tx: watch::channel(None).0,
            tools: Mutex::new(Arc::new(Vec::new())),
            tools_changed: broadcast::channel(16).0,
        })
    }

    // ---------------- observation ----------------

    pub fn session(&self) -> Option<SessionInfo> {
        self.session_tx.borrow().clone()
    }

    pub fn subscribe_session(&self) -> watch::Receiver<Option<SessionInfo>> {
        self.session_tx.subscribe()
    }

    /// Last clock snapshot pushed by the Supervisor.
    pub fn sim_state(&self) -> Option<Value> {
        self.state_tx.borrow().clone()
    }

    pub fn tools(&self) -> Arc<Vec<Value>> {
        self.tools.lock().unwrap().clone()
    }

    pub fn subscribe_tools(&self) -> broadcast::Receiver<()> {
        self.tools_changed.subscribe()
    }

    /// Replace the known Supervisor tool manifest; notifies subscribers on change.
    pub fn set_tools(&self, tools: Vec<Value>) {
        let changed = {
            let mut guard = self.tools.lock().unwrap();
            let changed = **guard != tools;
            if changed {
                *guard = Arc::new(tools);
            }
            changed
        };
        if changed {
            let _ = self.tools_changed.send(());
        }
    }

    /// Wait until a Supervisor with `session > after` is connected.
    pub async fn wait_session_after(&self, after: u64, timeout: Duration) -> Option<SessionInfo> {
        let mut rx = self.session_tx.subscribe();
        let wait = async {
            loop {
                let current = rx.borrow_and_update().clone();
                if let Some(info) = current {
                    if info.session > after {
                        return Some(info);
                    }
                }
                if rx.changed().await.is_err() {
                    return None;
                }
            }
        };
        tokio::time::timeout(timeout, wait).await.ok().flatten()
    }

    // ---------------- requests to the Supervisor ----------------

    pub async fn call(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, LinkError> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (reply_tx, reply_rx) = oneshot::channel();
        {
            let mut guard = self.conn.lock().unwrap();
            let conn = guard.as_mut().ok_or(LinkError::NotConnected)?;
            let line = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).to_string();
            if conn.tx.send(line).is_err() {
                return Err(LinkError::Disconnected);
            }
            conn.pending.insert(id, reply_tx);
        }
        match tokio::time::timeout(timeout, reply_rx).await {
            Ok(Ok(reply)) => reply,
            Ok(Err(_)) => Err(LinkError::Disconnected),
            Err(_) => {
                if let Some(conn) = self.conn.lock().unwrap().as_mut() {
                    conn.pending.remove(&id);
                }
                Err(LinkError::Timeout(timeout))
            }
        }
    }

    // ---------------- server side ----------------

    pub async fn serve(self: Arc<Self>, listener: TcpListener) {
        loop {
            match listener.accept().await {
                Ok((stream, peer)) => {
                    tracing::debug!("IPC connection from {peer}");
                    tokio::spawn(self.clone().handle_conn(stream));
                }
                Err(e) => {
                    tracing::error!("IPC accept failed: {e}");
                    tokio::time::sleep(Duration::from_millis(200)).await;
                }
            }
        }
    }

    async fn handle_conn(self: Arc<Self>, stream: TcpStream) {
        let _ = stream.set_nodelay(true);
        let (read_half, write_half) = stream.into_split();
        self.serve_stream(read_half, write_half).await;
    }

    /// Run the protocol over any byte stream (TCP for the Supervisor, a child's
    /// stdio for the project manager). Returns when the peer disconnects.
    pub async fn serve_stream<R, W>(self: Arc<Self>, reader: R, mut write_half: W)
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let mut lines = BufReader::new(reader).lines();

        // 1. The first message must be `hello`.
        let first = match tokio::time::timeout(Duration::from_secs(10), lines.next_line()).await {
            Ok(Ok(Some(line))) => line,
            _ => {
                tracing::warn!("{}: peer sent no hello; dropping it", self.role);
                return;
            }
        };
        let hello: Value = match serde_json::from_str(&first) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!("IPC hello is not JSON: {e}");
                return;
            }
        };
        let hello_id = hello.get("id").cloned().unwrap_or(Value::Null);
        let params = hello.get("params").cloned().unwrap_or(Value::Null);

        let reject = |message: &str| json!({"jsonrpc": "2.0", "id": hello_id, "error": {"code": -32600, "message": message}}).to_string();

        if hello.get("method").and_then(Value::as_str) != Some("hello") {
            let _ = write_half.write_all((reject("first message must be hello") + "\n").as_bytes()).await;
            return;
        }
        if params.get("protocol").and_then(Value::as_u64) != Some(PROTOCOL) {
            let _ = write_half.write_all((reject("unsupported protocol version") + "\n").as_bytes()).await;
            return;
        }
        if let Some(expected) = &self.token {
            if params.get("token").and_then(Value::as_str) != Some(expected.as_str()) {
                tracing::warn!("IPC hello rejected: bad token");
                let _ = write_half.write_all((reject("bad token") + "\n").as_bytes()).await;
                return;
            }
        }

        // 2. Install this connection (a newer Supervisor replaces an older one).
        let session = self.next_session.fetch_add(1, Ordering::Relaxed) + 1;
        let (tx, mut rx) = mpsc::unbounded_channel::<String>();
        let _ = tx.send(json!({"jsonrpc": "2.0", "id": hello_id, "result": {"ok": true, "session": session}}).to_string());

        let old = self.conn.lock().unwrap().replace(Conn { session, tx, pending: HashMap::new() });
        if let Some(old) = old {
            tracing::warn!("a new supervisor replaced session {}", old.session);
            fail_pending(old, || LinkError::Disconnected);
        }

        let tools: Vec<Value> = params.get("tools").and_then(Value::as_array).cloned().unwrap_or_default();
        let info = SessionInfo {
            session,
            pid: params.get("pid").and_then(Value::as_u64),
            world_path: params.get("world_path").and_then(Value::as_str).map(str::to_owned),
            project_path: params.get("project_path").and_then(Value::as_str).map(str::to_owned),
            server_version: params.get("server_version").and_then(Value::as_str).map(str::to_owned),
            time_step_ms: params.get("time_step_ms").and_then(Value::as_u64),
        };
        tracing::info!(
            "{} connected: session={session} pid={:?} world={:?} tools={}",
            self.role,
            info.pid,
            info.world_path,
            tools.len()
        );
        self.state_tx.send_replace(Some(json!({
            "state": params.get("state").cloned().unwrap_or(Value::Null),
            "mode": params.get("mode").cloned().unwrap_or(Value::Null),
            "sim_time": params.get("sim_time").cloned().unwrap_or(Value::Null),
        })));
        self.set_tools(tools);
        self.session_tx.send_replace(Some(info));

        // 3. Writer task: ends when the Conn (and its sender) is dropped.
        tokio::spawn(async move {
            while let Some(line) = rx.recv().await {
                if write_half.write_all(line.as_bytes()).await.is_err() || write_half.write_all(b"\n").await.is_err() {
                    break;
                }
            }
            let _ = write_half.shutdown().await;
        });

        // 4. Reader loop.
        loop {
            match lines.next_line().await {
                Ok(Some(line)) => self.on_message(session, &line),
                Ok(None) => break,
                Err(e) => {
                    tracing::debug!("IPC read error: {e}");
                    break;
                }
            }
        }
        self.on_disconnect(session);
    }

    fn on_message(&self, session: u64, line: &str) {
        let msg: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!("ignoring malformed IPC line ({e})");
                return;
            }
        };

        if let Some(method) = msg.get("method").and_then(Value::as_str) {
            let params = msg.get("params").cloned().unwrap_or(Value::Null);
            match method {
                "state" => {
                    self.state_tx.send_replace(Some(params));
                }
                "log" => tracing::info!(target: "supervisor", "{}", params),
                "ping" => {
                    if let (Some(id), Some(conn)) = (msg.get("id"), self.conn.lock().unwrap().as_ref()) {
                        let _ = conn.tx.send(json!({"jsonrpc": "2.0", "id": id, "result": {"pong": true}}).to_string());
                    }
                }
                other => tracing::debug!("ignoring supervisor message {other}"),
            }
            return;
        }

        // A response to one of our requests.
        let Some(id) = msg.get("id").and_then(Value::as_u64) else { return };
        let reply: Reply = match msg.get("error") {
            Some(err) => Err(LinkError::Remote {
                code: err.get("code").and_then(Value::as_i64).unwrap_or(-32000),
                message: err.get("message").and_then(Value::as_str).unwrap_or("unknown error").to_owned(),
            }),
            None => Ok(msg.get("result").cloned().unwrap_or(Value::Null)),
        };
        let waiter = {
            let mut guard = self.conn.lock().unwrap();
            match guard.as_mut() {
                Some(conn) if conn.session == session => conn.pending.remove(&id),
                _ => None,
            }
        };
        if let Some(waiter) = waiter {
            let _ = waiter.send(reply);
        }
    }

    fn on_disconnect(&self, session: u64) {
        let old = {
            let mut guard = self.conn.lock().unwrap();
            match guard.as_ref() {
                Some(conn) if conn.session == session => guard.take(),
                _ => None, // already replaced by a newer session
            }
        };
        if let Some(old) = old {
            tracing::info!("supervisor disconnected (session {session})");
            fail_pending(old, || LinkError::Disconnected);
            self.session_tx.send_replace(None);
            self.state_tx.send_replace(None);
        }
    }
}

fn fail_pending(conn: Conn, err: impl Fn() -> LinkError) {
    for (_, waiter) in conn.pending {
        let _ = waiter.send(Err(err()));
    }
}
