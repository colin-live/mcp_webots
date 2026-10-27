//! Runtime configuration: environment variables, overridable by CLI flags.

use std::net::TcpListener;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Config {
    /// Where the host listens for the Supervisor (and what Webots is told).
    pub ipc_addr: String,
    /// Optional shared secret the Supervisor must present in `hello`.
    pub token: Option<String>,
    /// Explicit Webots executable (else WEBOTS_HOME / PATH).
    pub webots_bin: Option<PathBuf>,
    /// World used by `sim_start` when none is given.
    pub default_world: Option<PathBuf>,
    /// Holds the cached Supervisor tool manifest.
    pub state_dir: PathBuf,
    /// Extra Webots command-line arguments.
    pub extra_webots_args: Vec<String>,
    /// Default timeout of a forwarded tool call.
    pub call_timeout: Duration,
    /// How long `sim_start` waits for the Supervisor to connect.
    pub connect_timeout: Duration,
    /// How long `sim_reset(reload)` waits for the Supervisor to reconnect before
    /// falling back to a clean restart of Webots.
    pub reload_timeout: Duration,
    /// How long a lifecycle tool (start/stop/restart/reload) waits for another
    /// lifecycle operation to finish before failing with a clear error.
    pub lifecycle_wait: Duration,
    /// Resume a paused world automatically for run_simulation / start_simulation.
    pub auto_resume: bool,
    /// Command that starts the project manager (None = feature disabled).
    pub pm_command: Option<Vec<String>>,
    /// Passed to the project manager as WEBOTS_MCP_PROJECTS_DIR.
    pub projects_dir: Option<PathBuf>,
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.trim().is_empty())
}

fn default_state_dir() -> PathBuf {
    if let Some(d) = env("WEBOTS_MCP_STATE_DIR") {
        return PathBuf::from(d);
    }
    if let Some(d) = env("LOCALAPPDATA") {
        return PathBuf::from(d).join("webots-mcp-host");
    }
    if let Some(d) = env("XDG_CACHE_HOME") {
        return PathBuf::from(d).join("webots-mcp-host");
    }
    if let Some(d) = env("HOME") {
        return PathBuf::from(d).join(".cache").join("webots-mcp-host");
    }
    std::env::temp_dir().join("webots-mcp-host")
}

impl Config {
    fn get_available_port() -> std::io::Result<u16> {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr()?.port();
        Ok(port)
    }
    pub fn load() -> Config {
        let port = Self::get_available_port().unwrap_or(47800);
        let mut cfg = Config {
            ipc_addr: format!("127.0.0.1:{port}"),
            token: env("WEBOTS_MCP_TOKEN"),
            webots_bin: env("WEBOTS_BIN").map(PathBuf::from),
            default_world: env("WEBOTS_MCP_WORLD").map(PathBuf::from),
            state_dir: default_state_dir(),
            extra_webots_args: env("WEBOTS_MCP_WEBOTS_ARGS")
                .map(|s| s.split_whitespace().map(str::to_owned).collect())
                .unwrap_or_default(),
            call_timeout: Duration::from_secs(30),
            connect_timeout: Duration::from_secs(60),
            reload_timeout: Duration::from_secs_f64(
                env("WEBOTS_MCP_RELOAD_TIMEOUT_S")
                    .and_then(|v| v.parse::<f64>().ok())
                    .map(|v| v.clamp(1.0, 120.0))
                    .unwrap_or(10.0),
            ),
            lifecycle_wait: Duration::from_secs(5),
            auto_resume: env("WEBOTS_MCP_AUTO_RESUME")
                .map(|v| v != "0")
                .unwrap_or(true),
            pm_command: None,
            projects_dir: env("WEBOTS_PROJECTS_DIR").map(PathBuf::from),
        };
        let mut supervisor_dir =
            env("WEBOTS_MCP_SUPERVISOR_DIR")
                .map(PathBuf::from)
                .or(std::env::current_exe()
                    .ok()
                    .and_then(|p| p.parent().and_then(|p| Some(p.join("_py")))));
        let mut pm_script = env("WEBOTS_MCP_PM_SCRIPT").map(PathBuf::from);
        let python = env("WEBOTS_MCP_PYTHON").unwrap_or_else(|| {
            if cfg!(windows) {
                "python".into()
            } else {
                "python3".into()
            }
        });

        let mut args = std::env::args().skip(1);
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--ipc-addr" => {
                    if let Some(v) = args.next() {
                        cfg.ipc_addr = v;
                    }
                }
                "--webots-bin" => cfg.webots_bin = args.next().map(PathBuf::from),
                "--world" => cfg.default_world = args.next().map(PathBuf::from),
                "--state-dir" => {
                    if let Some(v) = args.next() {
                        cfg.state_dir = PathBuf::from(v);
                    }
                }
                "--no-auto-resume" => cfg.auto_resume = false,
                "--supervisor-dir" => supervisor_dir = args.next().map(PathBuf::from),
                "--pm-script" => pm_script = args.next().map(PathBuf::from),
                "--projects-dir" => cfg.projects_dir = args.next().map(PathBuf::from),
                other => eprintln!("webots-mcp-host: ignoring unknown argument {other}"),
            }
        }
        // Project manager: explicit script, else <supervisor dir>/project_manager.py.
        let script = pm_script.or_else(|| supervisor_dir.map(|d| d.join("project_manager.py")));
        if let Some(script) = script {
            cfg.pm_command = Some(vec![python, script.display().to_string()]);
        }
        cfg
    }

    pub fn pm_manifest_cache(&self) -> PathBuf {
        self.state_dir.join("project_tools.json")
    }

    pub fn manifest_cache(&self) -> PathBuf {
        self.state_dir.join("supervisor_tools.json")
    }
}
