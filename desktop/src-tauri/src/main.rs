// isekai 桌面壳：拉起并监督 Python 核心进程，承载内建聊天客户端（UMP over 本地回环 WS）。
// 壳不实现世界 / 会话 / 记忆业务，只调用核心契约（DESKTOP_SPEC §一）。

#![windows_subsystem = "windows"]

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, State};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Default, Serialize)]
struct CoreStatus {
    state: String,
    endpoint: Option<String>,
    bootstrap: Option<String>,
    mgmt: Option<String>,
    pid: Option<u32>,
    app: Option<String>,
    data_format: Option<String>,
    rules: Option<String>,
    error: Option<String>,
}

struct AppState {
    root: PathBuf,
    status: Mutex<CoreStatus>,
    child: Mutex<Option<Child>>,
}

fn log_line(root: &Path, message: &str) {
    let dir = root.join("logs");
    let _ = fs::create_dir_all(&dir);
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(dir.join("shell.log")) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "[{now}] {message}");
    }
}

/// 从 exe / cwd 逐级上溯，找第一个含 isekai_core 的目录（cwd 不可靠：dev 下是 src-tauri/）。
fn find_project_root() -> PathBuf {
    if let Ok(value) = std::env::var("ISEKAI_ROOT") {
        let candidate = PathBuf::from(value);
        if candidate.join("isekai_core").exists() {
            return candidate;
        }
    }
    for start in [std::env::current_exe().ok(), std::env::current_dir().ok()]
        .into_iter()
        .flatten()
    {
        let mut candidate: Option<&Path> = Some(start.as_path());
        while let Some(path) = candidate {
            if path.join("isekai_core").exists() {
                return path.to_path_buf();
            }
            candidate = path.parent();
        }
    }
    std::env::current_dir().unwrap_or_default()
}

fn kill_core(state: &AppState) {
    let taken = state.child.lock().unwrap().take();
    if let Some(mut child) = taken {
        let pid = child.id();
        log_line(&state.root, &format!("stopping core pid={pid}"));
        let _ = Command::new("taskkill")
            .args(["/F", "/T", "/PID", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn spawn_core(root: &Path, app: &tauri::AppHandle) -> Result<(), String> {
    let python = root.join(".venv").join("Scripts").join("python.exe");
    if !python.exists() {
        return Err(format!("找不到核心解释器 {}", python.display()));
    }
    log_line(root, "spawning core");
    let mut child = Command::new(&python)
        .arg("-m")
        .arg("isekai_core")
        .arg("--root")
        .arg(root)
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .stdin(Stdio::null())
        .env_remove("PYTHONPATH")
        .env("PYTHONIOENCODING", "utf-8")
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|error| format!("启动核心失败：{error}"))?;
    let stdout = child.stdout.take().ok_or_else(|| "核心 stdout 不可用".to_string())?;
    let pid = child.id();
    log_line(root, &format!("core spawned pid={pid}"));
    app.state::<AppState>().child.lock().unwrap().replace(child);

    let handle = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else { break };
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) else {
                log_line(&handle.state::<AppState>().root, &format!("core stdout (not json): {trimmed}"));
                continue;
            };
            let event = value.get("event").and_then(|item| item.as_str()).unwrap_or("");
            let state = handle.state::<AppState>();
            match event {
                "ready" => {
                    let mut status = state.status.lock().unwrap();
                    status.state = "ready".to_string();
                    status.endpoint = value.get("endpoint").and_then(|item| item.as_str()).map(str::to_string);
                    status.bootstrap = value.get("bootstrap").and_then(|item| item.as_str()).map(str::to_string);
                    status.mgmt = value.get("mgmt").and_then(|item| item.as_str()).map(str::to_string);
                    status.pid = value.get("pid").and_then(|item| item.as_u64()).map(|value| value as u32);
                    status.app = value.get("app").and_then(|item| item.as_str()).map(str::to_string);
                    status.data_format = value.get("data_format").and_then(|item| item.as_str()).map(str::to_string);
                    status.rules = value.get("rules").and_then(|item| item.as_str()).map(str::to_string);
                    status.error = None;
                    let snapshot = status.clone();
                    drop(status);
                    log_line(
                        &state.root,
                        &format!("core ready at {}", snapshot.endpoint.clone().unwrap_or_default()),
                    );
                    let _ = handle.emit("core-status", snapshot);
                }
                "failed" => {
                    let mut status = state.status.lock().unwrap();
                    status.state = "failed".to_string();
                    status.error = value.get("message").and_then(|item| item.as_str()).map(str::to_string);
                    let snapshot = status.clone();
                    drop(status);
                    log_line(&state.root, "core reported failed");
                    let _ = handle.emit("core-status", snapshot);
                }
                _ => {}
            }
        }
        // stdout 关闭 = 核心退出：未就绪时给明确错误，运行中则提示需要重启
        let state = handle.state::<AppState>();
        let mut status = state.status.lock().unwrap();
        if status.state != "failed" {
            status.state = "failed".to_string();
            status.error = Some("核心进程已退出".to_string());
        }
        let snapshot = status.clone();
        drop(status);
        log_line(&state.root, "core stdout closed");
        let _ = handle.emit("core-status", snapshot);
    });
    Ok(())
}

#[tauri::command]
fn core_status(state: State<AppState>) -> CoreStatus {
    state.status.lock().unwrap().clone()
}

#[tauri::command]
fn core_restart(app: tauri::AppHandle, state: State<AppState>) -> Result<(), String> {
    kill_core(&state);
    {
        let mut status = state.status.lock().unwrap();
        *status = CoreStatus { state: "starting".to_string(), ..Default::default() };
    }
    spawn_core(&state.root, &app)
}

fn main() {
    let root = find_project_root();
    log_line(&root, &format!("shell starting, root={}", root.display()));
    let state = AppState {
        root,
        status: Mutex::new(CoreStatus { state: "starting".to_string(), ..Default::default() }),
        child: Mutex::new(None),
    };

    tauri::Builder::default()
        .manage(state)
        .invoke_handler(tauri::generate_handler![core_status, core_restart])
        .setup(|app| {
            let show_item = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
            let restart_item = MenuItem::with_id(app, "restart", "重启核心", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &restart_item, &quit_item])?;
            let icon = app
                .default_window_icon()
                .cloned()
                .ok_or_else(|| "缺少应用图标".to_string())?;
            TrayIconBuilder::new()
                .icon(icon)
                .tooltip("isekai")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "restart" => {
                        let handle = app.clone();
                        let state = app.state::<AppState>();
                        kill_core(&state);
                        {
                            let mut status = state.status.lock().unwrap();
                            *status = CoreStatus { state: "starting".to_string(), ..Default::default() };
                        }
                        if let Err(error) = spawn_core(&state.root, &handle) {
                            let mut status = state.status.lock().unwrap();
                            status.state = "failed".to_string();
                            status.error = Some(error);
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;

            let handle = app.handle().clone();
            let app_state = app.state::<AppState>();
            if let Err(error) = spawn_core(&app_state.root, &handle) {
                let mut status = app_state.status.lock().unwrap();
                status.state = "failed".to_string();
                status.error = Some(error);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // 关窗到托盘，核心继续运行；退出走托盘菜单（DESKTOP_SPEC §五）
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                let state = app.state::<AppState>();
                kill_core(&state);
                log_line(&state.root, "shell exited");
            }
        });
}
