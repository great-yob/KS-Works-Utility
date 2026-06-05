import { useEffect, useState } from "react";
import { Download, RefreshCw } from "lucide-react";

/**
 * Sidebar widget that surfaces auto-update state pushed from the Electron main
 * process. Renders nothing in a plain browser (dev / no electronAPI) or while
 * the app is up to date — it only appears when there is something to show.
 */
export default function UpdateNotice() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);

  useEffect(() => {
    if (!window.electronAPI?.onUpdateStatus) return;
    return window.electronAPI.onUpdateStatus(setStatus);
  }, []);

  if (!status) return null;

  if (status.state === "downloading") {
    return (
      <div className="mt-2 mb-3 mx-3 px-3 py-2 rounded-xl bg-white/[0.03] border border-white/5">
        <div className="flex items-center gap-2 text-[11px] text-slate-400 mb-1.5">
          <Download className="w-3 h-3 animate-pulse" />
          <span>업데이트 다운로드 중… {status.percent}%</span>
        </div>
        <div className="h-1 rounded-full bg-white/10 overflow-hidden">
          <div
            className="h-full bg-blue-400 transition-all duration-300"
            style={{ width: `${status.percent}%` }}
          />
        </div>
      </div>
    );
  }

  if (status.state === "downloaded") {
    return (
      <button
        onClick={() => window.electronAPI?.installUpdate()}
        className="mt-2 mb-3 mx-3 px-3 py-2.5 rounded-xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 hover:bg-emerald-500/20 transition-colors flex items-center gap-2 text-xs font-medium no-drag"
        title={`v${status.version} 설치를 위해 재시작합니다`}
      >
        <RefreshCw className="w-3.5 h-3.5" />
        <span>업데이트 준비됨 — 재시작</span>
      </button>
    );
  }

  return null;
}
