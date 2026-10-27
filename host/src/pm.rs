//! Project manager: a standalone Python process the host spawns and supervises.
//!
//! It speaks the same protocol as the Supervisor (hello + tool manifest,
//! tool.call) but over the child's stdin/stdout, so it needs no port and no
//! Webots. If it exits it is restarted with a short back-off.

use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

use crate::config::Config;
use crate::ipc::SupervisorLink;

pub fn spawn(cfg: Arc<Config>, link: Arc<SupervisorLink>) {
    let Some(command) = cfg.pm_command.clone() else {
        tracing::info!("project manager disabled (set WEBOTS_MCP_SUPERVISOR_DIR or WEBOTS_MCP_PM_SCRIPT to enable)");
        return;
    };
    tokio::spawn(async move {
        let mut backoff = Duration::from_millis(500);
        loop {
            let started = Instant::now();
            let mut cmd = Command::new(&command[0]);
            cmd.args(&command[1..]);
            if let Some(dir) = &cfg.projects_dir {
                cmd.env("WEBOTS_MCP_PROJECTS_DIR", dir);
            }
            cmd.env("PYTHONUNBUFFERED", "1").env("PYTHONIOENCODING", "utf-8");
            cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);

            match cmd.spawn() {
                Err(e) => tracing::error!("cannot start the project manager ({}): {e}", command.join(" ")),
                Ok(mut child) => {
                    tracing::info!("project manager started (pid {:?})", child.id());
                    if let Some(err) = child.stderr.take() {
                        tokio::spawn(async move {
                            let mut lines = BufReader::new(err).lines();
                            while let Ok(Some(line)) = lines.next_line().await {
                                tracing::info!(target: "project_manager", "{line}");
                            }
                        });
                    }
                    if let (Some(stdout), Some(stdin)) = (child.stdout.take(), child.stdin.take()) {
                        link.clone().serve_stream(stdout, stdin).await;
                    }
                    let _ = child.kill().await;
                    let _ = child.wait().await;
                    tracing::warn!("project manager exited");
                }
            }

            // Healthy long run -> restart quickly; crash loop -> back off.
            backoff = if started.elapsed() > Duration::from_secs(30) {
                Duration::from_millis(500)
            } else {
                (backoff * 2).min(Duration::from_secs(10))
            };
            tokio::time::sleep(backoff).await;
        }
    });
}
