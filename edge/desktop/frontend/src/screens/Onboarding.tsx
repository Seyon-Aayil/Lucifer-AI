import { useState } from "react";
import { ipc } from "../lib/ipc";

const STEPS = ["Welcome", "Pair device", "Pick models", "Permissions", "Done"];

export function Onboarding() {
  const [step, setStep] = useState(1);
  const [code, setCode] = useState(["", "", "", "", "", ""]);
  const [endpoint, setEndpoint] = useState("https://lucifer.home.local:50051");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function setDigit(i: number, v: string) {
    const next = [...code];
    next[i] = v.replace(/\D/g, "").slice(0, 1);
    setCode(next);
  }

  async function pair() {
    setError(null);
    setBusy(true);
    try {
      // Exchange the 6-digit code for JWT + cert material against the master.
      // The master web admin endpoint is at <endpoint>/devices/pair.
      const codeStr = code.join("");
      const httpEndpoint = endpoint.replace(/^grpc/, "http");
      const res = await fetch(`${httpEndpoint}/devices/pair`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ code: codeStr, device_id: "device-mac-01" }),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(`pairing failed (${res.status}): ${detail}`);
      }
      const bundle = await res.json();
      // Persist cert + key + CA to disk and JWT to OS keychain.
      const persisted = await ipc.persistPairingBundle({
        device_id: bundle.device_id,
        jwt: bundle.jwt,
        refresh_token: bundle.refresh_token,
        client_cert_pem_b64: bundle.client_cert_pem_b64,
        client_key_pem_b64: bundle.client_key_pem_b64,
        ca_cert_pem_b64: bundle.ca_cert_pem_b64,
      });
      await ipc.connectMaster({
        masterEndpoint: endpoint,
        deviceId: persisted.device_id,
        clientCertPath: persisted.client_cert_path,
        clientKeyPath: persisted.client_key_path,
        caCertPath: persisted.ca_cert_path,
        jwt: bundle.jwt,
        refreshToken: bundle.refresh_token,
        httpMasterEndpoint: httpEndpoint,
      });
      setStep(2);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 flex items-center justify-center px-6">
      <div className="max-w-2xl w-full">
        <div className="flex items-center gap-2 mb-6">
          <span className="size-2 rounded-full bg-primary shadow-[0_0_10px_#7C5CFF]" />
          <span className="font-medium">Lucifer</span>
          <div className="ml-auto flex items-center gap-1.5">
            {STEPS.map((_, i) => (
              <span
                key={i}
                className={`size-2 rounded-full ${
                  i === step ? "bg-primary" : "bg-text-3"
                }`}
              />
            ))}
          </div>
        </div>

        <h1 className="text-3xl font-semibold tracking-tight mb-2">
          Pair this device with your master
        </h1>
        <p className="text-text-2 mb-8">
          Open <span className="font-mono">/admin/devices</span> on your master web admin.
          Scan the QR or type the 6-digit code below.
        </p>

        <div className="grid grid-cols-2 gap-4 mb-6">
          <div className="card flex items-center justify-center aspect-square">
            <div className="size-40 border-2 border-dashed border-text-3 rounded-lg flex items-center justify-center text-text-3 text-sm">
              QR scanner
            </div>
          </div>

          <div className="card">
            <div className="text-xs text-text-2 mb-2">Type code</div>
            <div className="flex gap-1.5">
              {code.map((d, i) => (
                <input
                  key={i}
                  value={d}
                  onChange={(e) => setDigit(i, e.target.value)}
                  className="input !text-center !text-xl !w-10 !h-12 !p-0"
                  maxLength={1}
                />
              ))}
            </div>
            <div className="text-[11px] text-text-3 mt-3">
              or paste from /admin/devices on your master
            </div>
          </div>
        </div>

        <div className="card mb-4 flex items-center gap-2">
          <span className="text-xs text-text-2">Master endpoint</span>
          <input
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            className="input flex-1 ml-auto"
          />
        </div>

        <div className="text-xs text-text-3 flex items-center gap-2 mb-6">
          <span>🛡</span>
          <span>
            mTLS certs minted now and stored in macOS Keychain. Code expires in 60s.
          </span>
        </div>

        {error ? <div className="text-sm text-danger mb-3">{error}</div> : null}

        <div className="flex items-center gap-3">
          <button className="btn btn-ghost" onClick={() => setStep(Math.max(0, step - 1))}>
            Back
          </button>
          <button
            className="btn btn-primary"
            disabled={busy || code.join("").length < 6}
            onClick={pair}
          >
            {busy ? "Pairing…" : "Pair device"}
          </button>
          <span className="ml-auto text-[11px] text-text-3 cursor-pointer hover:text-text">
            Skip — use existing certs
          </span>
        </div>
      </div>
    </div>
  );
}
