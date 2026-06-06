import { useEffect, useState } from "react";
import { ipc, type AppSettings } from "../lib/ipc";

const BACKENDS = ["auto", "ollama", "mlx"] as const;

export function Settings() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    ipc
      .getSettings()
      .then(setSettings)
      .catch((e) => setError(String(e)));
  }, []);

  function patch(next: Partial<AppSettings>) {
    setSettings((s) => (s ? { ...s, ...next } : s));
    setStatus("idle");
  }

  async function save() {
    if (!settings) return;
    setStatus("saving");
    setError(null);
    try {
      await ipc.updateSettings(settings);
      setStatus("saved");
    } catch (e) {
      setStatus("error");
      setError(String(e));
    }
  }

  if (!settings) {
    return (
      <div className="flex-1 flex items-center justify-center text-text-3 text-sm">
        {error ? `Failed to load settings: ${error}` : "Loading settings…"}
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-6 py-8">
      <div className="max-w-xl mx-auto">
        <h1 className="text-lg font-semibold mb-1">Settings</h1>
        <p className="text-xs text-text-3 mb-6">
          Stored locally in <span className="font-mono">settings.json</span>.
        </p>

        <div className="card flex flex-col gap-5">
          <Field
            label="Overlay hotkey"
            hint="e.g. Super+Space, Control+Shift+K. Applies on next launch."
          >
            <input
              className="input font-mono"
              value={settings.overlay_hotkey}
              onChange={(e) => patch({ overlay_hotkey: e.target.value })}
              spellCheck={false}
            />
          </Field>

          <Field label="Inference backend" hint="Local engine for generation.">
            <select
              className="input"
              value={settings.inference_backend}
              onChange={(e) => patch({ inference_backend: e.target.value })}
            >
              {BACKENDS.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Default model" hint="Model id used for local generation.">
            <input
              className="input font-mono"
              value={settings.default_model}
              onChange={(e) => patch({ default_model: e.target.value })}
              spellCheck={false}
            />
          </Field>

          <div className="flex items-center gap-3 pt-1">
            <button
              className="btn btn-primary disabled:opacity-50"
              onClick={save}
              disabled={status === "saving"}
            >
              {status === "saving" ? "Saving…" : "Save"}
            </button>
            {status === "saved" && (
              <span className="text-xs text-success">Saved</span>
            )}
            {status === "error" && (
              <span className="text-xs text-danger">{error ?? "Save failed"}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-sm font-medium">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-text-3">{hint}</span>}
    </label>
  );
}
