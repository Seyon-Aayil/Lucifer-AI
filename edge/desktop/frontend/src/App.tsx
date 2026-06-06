import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Sidebar } from "./components/Sidebar";
import { ConnectionBar } from "./components/ConnectionBar";
import { Conversation } from "./screens/Conversation";
import { HotkeyOverlay } from "./screens/HotkeyOverlay";
import { MenuBar } from "./screens/MenuBar";
import { KnowledgeGraph } from "./screens/KnowledgeGraph";
import { Approvals } from "./screens/Approvals";
import { Onboarding } from "./screens/Onboarding";
import { Settings } from "./screens/Settings";

export function App() {
  const { pathname } = useLocation();
  // Overlay window loads with hash `#/overlay`; render the panel only.
  const overlayOnly = pathname === "/overlay";
  if (overlayOnly) {
    return (
      <div className="h-full bg-transparent">
        <HotkeyOverlay />
      </div>
    );
  }
  return (
    <div className="h-full flex">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <ConnectionBar />
        <Routes>
          <Route path="/" element={<Navigate to="/conversation" replace />} />
          <Route path="/conversation" element={<Conversation />} />
          <Route path="/menu-bar"     element={<MenuBar />} />
          <Route path="/graph"        element={<KnowledgeGraph />} />
          <Route path="/approvals"    element={<Approvals />} />
          <Route path="/onboarding"   element={<Onboarding />} />
          <Route path="/settings"     element={<Settings />} />
        </Routes>
      </div>
    </div>
  );
}
