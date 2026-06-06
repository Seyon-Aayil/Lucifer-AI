import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ipc } from "../lib/ipc";

const STEPS = ["Welcome", "Pair device", "Local models", "Permissions", "Done"];

export function Onboarding() {
  const [step, setStep] = useState(0);

  return (
    <div className="flex-1 flex items-center justify-center px-6">
      <div className="max-w-2xl w-full">
        <Header step={step} />
        {step === 0 ? <Welcome onNext={() => setStep(1)} /> : null}
        {step === 1 ? (
          <Pair onNext={() => setStep(2)} onBack={() => setStep(0)} />
        ) : null}
        {step === 2 ? (
          <Models onNext={() => setStep(3)} onBack={() => setStep(1)} />
        ) : null}
        {step === 3 ? (
          <Permissions onNext={() => setStep(4)} onBack={() => setStep(2)} />
        ) : null}
        {step === 4 ? <Done /> : null}
      </div>
    </div>
  );
}

function Header({ step }: { step: number }) {
  return (
    <div className="flex items-center gap-2 mb-6">
      <span className="size-2 rounded-full bg-primary shadow-[0_0_10px_#7C5CFF]" />
      <span className="font-medium">Lucifer</span>
      <span className="text-text-3 ml-2 text-xs">{STEPS[step]}</span>
      <div className="ml-auto flex items-center gap-1.5">
        {STEPS.map((_, i) => (
          <span
            key={i}
            className={`h-1.5 rounded-full transition-all ${
              i === step ? "w-6 bg-primary" : i < step ? "w-1.5 bg-primary/60" : "w-1.5 bg-text-3"
            }`}
          />
        ))}
      </div>
    </div>
  );
}

function Footer({
  onBack,
  onNext,
  nextLabel = "Continue",
  nextDisabled = false,
  busy = false,
  skipLabel,
  onSkip,
}: {
  onBack?: () => void;
  onNext?: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  busy?: boolean;
  skipLabel?: string;
  onSkip?: () => void;
}) {
  return (
    <div className="flex items-center gap-3 mt-6">
      {onBack ? (
        <button className="btn btn-ghost" onClick={onBack}>
          Back
        </button>
      ) : null}
      {onNext ? (
        <button
          className="btn btn-primary"
          disabled={busy || nextDisabled}
          onClick={onNext}
        >
          {busy ? "…" : nextLabel}
        </button>
      ) : null}
      {onSkip && skipLabel ? (
        <span
          className="ml-auto text-[11px] text-text-3 cursor-pointer hover:text-text"
          onClick={onSkip}
        >
          {skipLabel}
        </span>
      ) : null}
    </div>
  );
}

// ── Step 0 — Welcome ──────────────────────────────────────────────────────────

function Welcome({ onNext }: { onNext: () => void }) {
  return (
    <>
      <h1 className="text-3xl font-semibold tracking-tight mb-2">
        Welcome to Lucifer
      </h1>
      <p className="text-text-2 mb-6">
        Your personal AI master server. Lucifer runs locally on your hardware,
        sees only what you allow, and never sends sensitive data to the cloud.
      </p>

      <div className="grid grid-cols-2 gap-3 mb-6">
        <Card title="Local-first">
          On-device inference via Ollama or MLX. Your conversations never leave
          your hardware unless you opt in.
        </Card>
        <Card title="Memory as infrastructure">
          The Librarian Agent owns your knowledge graph. Every other agent goes
          through it; access is governed by an explicit ACL.
        </Card>
        <Card title="HitL by default">
          High-risk actions pause for your approval. Send-email, schedule,
          spend money — you approve, Lucifer acts.
        </Card>
        <Card title="Offline-capable">
          Queue actions while disconnected; deterministic replay on reconnect.
        </Card>
      </div>

      <Footer onNext={onNext} nextLabel="Get started" />
    </>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card !p-3.5">
      <div className="text-[11px] tracking-widest font-semibold text-text-3 uppercase mb-1">
        {title}
      </div>
      <div className="text-sm">{children}</div>
    </div>
  );
}

// ── Step 1 — Pair device ──────────────────────────────────────────────────────

function Pair({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const [code, setCode] = useState(["", "", "", "", "", ""]);
  const [endpoint, setEndpoint] = useState("https://lucifer.home.local:50051");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function setDigit(i: number, v: string) {
    const next = [...code];
    next[i] = v.replace(/\D/g, "").slice(0, 1);
    setCode(next);
    if (next[i] && i < 5) {
      const nextEl = document.getElementById(`code-${i + 1}`);
      nextEl?.focus();
    }
  }

  async function pair() {
    setError(null);
    setBusy(true);
    try {
      const codeStr = code.join("");
      const httpEndpoint = endpoint.replace(/^grpc/, "http");
      // Stable per-install id: reuse the persisted one, or mint + persist a uuid
      // on first pair so the device keeps a consistent identity across re-pairs.
      const settings = await ipc.getSettings();
      const installId =
        settings.device_id || (crypto.randomUUID?.() ?? `device-${Date.now()}`);
      if (!settings.device_id) {
        await ipc.updateSettings({ ...settings, device_id: installId });
      }
      const res = await fetch(`${httpEndpoint}/devices/pair`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ code: codeStr, device_id: installId }),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(`pairing failed (${res.status}): ${detail}`);
      }
      const bundle = await res.json();
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
      // Remember the paired device id so the rest of the app can attribute
      // sync + telemetry to the right device.
      try {
        const current = await ipc.getSettings();
        await ipc.updateSettings({ ...current, device_id: persisted.device_id });
      } catch {
        /* non-fatal: settings persistence is best-effort */
      }
      onNext();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1 className="text-3xl font-semibold tracking-tight mb-2">
        Pair this device
      </h1>
      <p className="text-text-2 mb-8">
        Open <span className="font-mono">/admin/devices</span> on your master web admin.
        Type the 6-digit code below.
      </p>

      <div className="card mb-4">
        <div className="text-xs text-text-2 mb-2">Code</div>
        <div className="flex gap-1.5">
          {code.map((d, i) => (
            <input
              id={`code-${i}`}
              key={i}
              value={d}
              onChange={(e) => setDigit(i, e.target.value)}
              className="input !text-center !text-xl !w-10 !h-12 !p-0"
              maxLength={1}
              autoFocus={i === 0}
            />
          ))}
        </div>
        <div className="text-[11px] text-text-3 mt-3">
          Codes expire in 60 seconds and are single-use.
        </div>
      </div>

      <div className="card mb-4 flex items-center gap-2">
        <span className="text-xs text-text-2 whitespace-nowrap">Master endpoint</span>
        <input
          value={endpoint}
          onChange={(e) => setEndpoint(e.target.value)}
          className="input flex-1 ml-auto"
        />
      </div>

      <div className="text-xs text-text-3 flex items-center gap-2 mb-6">
        <span>🛡</span>
        <span>
          mTLS certs are minted now and stored in your macOS Keychain. The JWT
          auto-refreshes before expiry.
        </span>
      </div>

      {error ? <div className="text-sm text-danger mb-3">{error}</div> : null}

      <Footer
        onBack={onBack}
        onNext={pair}
        nextLabel="Pair device"
        nextDisabled={code.join("").length < 6}
        busy={busy}
      />
    </>
  );
}

// ── Step 2 — Local models ─────────────────────────────────────────────────────

function Models({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const [backend, setBackend] = useState<string>("loading…");

  useEffect(() => {
    ipc.localBackend().then(setBackend).catch(() => setBackend("ollama"));
  }, []);

  return (
    <>
      <h1 className="text-3xl font-semibold tracking-tight mb-2">
        Local models
      </h1>
      <p className="text-text-2 mb-6">
        Lucifer runs inference locally for privacy and latency. Install Ollama
        and pull at least one model — that's the on-device backend.
      </p>

      <div className="card mb-3">
        <div className="text-[11px] tracking-widest font-semibold text-text-3 uppercase mb-2">
          Detected backend
        </div>
        <span className="pill pill-standard font-mono">{backend}</span>
      </div>

      <div className="card mb-4">
        <div className="text-xs text-text-2 mb-2">1 — Install Ollama</div>
        <pre className="font-mono text-[11px] bg-surface-elev rounded p-3 overflow-auto mb-3">
{`brew install ollama
brew services start ollama`}
        </pre>
        <div className="text-xs text-text-2 mb-2">2 — Pull a starter model</div>
        <pre className="font-mono text-[11px] bg-surface-elev rounded p-3 overflow-auto">
{`ollama pull llama3.2     # 2 GB · default chat model
ollama pull nomic-embed-text  # 274 MB · embeddings`}
        </pre>
      </div>

      <div className="text-xs text-text-3 mb-6">
        Apple Silicon users can opt into the MLX runtime later — it builds on
        the same model files and lights up automatically when the
        <span className="font-mono"> mlx </span> feature is enabled.
      </div>

      <Footer
        onBack={onBack}
        onNext={onNext}
        nextLabel="Continue"
        skipLabel="Skip — I'll do this later"
        onSkip={onNext}
      />
    </>
  );
}

// ── Step 3 — Permissions ──────────────────────────────────────────────────────

function Permissions({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const [accessibility, setAccessibility] = useState(false);
  const [notifications, setNotifications] = useState(false);

  const links = useMemo(
    () => ({
      accessibility:
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
      notifications:
        "x-apple.systempreferences:com.apple.preference.notifications",
    }),
    [],
  );

  return (
    <>
      <h1 className="text-3xl font-semibold tracking-tight mb-2">
        Grant permissions
      </h1>
      <p className="text-text-2 mb-6">
        Two macOS permissions unlock the full experience. You can revisit these
        anytime in System Settings.
      </p>

      <PermissionRow
        title="Accessibility"
        body="Required for the global ⌘+Space hotkey overlay and active-app context."
        href={links.accessibility}
        granted={accessibility}
        onMark={() => setAccessibility(true)}
      />
      <PermissionRow
        title="Notifications"
        body="HitL approval prompts and agent alerts surface as native banners."
        href={links.notifications}
        granted={notifications}
        onMark={() => setNotifications(true)}
      />

      <div className="text-xs text-text-3 my-4 flex items-center gap-2">
        <span>🛡</span>
        <span>
          Lucifer never asks for Full Disk Access or Camera at install time.
          MCP tools that need them prompt on first use, and you decide.
        </span>
      </div>

      <Footer
        onBack={onBack}
        onNext={onNext}
        nextLabel={accessibility && notifications ? "Continue" : "Continue anyway"}
        skipLabel="Skip permissions"
        onSkip={onNext}
      />
    </>
  );
}

function PermissionRow({
  title,
  body,
  href,
  granted,
  onMark,
}: {
  title: string;
  body: string;
  href: string;
  granted: boolean;
  onMark: () => void;
}) {
  return (
    <div
      className={`card mb-3 flex items-start gap-3 ${
        granted ? "!border-success/60" : ""
      }`}
    >
      <div
        className={`size-2 rounded-full mt-1.5 ${
          granted ? "bg-success shadow-[0_0_10px_#4ADE80]" : "bg-text-3"
        }`}
      />
      <div className="flex-1">
        <div className="font-medium">{title}</div>
        <div className="text-xs text-text-2 mt-0.5">{body}</div>
      </div>
      <a href={href} className="btn btn-ghost text-xs">
        Open settings
      </a>
      <button className="btn btn-primary text-xs" onClick={onMark} disabled={granted}>
        {granted ? "Granted ✓" : "Mark granted"}
      </button>
    </div>
  );
}

// ── Step 4 — Done ─────────────────────────────────────────────────────────────

function Done() {
  const navigate = useNavigate();
  return (
    <>
      <h1 className="text-3xl font-semibold tracking-tight mb-2">
        You're set up
      </h1>
      <p className="text-text-2 mb-6">
        Lucifer is connected to your master, JWT auto-refresh is running, and
        the offline queue is ready. Press <span className="font-mono">⌘+Space</span>
        anywhere to summon the hotkey overlay.
      </p>

      <div className="card mb-4">
        <div className="text-[11px] tracking-widest font-semibold text-text-3 uppercase mb-2">
          What's next
        </div>
        <ul className="text-sm space-y-2">
          <li>
            <span className="font-mono pill pill-standard mr-2">Conversation</span>
            ask anything — replies stream from your local backend
          </li>
          <li>
            <span className="font-mono pill pill-standard mr-2">Approvals</span>
            review actions before they execute
          </li>
          <li>
            <span className="font-mono pill pill-standard mr-2">Knowledge Graph</span>
            inspect what Lucifer remembers, kill anything you don't want
          </li>
        </ul>
      </div>

      <div className="flex items-center gap-3">
        <button
          className="btn btn-primary"
          onClick={() => navigate("/conversation")}
        >
          Open Lucifer
        </button>
        <span
          className="ml-auto text-[11px] text-text-3 cursor-pointer hover:text-text"
          onClick={() => navigate("/menu-bar")}
        >
          Or check today's status
        </span>
      </div>
    </>
  );
}
