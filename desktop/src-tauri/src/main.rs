// isekai 桌面壳：拉起并监督 Python 核心进程，承载内建聊天客户端（UMP over 本地回环 WS）。
// 壳不实现世界 / 会话 / 记忆业务，只调用核心契约（DESKTOP_SPEC §一）。

#![windows_subsystem = "windows"]

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, State};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
/// 退出握手①：界面完成「退出前保存」（管理面 op app.shutdown）的上限（DESKTOP_SPEC §五）。
const FLUSH_WAIT_MS: u64 = 3000;
/// 退出握手②：等核心自行退出的上限；超时才硬杀。
const EXIT_WAIT_MS: u64 = 3000;

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
    //: 退出握手：界面做完「退出前保存」后的确认（说明文字, 是否已请求核心退出）
    exit_ready: Mutex<Option<(String, bool)>>,
    //: 退出序列只跑一次
    quitting: AtomicBool,
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
                    status.state = value
                        .get("state")
                        .and_then(|item| item.as_str())
                        .unwrap_or("ready")
                        .to_string();
                    status.endpoint = value.get("endpoint").and_then(|item| item.as_str()).map(str::to_string);
                    status.bootstrap = value.get("bootstrap").and_then(|item| item.as_str()).map(str::to_string);
                    status.mgmt = value.get("mgmt").and_then(|item| item.as_str()).map(str::to_string);
                    status.pid = value.get("pid").and_then(|item| item.as_u64()).map(|value| value as u32);
                    status.app = value.get("app").and_then(|item| item.as_str()).map(str::to_string);
                    status.data_format = value.get("data_format").and_then(|item| item.as_str()).map(str::to_string);
                    status.rules = value.get("rules").and_then(|item| item.as_str()).map(str::to_string);
                    // 非 ready 的就绪帧（persistence_blocked 等）带原因：别丢，界面要显示它
                    status.error = value.get("error").and_then(|item| item.as_str()).map(str::to_string);
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
        // stdout 关闭 = 核心退出：就绪前或运行中给明确错误；已报告过的终态（如存储不可用）保持不变
        let state = handle.state::<AppState>();
        let mut status = state.status.lock().unwrap();
        if status.state == "starting" || status.state == "ready" {
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

/// 日志目录：壳与核心的日志都在数据根的 logs/ 下（DESKTOP_SPEC §3.3「关于」）。
#[tauri::command]
fn log_dir(state: State<AppState>) -> String {
    state.root.join("logs").to_string_lossy().to_string()
}

/// 打开目录（日志 / 备份）：只用资源管理器，不引新依赖。
#[tauri::command]
fn open_dir(path: String) -> Result<(), String> {
    let folder = PathBuf::from(path.trim());
    if !folder.is_dir() {
        return Err(format!("目录还不存在：{}", folder.display()));
    }
    Command::new("explorer")
        .arg(folder.as_os_str())
        .spawn()
        .map_err(|error| format!("打开目录失败：{error}"))?;
    Ok(())
}

/// 原生文件对话框（恢复备份选文件）：用系统自带 PowerShell 的 OpenFileDialog。
#[tauri::command]
fn pick_backup_file(dir: Option<String>) -> Result<Option<String>, String> {
    let script = concat!(
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;",
        "Add-Type -AssemblyName System.Windows.Forms;",
        "$d=New-Object System.Windows.Forms.OpenFileDialog;",
        "$d.Title='选择要恢复的备份';",
        "$d.Filter='备份文件 (*.db)|*.db|所有文件 (*.*)|*.*';",
        "if($env:ISEKAI_BACKUP_DIR -and (Test-Path $env:ISEKAI_BACKUP_DIR)){$d.InitialDirectory=$env:ISEKAI_BACKUP_DIR};",
        "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($d.FileName)}"
    );
    let output = Command::new("powershell")
        .args(["-NoProfile", "-STA", "-Command", script])
        .env("ISEKAI_BACKUP_DIR", dir.unwrap_or_default())
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|error| format!("打开文件对话框失败：{error}"))?;
    let picked = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Ok(if picked.is_empty() { None } else { Some(picked) })
}

/// 界面完成「退出前保存」后的确认（DESKTOP_SPEC §五 第②步）。
#[tauri::command]
fn exit_ready(state: State<AppState>, detail: String, saved: bool) {
    *state.exit_ready.lock().unwrap() = Some((detail, saved));
}

/// 等核心自己退出（有上限）：成功即说明它走完了 app.py 的 finally。
fn wait_core_exit(state: &AppState, timeout_ms: u64) -> bool {
    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    loop {
        {
            let mut guard = state.child.lock().unwrap();
            match guard.as_mut() {
                None => return true, // 没有子进程（没起来或已退出）
                Some(child) => {
                    if let Ok(Some(_)) = child.try_wait() {
                        return true;
                    }
                }
            }
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// 显式退出（DESKTOP_SPEC §五）：先保存再停进程，全程有上限。
/// ① 请界面走管理面 op `app.shutdown`（补做退出前备份 + 请求核心自行退出），上限 3 秒；
/// ② 等核心自行退出（finally 会收尾会话 / 关服务 / 释放写库锁），上限 3 秒；
/// ③ 界面不可用、保存失败或②超时，才回落到 taskkill /F /T 硬杀。
fn begin_quit(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    if state.quitting.swap(true, Ordering::SeqCst) {
        return; // 退出序列只跑一次
    }
    log_line(&state.root, "exit requested: 先保存再停进程");
    let _ = app.emit("exit-request", ());

    let deadline = Instant::now() + Duration::from_millis(FLUSH_WAIT_MS);
    let mut confirmed: Option<(String, bool)> = None;
    while Instant::now() < deadline {
        if let Some(value) = state.exit_ready.lock().unwrap().clone() {
            confirmed = Some(value);
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let (detail, asked) = confirmed.unwrap_or_else(|| {
        ("界面未在 3 秒内确认（按已有持久化水位退出）".to_string(), false)
    });
    log_line(&state.root, &format!("exit save: {detail}"));

    if asked && wait_core_exit(&state, EXIT_WAIT_MS) {
        let exited = state.child.lock().unwrap().take();
        let code = exited.and_then(|mut child| child.wait().ok()).and_then(|status| status.code());
        log_line(&state.root, &format!("core exited on its own code={code:?}（未硬杀）"));
    } else {
        kill_core(&state); // 兜底：别留孤儿写入者
    }
}

/// 退出入口：托盘菜单调用它；同一序列也可由界面 invoke 触发（便于无人值守验收）。
#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        begin_quit(&app);
        app.exit(0);
    });
}

fn main() {
    let root = find_project_root();
    log_line(&root, &format!("shell starting, root={}", root.display()));
    let state = AppState {
        root,
        status: Mutex::new(CoreStatus { state: "starting".to_string(), ..Default::default() }),
        child: Mutex::new(None),
        exit_ready: Mutex::new(None),
        quitting: AtomicBool::new(false),
    };

    tauri::Builder::default()
        .manage(state)
        .invoke_handler(tauri::generate_handler![
            core_status,
            core_restart,
            log_dir,
            open_dir,
            pick_backup_file,
            exit_ready,
            quit_app
        ])
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
                    "quit" => quit_app(app.clone()),
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
