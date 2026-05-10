// Avoid an extra console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use lucifer_desktop_bindings::{handlers, ClientHandle};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const OVERLAY_LABEL: &str = "overlay";

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
        .invoke_handler(handlers!())
        .setup(|app| {
            register_overlay_shortcut(app.handle())?;
            install_tray(app.handle())?;
            tracing::info!("Lucifer ready · ⌘+Space toggles overlay");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to launch Lucifer desktop");
}

fn is_overlay_shortcut(s: &Shortcut) -> bool {
    s.matches(Modifiers::SUPER, Code::Space)
}

fn register_overlay_shortcut(app: &tauri::AppHandle) -> tauri::Result<()> {
    let shortcut = Shortcut::new(Some(Modifiers::SUPER), Code::Space);
    if let Err(e) = app.global_shortcut().register(shortcut) {
        tracing::warn!(error = %e, "could not register ⌘+Space — another app may own it");
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

fn init_tracing() {
    use tracing_subscriber::EnvFilter;
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .init();
}
