//! Webots process lifecycle: spawn with the right flags/environment, observe, stop.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;

use crate::config::Config;

struct Running {
    child: Child,
    pid: u32,
    world: PathBuf,
    started: Instant,
}

#[derive(Debug, Clone)]
pub struct ProcInfo {
    pub pid: u32,
    pub world: PathBuf,
    pub uptime_s: f64,
}

/// How the last Webots process ended (kept so callers can explain failures).
#[derive(Debug, Clone)]
pub struct ExitInfo {
    pub success: bool,
    pub description: String,
}

pub struct WebotsProc {
    cfg: Arc<Config>,
    running: Mutex<Option<Running>>,
    last_exit: Mutex<Option<ExitInfo>>,
}

impl WebotsProc {
    pub fn new(cfg: Arc<Config>) -> Self {
        WebotsProc { cfg, running: Mutex::new(None), last_exit: Mutex::new(None) }
    }

    pub fn resolve_binary(&self) -> PathBuf {
        if let Some(bin) = &self.cfg.webots_bin {
            return bin.clone();
        }
        if let Some(home) = std::env::var_os("WEBOTS_HOME") {
            let home = PathBuf::from(home);
            let candidates: Vec<PathBuf> = if cfg!(windows) {
                // The Windows installer ships an MSYS2 tree; the sub-folder depends on the Webots version.
                vec![
                    home.join("msys64").join("ucrt64").join("bin").join("webots.exe"),
                    home.join("msys64").join("mingw64").join("bin").join("webots.exe"),
                    home.join("webots.exe"),
                ]
            } else {
                vec![home.join("webots")]
            };
            for candidate in candidates {
                if candidate.is_file() {
                    return candidate;
                }
            }
        }
        if cfg!(windows) {
            let default = PathBuf::from(r"C:\Program Files\Webots\msys64\mingw64\bin\webots.exe");
            if default.is_file() {
                return default;
            }
            PathBuf::from("webots.exe")
        } else {
            PathBuf::from("webots")
        }
    }

    /// Launch Webots on `world`, paused, telling the Supervisor where the host listens.
    pub async fn start(&self, world: &Path) -> Result<ProcInfo> {
        let mut guard = self.running.lock().await;
        if let Some(r) = guard.as_mut() {
            if r.child.try_wait()?.is_none() {
                bail!("Webots is already running (pid {})", r.pid);
            }
        }
        *guard = None;
        *self.last_exit.lock().await = None;

        let bin = self.resolve_binary();
        let mut cmd = Command::new(&bin);
        // Webots only starts controllers once the simulation runs (verified: with
        // --mode=pause no controller process appears).  So launch in real time; the
        // Supervisor pauses the world as soon as it connects and the host then resets
        // the clock to t=0 (see SimController::start_locked).
        cmd.arg("--mode=realtime").arg("--batch").arg("--stdout").arg("--stderr");
        cmd.args(&self.cfg.extra_webots_args);
        cmd.arg(world);
        cmd.env("WEBOTS_MCP_IPC", &self.cfg.ipc_addr);
        if let Some(token) = &self.cfg.token {
            cmd.env("WEBOTS_MCP_TOKEN", token);
        }
        // stdout belongs to the MCP protocol: never let the child inherit it.
        cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);

        let mut child = cmd.spawn().with_context(|| format!("failed to launch Webots at {}", bin.display()))?;
        let pid = child.id().unwrap_or(0);
        if let Some(out) = child.stdout.take() {
            tokio::spawn(forward(out, "stdout"));
        }
        if let Some(err) = child.stderr.take() {
            tokio::spawn(forward(err, "stderr"));
        }
        tracing::info!("launched {} (pid {pid}) on {}", bin.display(), world.display());

        let info = ProcInfo { pid, world: world.to_path_buf(), uptime_s: 0.0 };
        *guard = Some(Running { child, pid, world: world.to_path_buf(), started: Instant::now() });
        Ok(info)
    }

    /// Running process, if any (reaps an exited one and remembers how it ended).
    pub async fn info(&self) -> Option<ProcInfo> {
        let mut guard = self.running.lock().await;
        let status = match guard.as_mut() {
            None => return None,
            Some(r) => r.child.try_wait().ok().flatten(),
        };
        if let Some(status) = status {
            let pid = guard.as_ref().map(|r| r.pid).unwrap_or(0);
            tracing::info!("Webots (pid {pid}) exited: {status}");
            *self.last_exit.lock().await = Some(ExitInfo { success: status.success(), description: status.to_string() });
            *guard = None;
            return None;
        }
        guard.as_ref().map(|r| ProcInfo { pid: r.pid, world: r.world.clone(), uptime_s: r.started.elapsed().as_secs_f64() })
    }

    pub async fn last_exit(&self) -> Option<ExitInfo> {
        self.last_exit.lock().await.clone()
    }

    /// Wait for the process to end; true if it did (or was never running).
    pub async fn wait_exit(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            if self.info().await.is_none() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    pub async fn kill(&self) -> Result<()> {
        let mut guard = self.running.lock().await;
        if let Some(r) = guard.as_mut() {
            tracing::warn!("killing Webots (pid {})", r.pid);
            // webots.exe is a launcher whose child (webots-bin.exe) does the work:
            // on Windows kill the whole process tree, not just the launcher.
            #[cfg(windows)]
            {
                let _ = std::process::Command::new("taskkill")
                    .args(["/PID", &r.pid.to_string(), "/T", "/F"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            let _ = r.child.kill().await;
        }
        *guard = None;
        Ok(())
    }
}

async fn forward<R: AsyncRead + Unpin + Send + 'static>(reader: R, stream: &'static str) {
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        tracing::info!(target: "webots", "[{stream}] {line}");
    }
}
