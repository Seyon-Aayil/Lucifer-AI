// Avoid an extra console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::OnceLock;

use lucifer_desktop_bindings::{
    handlers, settings::AppSettings, ClientHandle, EdgeStoreHandle, InferenceHandle,
    OfflineQueueHandle, SettingsHandle,
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const OVERLAY_LABEL: &str = "overlay";

/// The (modifiers, key) the overlay toggle is bound to, resolved from settings
/// at startup. Changing the hotkey in Settings takes effect on next launch.
static OVERLAY_BINDING: OnceLock<(Modifiers, Code)> = OnceLock::new();

fn main() {
    init_tracing();
    tracing::info!("Lucifer desktop starting");

    tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state() == ShortcutState::Pressed && is_overlay_shortcut(shortcut) {
                        toggle_overlay(app);
                    }
                })
                .build(),
        )
        .manage(ClientHandle::default())
        .manage(EdgeStoreHandle::default())
        .manage(OfflineQueueHandle::default())
        .manage(SettingsHandle::default())
        .manage(build_inference_handle())
        .invoke_handler(handlers!())
        .setup(|app| {
            load_settings(app.handle());
            register_overlay_shortcut(app.handle())?;
            install_tray(app.handle())?;
            open_default_local_stores(app.handle());
            tracing::info!("Lucifer ready · overlay toggle registered");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to launch Lucifer desktop");
}

fn is_overlay_shortcut(s: &Shortcut) -> bool {
    let (mods, code) = OVERLAY_BINDING.get().copied().unwrap_or((Modifiers::SUPER, Code::Space));
    s.matches(mods, code)
}

/// Load persisted settings (sync, tiny file) and resolve the overlay binding.
/// Also seeds the async `SettingsHandle` so the IPC commands serve the same data.
fn load_settings(app: &tauri::AppHandle) {
    let path = match app.path().app_data_dir() {
        Ok(dir) => dir.join("settings.json"),
        Err(e) => {
            tracing::warn!(error = %e, "could not resolve app data dir for settings");
            let _ = OVERLAY_BINDING.set((Modifiers::SUPER, Code::Space));
            return;
        }
    };

    let settings = AppSettings::load(&path).unwrap_or_else(|e| {
        tracing::warn!(error = %e, "settings load failed — using defaults");
        AppSettings::default()
    });

    let binding =
        parse_accelerator(&settings.overlay_hotkey).unwrap_or((Modifiers::SUPER, Code::Space));
    let _ = OVERLAY_BINDING.set(binding);

    // Populate the IPC-facing handle from the same file.
    let handle: SettingsHandle = (*app.state::<SettingsHandle>()).clone();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = handle.open(path).await {
            tracing::warn!(error = %e, "settings handle open failed");
        }
    });
}

/// Parse a `Mod+Mod+Key` accelerator (e.g. `Super+Space`, `Control+Shift+K`)
/// into modifiers + key code. Returns `None` if any token is unrecognised.
fn parse_accelerator(accel: &str) -> Option<(Modifiers, Code)> {
    let mut mods = Modifiers::empty();
    let mut code: Option<Code> = None;
    for token in accel.split('+').map(str::trim).filter(|t| !t.is_empty()) {
        match token.to_ascii_lowercase().as_str() {
            "super" | "cmd" | "command" | "meta" | "win" => mods |= Modifiers::SUPER,
            "ctrl" | "control" => mods |= Modifiers::CONTROL,
            "alt" | "option" | "opt" => mods |= Modifiers::ALT,
            "shift" => mods |= Modifiers::SHIFT,
            other => code = Some(parse_key(other)?),
        }
    }
    code.map(|c| (mods, c))
}

/// Map a single key token to a `Code`. Supports A–Z, 0–9, Space, and a few
/// common named keys; returns `None` for anything else.
fn parse_key(token: &str) -> Option<Code> {
    let t = token.to_ascii_lowercase();
    Some(match t.as_str() {
        "space" => Code::Space,
        "enter" | "return" => Code::Enter,
        "tab" => Code::Tab,
        "escape" | "esc" => Code::Escape,
        "a" => Code::KeyA,
        "b" => Code::KeyB,
        "c" => Code::KeyC,
        "d" => Code::KeyD,
        "e" => Code::KeyE,
        "f" => Code::KeyF,
        "g" => Code::KeyG,
        "h" => Code::KeyH,
        "i" => Code::KeyI,
        "j" => Code::KeyJ,
        "k" => Code::KeyK,
        "l" => Code::KeyL,
        "m" => Code::KeyM,
        "n" => Code::KeyN,
        "o" => Code::KeyO,
        "p" => Code::KeyP,
        "q" => Code::KeyQ,
        "r" => Code::KeyR,
        "s" => Code::KeyS,
        "t" => Code::KeyT,
        "u" => Code::KeyU,
        "v" => Code::KeyV,
        "w" => Code::KeyW,
        "x" => Code::KeyX,
        "y" => Code::KeyY,
        "z" => Code::KeyZ,
        "0" => Code::Digit0,
        "1" => Code::Digit1,
        "2" => Code::Digit2,
        "3" => Code::Digit3,
        "4" => Code::Digit4,
        "5" => Code::Digit5,
        "6" => Code::Digit6,
        "7" => Code::Digit7,
        "8" => Code::Digit8,
        "9" => Code::Digit9,
        _ => return None,
    })
}

fn register_overlay_shortcut(app: &tauri::AppHandle) -> tauri::Result<()> {
    let (mods, code) = OVERLAY_BINDING.get().copied().unwrap_or((Modifiers::SUPER, Code::Space));
    let shortcut = Shortcut::new(Some(mods), code);
    if let Err(e) = app.global_shortcut().register(shortcut) {
        tracing::warn!(error = %e, "could not register overlay hotkey — another app may own it");
    }
    Ok(())
}

fn toggle_overlay(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window(OVERLAY_LABEL) else {
        tracing::warn!("overlay window not found");
        return;
    };
    let visible = window.is_visible().unwrap_or(false);
    if visible {
        let _ = window.hide();
    } else {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn install_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let open_main = MenuItem::with_id(app, "open_main", "Open Lucifer", true, None::<&str>)?;
    let toggle_overlay_item = MenuItem::with_id(
        app,
        "toggle_overlay",
        "Toggle Overlay  ⌘Space",
        true,
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open_main, &toggle_overlay_item, &quit])?;

    let _tray = TrayIconBuilder::with_id("lucifer-tray")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open_main" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "toggle_overlay" => toggle_overlay(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_overlay(tray.app_handle());
            }
        })
        .icon(
            app.default_window_icon()
                .cloned()
                .expect("default window icon present"),
        )
        .build(app)?;

    Ok(())
}

fn build_inference_handle() -> InferenceHandle {
    // Read OLLAMA_HOST or default to the conventional 127.0.0.1:11434.
    let endpoint = std::env::var("OLLAMA_HOST")
        .ok()
        .map(|h| {
            if h.starts_with("http") {
                h
            } else {
                format!("http://{h}")
            }
        })
        .unwrap_or_else(|| "http://127.0.0.1:11434".to_string());
    let (backend, inference) = lucifer_mlx_runtime::select_backend(&endpoint);
    tracing::info!(?backend, endpoint, "local inference backend selected");
    InferenceHandle::new(inference)
}

fn open_default_local_stores(app: &tauri::AppHandle) {
    let app_dir = match app.path().app_data_dir() {
        Ok(dir) => dir,
        Err(e) => {
            tracing::warn!(error = %e, "could not resolve app data dir");
            return;
        }
    };
    if !app_dir.exists() {
        if let Err(e) = std::fs::create_dir_all(&app_dir) {
            tracing::warn!(error = %e, "could not create app data dir");
            return;
        }
    }

    let store: EdgeStoreHandle = (*app.state::<EdgeStoreHandle>()).clone();
    let queue: OfflineQueueHandle = (*app.state::<OfflineQueueHandle>()).clone();
    let store_path = app_dir.join("edge_store.db");
    let queue_path = app_dir.join("offline_queue.db");

    tauri::async_runtime::spawn(async move {
        if let Err(e) = store.open(store_path.clone()).await {
            tracing::warn!(path = %store_path.display(), error = %e, "edge-store open failed");
        }
        if let Err(e) = queue.open(queue_path.clone()).await {
            tracing::warn!(path = %queue_path.display(), error = %e, "offline-queue open failed");
        }
    });
}

fn init_tracing() {
    use tracing_subscriber::EnvFilter;
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .init();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_super_space() {
        assert_eq!(
            parse_accelerator("Super+Space"),
            Some((Modifiers::SUPER, Code::Space))
        );
    }

    #[test]
    fn parses_multi_modifier_letter() {
        assert_eq!(
            parse_accelerator("Control+Shift+K"),
            Some((Modifiers::CONTROL | Modifiers::SHIFT, Code::KeyK))
        );
    }

    #[test]
    fn aliases_and_whitespace_tolerated() {
        assert_eq!(
            parse_accelerator(" cmd + opt + enter "),
            Some((Modifiers::SUPER | Modifiers::ALT, Code::Enter))
        );
    }

    #[test]
    fn rejects_unknown_key() {
        assert_eq!(parse_accelerator("Super+F13"), None);
        assert_eq!(parse_accelerator("Super"), None); // no key
    }
}
