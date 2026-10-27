//! webots-mcp-host: the long-lived main process.
//!
//! * speaks MCP over stdio to the agent,
//! * owns the Webots process and the simulation clock policy,
//! * listens for the (single) Supervisor and exposes its tools dynamically,
//! * spawns and supervises the project manager process.

mod config;
mod ipc;
mod mcp;
mod pm;
mod proc;
mod sim;

use std::sync::Arc;

use anyhow::{Context, Result};
use serde_json::Value;
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

use crate::config::Config;
use crate::ipc::SupervisorLink;
use crate::sim::SimController;

#[tokio::main]
async fn main() -> Result<()> {
    // stdout is the MCP channel: logs must go to stderr.
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_env("WEBOTS_MCP_LOG").unwrap_or_else(|_| EnvFilter::new("info")))
        .with_writer(std::io::stderr)
        .with_ansi(false)
        .init();

    let cfg = Arc::new(Config::load());
    let link = SupervisorLink::new("supervisor", cfg.token.clone());
    let pm_link = SupervisorLink::new("project_manager", None);

    // Show the last known tools before Webots / the project manager are up.
    for (what, path, target) in [
        ("supervisor", cfg.manifest_cache(), &link),
        ("project manager", cfg.pm_manifest_cache(), &pm_link),
    ] {
        if let Ok(text) = std::fs::read_to_string(&path) {
            match serde_json::from_str::<Vec<Value>>(&text) {
                Ok(tools) => {
                    tracing::info!("loaded {} cached {what} tools", tools.len());
                    target.set_tools(tools);
                }
                Err(e) => tracing::warn!("ignoring unreadable {what} tool cache: {e}"),
            }
        }
        spawn_cache_writer(path, target.clone());
    }

    let listener = TcpListener::bind(&cfg.ipc_addr)
        .await
        .with_context(|| format!("cannot listen on {} (is another host already running?)", cfg.ipc_addr))?;
    tracing::info!("waiting for the supervisor on {}", cfg.ipc_addr);
    tokio::spawn(link.clone().serve(listener));

    pm::spawn(cfg.clone(), pm_link.clone());

    let sim = Arc::new(SimController::new(cfg.clone(), link.clone()));

    tokio::select! {
        result = mcp::run(sim.clone(), link.clone(), pm_link.clone()) => result?,
        _ = tokio::signal::ctrl_c() => tracing::info!("interrupted"),
    }

    sim.shutdown().await;
    Ok(())
}

/// Persist a provider's tool manifest whenever it changes.
fn spawn_cache_writer(path: std::path::PathBuf, link: Arc<SupervisorLink>) {
    let mut changed = link.subscribe_tools();
    tokio::spawn(async move {
        while changed.recv().await.is_ok() {
            let tools = link.tools();
            if let Some(dir) = path.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            match serde_json::to_string_pretty(&*tools) {
                Ok(text) => {
                    if let Err(e) = std::fs::write(&path, text) {
                        tracing::warn!("cannot write {}: {e}", path.display());
                    }
                }
                Err(e) => tracing::warn!("cannot serialize tool cache: {e}"),
            }
        }
    });
}
