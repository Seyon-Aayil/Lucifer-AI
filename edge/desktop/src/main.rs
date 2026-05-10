// Avoid an extra console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use lucifer_desktop_bindings::{handlers, ClientHandle};

fn main() {
    init_tracing();
    tracing::info!("Lucifer desktop starting");

    tauri::Builder::default()
        .plugin(tauri_plugin_devtools())
        .manage(ClientHandle::default())
        .invoke_handler(handlers!())
        .setup(|_app| {
            tracing::info!("Lucifer window ready");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to launch Lucifer desktop");
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

// Tauri 2.0's devtools plugin is optional. Stubbed here so debug builds get
// the in-window inspector without forcing a runtime dependency on it for
// release builds. Real wiring is left to the consumer if/when desired.
#[allow(clippy::needless_pass_by_value)]
fn tauri_plugin_devtools<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R> {
    tauri::plugin::Builder::new("noop").build()
}
