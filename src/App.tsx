import { BrowserRouter, NavLink, useRoutes } from "react-router-dom";
import { X, Minus } from "lucide-react";
import { modules } from "./modules/registry";
import type { AccentColor } from "./modules/types";
import UpdateNotice from "./components/UpdateNotice";
import logo from "./assets/logo.png";

// Routes derived from the registry. useRoutes (vs <Routes>) is the idiomatic way
// to drive routing from a data array and avoids per-<Route> key typing issues.
function PortalRoutes() {
  return useRoutes(
    modules.map((mod) => ({ path: mod.path, element: <mod.Component /> }))
  );
}

const closeApp = async () => {
  try {
    await fetch("/api/close", { method: "POST" });
  } catch (e) {
    console.error(e);
  }
};

const minimizeApp = async () => {
  try {
    await fetch("/api/minimize", { method: "POST" });
  } catch (e) {
    console.error(e);
  }
};

// Active-state classes per accent. Full literal strings so Tailwind keeps them.
const ACCENT_ACTIVE: Record<AccentColor, string> = {
  blue: "bg-blue-500/10 text-blue-400 font-bold border border-blue-500/20 shadow-[0_0_15px_rgba(59,130,246,0.15)]",
  indigo: "bg-indigo-500/10 text-indigo-400 font-bold border border-indigo-500/20 shadow-[0_0_15px_rgba(99,102,241,0.15)]",
  emerald: "bg-emerald-500/10 text-emerald-400 font-bold border border-emerald-500/20 shadow-[0_0_15px_rgba(16,185,129,0.15)]",
  amber: "bg-amber-500/10 text-amber-400 font-bold border border-amber-500/20 shadow-[0_0_15px_rgba(245,158,11,0.15)]",
  rose: "bg-rose-500/10 text-rose-400 font-bold border border-rose-500/20 shadow-[0_0_15px_rgba(244,63,94,0.15)]",
};

const INACTIVE = "text-slate-400 hover:text-slate-200 hover:bg-white/[0.04]";

export default function App() {
  return (
    <BrowserRouter>
      <div className="h-screen bg-[#13161d] text-slate-200 font-sans selection:bg-blue-500/30 overflow-hidden flex flex-row rounded-2xl brightness-125">
        {/* Dynamic Background */}
        <div className="fixed inset-0 pointer-events-none">
          <div className="absolute top-[-10%] left-[-10%] w-[40%] h-[40%] rounded-full bg-blue-600/10 blur-[120px]" />
          <div className="absolute bottom-[-10%] right-[-10%] w-[40%] h-[40%] rounded-full bg-indigo-600/10 blur-[120px]" />
        </div>

        {/* Sidebar (LNB) */}
        <div className="w-44 bg-white/[0.02] border-r border-white/5 backdrop-blur-3xl flex flex-col pt-8 pb-4 relative z-50">
          <div className="px-4 mb-8 draggable">
            <img
              src={logo}
              alt="utility"
              draggable={false}
              className="w-28 select-none pointer-events-none"
            />
          </div>

          {/* Navigation — driven entirely by the module registry */}
          <div className="flex-1 flex flex-col gap-2 px-3">
            {modules.map((mod) => {
              const Icon = mod.icon;
              return (
                <NavLink
                  key={mod.id}
                  to={mod.path}
                  end={mod.path === "/"}
                  className={({ isActive }) =>
                    `flex items-center gap-2 px-3 py-3 rounded-xl transition-all duration-300 text-sm ${
                      isActive ? ACCENT_ACTIVE[mod.accent] : INACTIVE
                    }`
                  }
                >
                  <Icon className="w-5 h-5 shrink-0" />
                  <span>{mod.label}</span>
                </NavLink>
              );
            })}
          </div>

          {/* Auto-update status (visible only when an update is in progress) */}
          <UpdateNotice />

          <div className="px-4 mt-auto">
            <div className="text-[11px] text-slate-500">© kim daekyung</div>
            <div className="text-[10px] text-slate-600 font-mono mt-1">version {__APP_VERSION__}</div>
          </div>
        </div>

        {/* Main Content Area */}
        <div className="flex-1 flex flex-col relative z-10 overflow-hidden">
          {/* Header (Window Controls) */}
          <div className="h-14 flex items-center justify-between px-6 draggable border-b border-white/5 bg-white/[0.01]">
            <div className="flex-1"></div>
            <div className="flex items-center gap-3 no-drag">
              <button
                onClick={minimizeApp}
                className="w-7 h-7 rounded-full flex items-center justify-center bg-white/5 hover:bg-white/10 text-slate-400 transition-colors"
                title="최소화"
              >
                <Minus className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={closeApp}
                className="w-7 h-7 rounded-full flex items-center justify-center bg-white/5 hover:bg-red-500/20 text-slate-400 hover:text-red-400 transition-colors"
                title="닫기"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          {/* Page Routing — pages render their own 3-column grid */}
          <div className="flex-1 overflow-hidden p-4 flex flex-col">
            <PortalRoutes />
          </div>
        </div>
      </div>
    </BrowserRouter>
  );
}
