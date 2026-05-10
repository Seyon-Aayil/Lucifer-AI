import { useEffect, useRef, useState } from "react";
import { ipc } from "../lib/ipc";

const DEFAULT_MODEL = "llama3.2";

export function HotkeyOverlay() {
  const [prompt, setPrompt] = useState("");
  const [output, setOutput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [backend, setBackend] = useState("ollama");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    ipc.localBackend().then(setBackend).catch(() => {});
    inputRef.current?.focus();
  }, []);

  async function submit() {
    const text = prompt.trim();
    if (!text || streaming) return;
    setError(null);
    setOutput("");
    setStreaming(true);
    try {
      let acc = "";
      await ipc.localGenerateStream(DEFAULT_MODEL, text, (chunk) => {
        acc += chunk.text;
        setOutput(acc);
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setStreaming(false);
    }
  }

  return (
    <div className="flex-1 flex items-start justify-center pt-16 px-6 bg-bg">
      <div
        className="w-[720px] card !p-0 !rounded-lg overflow-hidden backdrop-blur"
        style={{
          boxShadow:
            "0 24px 60px rgba(0,0,0,0.6), inset 0 0 0 1px rgba(255,255,255,0.06)",
        }}
      >
        <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border bg-surface-elev">
          <span className="size-2 rounded-full bg-primary shadow-[0_0_10px_#7C5CFF]" />
          <div className="pill bg-primary/15 text-primary">
            personal-agent
            <span className="text-text-3 ml-2">picked by intent classifier</span>
          </div>
          <div className="ml-auto flex items-center gap-2 text-[11px] font-mono text-text-2">
            <span className="pill pill-standard !text-success !border-success/40">
              backend · {backend}
            </span>
          </div>
        </div>

        <div className="p-4">
          <input
            ref={inputRef}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              } else if (e.key === "Escape") {
                setPrompt("");
                setOutput("");
              }
            }}
            disabled={streaming}
            className="input w-full !bg-transparent !border-0 !text-base font-mono"
            placeholder="Ask or describe a task…"
          />
        </div>

        <div className="px-4 pb-4 max-h-[420px] overflow-auto flex flex-col gap-3">
          {output ? (
            <div className="card text-sm">
              <div className="whitespace-pre-wrap">
                {output}
                {streaming ? (
                  <span className="inline-block w-2 h-4 align-middle bg-primary/80 animate-pulse ml-0.5" />
                ) : null}
              </div>
            </div>
          ) : streaming ? (
            <div className="text-text-3 text-sm italic">…thinking</div>
          ) : (
            <div className="text-text-3 text-sm">
              Press ↩ to send. Replies stream from the local backend.
            </div>
          )}

          {error ? (
            <div className="card text-sm !border-danger/60 text-danger">{error}</div>
          ) : null}
        </div>

        <div className="px-4 py-2 border-t border-border bg-surface-elev text-[11px] text-text-3 flex gap-4">
          <span>Esc clear</span>
          <span>↩ submit</span>
          <span className="ml-auto font-mono">{DEFAULT_MODEL}</span>
        </div>
      </div>
    </div>
  );
}
