// Avoid an extra console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Desktop binary entry. Mobile (iOS/Android) enters through
// `lucifer_desktop_lib::run()` directly via `tauri::mobile_entry_point`.
fn main() {
    lucifer_desktop_lib::run()
}
