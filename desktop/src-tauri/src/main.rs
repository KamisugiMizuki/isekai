// isekai 桌面壳：拉起并监督 Python 核心进程，承载内建聊天客户端（UMP over 本地回环 WS）。
// 壳不实现世界 / 会话 / 记忆业务，只调用核心契约（DESKTOP_SPEC §一）。

#![windows_subsystem = "windows"]

use std::collections::HashMap;
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
    //: 退出请求已发出：界面靠轮询取走（隐藏到托盘时壳叫不动页面）
    exit_requested: AtomicBool,
    //: 提醒点击后待定位的提醒标识：同样由界面轮询取走（§3.1/A17）
    pending_notice: Mutex<Option<String>>,
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

/// 界面轮询用：壳是否已请求退出（隐藏到托盘时壳发不出事件，只能让界面来取）。
#[tauri::command]
fn exit_pending(state: State<AppState>) -> bool {
    state.exit_requested.load(Ordering::SeqCst)
}

/// 桌面提醒（DESKTOP_SPEC §3.1 / §十.17）：只作「已固化主动消息」的入口。
/// 显示系统通知；点击落点走 `open_notice`（系统回调与无人值守验收共用同一段处理）。
/// 标题 / 正文由渲染层给（不含实例 / 时间线 / 会话标识），壳只负责显示。
#[tauri::command(rename_all = "snake_case")]
fn notify_message(
    app: tauri::AppHandle,
    notice_id: String,
    title: String,
    body: String,
) -> Result<(), String> {
    // 开发 / 验收开关：让通知直接失败，用于验证「系统通知不可用」的降级路径（§3.1 末条）。
    // 与核心的 ISEKAI_LLM_FAKE 同类；不写正文、错误码不带凭据。
    if std::env::var("ISEKAI_NOTIFY_FAIL").is_ok() {
        let message = "系统通知不可用：注入的失败（ISEKAI_NOTIFY_FAIL）".to_string();
        let state = app.state::<AppState>();
        log_line(&state.root, &format!("notification failed: {message}"));
        return Err(message);
    }
    let mut notification = notify_rust::Notification::new();
    notification.summary(&title).body(&body);
    // 安装版才有注册的 AppUserModelID；dev（target/debug|release）走系统默认，
    // 否则自定义 AUMID 没有对应快捷方式时通知根本不显示（与官方插件同一口径）。
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(|item| item.display().to_string()))
        .unwrap_or_default();
    if !(exe_dir.ends_with("\\target\\debug") || exe_dir.ends_with("\\target\
elease")) {
        notification.app_id(&app.config().identifier);
    }
    let handle = match notification.show() {
        Ok(handle) => handle,
        Err(error) => {
            let message = format!("系统通知不可用：{error}");
            let state = app.state::<AppState>();
            log_line(&state.root, &format!("notification failed: {message}"));
            return Err(message);
        }
    };
    {
        let state = app.state::<AppState>();
        log_line(&state.root, &format!("notification shown notice={notice_id}"));
    }
    let click_app = app.clone();
    std::thread::spawn(move || {
        // 只有真的点开通知（Default / 动作按钮）才定位会话；通知自己过期或被划掉（Closed）不动窗口。
        let _ = handle.wait_for_response(move |response: &notify_rust::NotificationResponse| {
            if matches!(
                response,
                notify_rust::NotificationResponse::Default | notify_rust::NotificationResponse::Action(_)
            ) {
                open_notice(&click_app, &notice_id);
            }
        });
    });
    Ok(())
}

/// 通知点击落点：把窗口带到前台，再把提醒交给渲染层定位（§3.1 末条）。
/// 窗口操作要用 run_on_main_thread：系统通知的激活回调跑在壳自己的线程上，不在主线程。
fn open_notice(app: &tauri::AppHandle, notice_id: &str) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window("main") {
            let _ = window.show();
            let _ = window.unminimize();
            let _ = window.set_focus();
        }
    });
    let state = app.state::<AppState>();
    *state.pending_notice.lock().unwrap() = Some(notice_id.to_string());
    // 隐藏到托盘时 emit 送不到页面（见 begin_quit 注释）：标志位留一份，页面轮询取走
    let _ = app.emit("notice-open", notice_id);
}

#[tauri::command(rename_all = "snake_case")]
fn notice_click(app: tauri::AppHandle, notice_id: String) {
    open_notice(&app, &notice_id);
}

/// 页面轮询取走待定位的提醒（取走即清，避免重复定位）
#[tauri::command]
fn take_pending_notice(state: State<AppState>) -> Option<String> {
    state.pending_notice.lock().unwrap().take()
}

/// 壳自己的设置文件（不动核心的 config.yaml）：目前只有「内建聊天开关」这类壳侧偏好。
fn shell_settings_path(root: &Path) -> PathBuf {
    root.join("config").join("shell.json")
}

#[tauri::command]
fn shell_settings(state: State<AppState>) -> serde_json::Value {
    fs::read_to_string(shell_settings_path(&state.root))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| serde_json::json!({}))
}

#[tauri::command]
fn shell_setting_set(state: State<AppState>, key: String, value: serde_json::Value) -> Result<(), String> {
    let path = shell_settings_path(&state.root);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("壳设置目录不可写：{error}"))?;
    }
    let mut map: serde_json::Map<String, serde_json::Value> = fs::read_to_string(&path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default();
    map.insert(key, value);
    let text = serde_json::to_string_pretty(&map).map_err(|error| format!("壳设置序列化失败：{error}"))?;
    fs::write(&path, text).map_err(|error| format!("壳设置写入失败：{error}"))
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

/// 本地配置里的只读设置事实（DESKTOP_SPEC §3.3 记忆向量化 / 提交 / 世界·会话 组）：
/// 核心 settings 契约只有 llm + core 两段，这几个键由壳读本地配置文件展示可读值；
/// 凭据只回「是否已配置」，明文不进渲染层（§二.6）。写入仍只走核心契约。
#[derive(Clone, Default, Serialize)]
struct ConfigFacts {
    config_file: String,
    mtime: f64,
    packages_dir: String,
    memory_model: String,
    memory_base_url: String,
    memory_key_set: bool,
    commit_enabled: Option<bool>,
    commit_minutes: Option<f64>,
    commit_events: Option<f64>,
    max_active_timelines: Option<f64>,
    rate_max: Option<f64>,
    render_calls_per_day: Option<f64>,
}

// ponytail: 逐行取首个 `key: value`，对本仓库的扁平 config.yaml 够用；
// 配置改成嵌套同名键或多文档时换 yaml crate。
#[tauri::command]
fn config_facts(state: State<AppState>) -> ConfigFacts {
    let path = state.root.join("config").join("config.yaml");
    let text = fs::read_to_string(&path).unwrap_or_default();
    let mut facts = ConfigFacts {
        config_file: path.to_string_lossy().to_string(),
        packages_dir: state.root.join("packages").to_string_lossy().to_string(),
        mtime: fs::metadata(&path)
            .and_then(|meta| meta.modified())
            .ok()
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|value| value.as_secs_f64())
            .unwrap_or(0.0),
        ..Default::default()
    };
    let mut values: HashMap<String, String> = HashMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once(':') else { continue };
        let key = key.trim().trim_start_matches("- ").to_string();
        let value = value
            .split(" #")
            .next()
            .unwrap_or("")
            .trim()
            .trim_matches('"')
            .trim_matches('\'')
            .to_string();
        values.entry(key).or_insert(value);
    }
    let get = |key: &str| values.get(key).cloned().unwrap_or_default();
    facts.memory_model = get("memory_embedding_model");
    facts.memory_base_url = get("memory_embedding_base_url");
    facts.memory_key_set = !get("memory_embedding_api_key").is_empty();
    facts.commit_enabled = get("autocommit_enabled").parse::<bool>().ok();
    facts.commit_minutes = get("autocommit_minutes").parse::<f64>().ok();
    facts.commit_events = get("autocommit_events").parse::<f64>().ok();
    facts.max_active_timelines = get("max_active_timelines").parse::<f64>().ok();
    facts.rate_max = get("rate_max").parse::<f64>().ok();
    facts.render_calls_per_day = get("render_calls_per_day").parse::<f64>().ok();
    facts
}

/// 原生文件对话框（恢复备份选文件）：用系统自带 PowerShell 的 OpenFileDialog。
/// 对话框会一直阻塞到用户选择（或挂起不选）：跑在阻塞线程池上，壳主线程保持可用，
/// 托盘退出 / 关窗不会被它挂住（DESKTOP_SPEC §3.2）。
#[tauri::command]
async fn pick_backup_file(dir: Option<String>) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || pick_backup_file_blocking(dir))
        .await
        .map_err(|error| format!("文件对话框任务失败：{error}"))?
}

fn pick_backup_file_blocking(dir: Option<String>) -> Result<Option<String>, String> {
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
    // 隐藏到托盘后壳叫不动页面：tauri emit / eval / show 全部报 “failed to send message to the webview”，
    // 而页面自己的定时器与 invoke 照常（2026-09 实测）。所以退出请求放在标志位上，
    // 由界面轮询 exit_pending 取走，再回显式的「退出前保存」结果（§五：先保存再停进程）。
    state.exit_requested.store(true, Ordering::SeqCst);
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
        exit_requested: AtomicBool::new(false),
        pending_notice: Mutex::new(None),
    };

    tauri::Builder::default()
        // 单实例插件必须最先注册（§二.1）：第二次启动只唤起已有窗口，
        // 不再起第二个壳、第二个核心（写库进程）
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(state)
        .invoke_handler(tauri::generate_handler![
            core_status,
            exit_pending,
            notify_message,
            notice_click,
            take_pending_notice,
            shell_settings,
            shell_setting_set,
            core_restart,
            log_dir,
            open_dir,
            config_facts,
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
