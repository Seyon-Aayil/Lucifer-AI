import { useEffect, useState } from "react";
import { ipc } from "../lib/ipc";

export function ConnectionBar() {
  const [connected, setConnected] = useState(false);
  const [deviceId, setDeviceId] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    ipc.isConnected().then(setConnected).catch(() => {});
    ipc.getSettings().then((s) => setDeviceId(s.device_id)).catch(() => {});
  }, []);

  async function pull() {
    setError(null);
    try {
      const summary = await ipc.getHotSubgraph(deviceId || "unpaired", 0);
      // Surface as a transient toast via state; here just log to keep the bar terse
      console.log("hot subgraph", summary);
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="flex items-center gap-3 px-4 py-2 border-b border-border bg-surface text-xs">
      <span
        className={`size-2 rounded-full ${
          connected ? "bg-success shadow-[0_0_10px_#4ADE80]" : "bg-text-3"
        }`}
      />
      <span className="text-text-2">
        {connected ? "Connected to master" : "Offline"}
      </span>
      {ipc.inTauri ? null : (
        <span className="pill pill-restricted">browser preview · IPC mocked</span>
      )}
      <div className="ml-auto flex items-center gap-2">
        <button className="btn btn-ghost" onClick={pull} disabled={!connected}>
          Pull subgraph
        </button>
      </div>
      {error ? <span className="text-danger ml-3">{error}</span> : null}
    </div>
  );
}
