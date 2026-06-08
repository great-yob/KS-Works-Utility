import React, { useState, useRef, DragEvent, useEffect } from "react";
import { UploadCloud, Settings, Sliders, Terminal, AlertCircle, CheckCircle, RotateCcw, FolderOpen } from "lucide-react";

type ScannedFile = { path: string; name: string; ext: string; size: number };

type ConvertState = {
  status: "idle" | "selected" | "converting" | "success" | "error";
  files: ScannedFile[];
  totalCount: number;
  successCount: number;
  failCount: number;
  outputDir: string;
  errorMessage?: string;
  extCounts?: Record<string, number>;
  logs?: string[];
};

const DPI_OPTIONS = [
  { id: 72, label: "72 DPI", badge: "웹용 (빠름)" },
  { id: 100, label: "100 DPI", badge: "화면용" },
  { id: 150, label: "150 DPI", badge: "일반 품질" },
  { id: 200, label: "200 DPI", badge: "고품질" },
  { id: 300, label: "300 DPI", badge: "인쇄용 (최고 화질)" },
];

const baseName = (p: string) => {
  const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  return i >= 0 ? p.slice(i + 1) : p;
};

export default function ImageConverter() {
  const [fileState, setFileState] = useState<ConvertState>({
    status: "idle", files: [], totalCount: 0, successCount: 0, failCount: 0, outputDir: ""
  });

  const [dpi, setDpi] = useState<number>(300);
  const [uppercase, setUppercase] = useState<boolean>(false);
  const [options, setOptions] = useState({ jpg: false, bmp: false, emf: false });
  const [dragActive, setDragActive] = useState<boolean>(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [lastRawPaths, setLastRawPaths] = useState<string[]>([]);

  const handleDragOver = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!dragActive) setDragActive(true);
  };

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const paths = Array.from(e.dataTransfer.files)
        .map(f => (window as any).electronAPI ? (window as any).electronAPI.getPathForFile(f) : (f as any).path)
        .filter(p => !!p);
      setLastRawPaths(paths);
      scanFiles(paths, options);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const paths = Array.from(e.target.files)
        .map(f => (window as any).electronAPI ? (window as any).electronAPI.getPathForFile(f) : (f as any).path)
        .filter(p => !!p);
      setLastRawPaths(paths);
      scanFiles(paths, options);
    }
  };

  const scanFiles = async (paths: string[], currentOpts: typeof options) => {
    if (!paths.length) return;

    try {
      const res = await fetch("/api/image/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, options: currentOpts })
      });
      const data = await res.json();

      if (data.success && data.files && data.files.length > 0) {
        const extCounts: Record<string, number> = {};
        data.files.forEach((f: ScannedFile) => {
          extCounts[f.ext] = (extCounts[f.ext] || 0) + 1;
        });

        setFileState({
          status: "selected",
          files: data.files,
          totalCount: data.files.length,
          successCount: 0,
          failCount: 0,
          outputDir: "",
          extCounts,
          logs: []
        });
      } else {
        setFileState(prev => ({
          ...prev, status: "error", errorMessage: "지원하는 이미지 파일을 찾을 수 없습니다."
        }));
      }
    } catch (err: any) {
      setFileState(prev => ({
        ...prev, status: "error", errorMessage: "파일 스캔 중 오류가 발생했습니다."
      }));
    }
  };

  useEffect(() => {
    if (fileState.status === "selected" && lastRawPaths.length > 0) {
      scanFiles(lastRawPaths, options);
    }
  }, [options]);

  const triggerConversion = async () => {
    if (!fileState.files.length) return;

    setFileState(prev => ({ ...prev, status: "converting", successCount: 0, failCount: 0, logs: [] }));

    const firstFilePath = fileState.files[0].path;
    const lastSlash = Math.max(firstFilePath.lastIndexOf("/"), firstFilePath.lastIndexOf("\\"));
    const parentDir = firstFilePath.substring(0, lastSlash);
    const slash = firstFilePath.includes("\\") ? "\\" : "/";
    const outputDir = parentDir + slash + "converted_jpg";

    try {
      const response = await fetch("/api/image/convert-batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          paths: fileState.files.map(f => f.path),
          outputDir,
          dpi,
          uppercase: uppercase.toString()
        })
      });

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No reader from response");
      const decoder = new TextDecoder("utf-8");

      let success = 0;
      let fail = 0;
      const total = fileState.files.length;
      let logs: string[] = [`[시작] 총 ${total}개 파일 JPG 변환을 시작합니다.`];
      setFileState(prev => ({ ...prev, logs }));

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split("\n").filter(l => l.trim());

        for (const line of lines) {
          try {
            const data = JSON.parse(line);
            if (data.event === "progress") {
              if (data.success) success++; else fail++;
              const nm = baseName(data.output || data.input || data.name || data.file || "") || `파일 ${success + fail}`;
              logs = [...logs, data.success
                ? `[성공] ${nm}`
                : `[실패] ${nm}${data.error ? ` — ${data.error}` : ""}`];
              setFileState(prev => ({ ...prev, successCount: success, failCount: fail, outputDir, logs }));
            }
          } catch (e) {
            // ignore JSON parse errors from partial chunks
          }
        }
      }

      logs = [...logs, `[완료] 성공 ${success}건 / 실패 ${fail}건`, `[저장] ${outputDir}`];
      setFileState(prev => ({ ...prev, status: "success", outputDir, successCount: success, failCount: fail, logs }));
    } catch (err: any) {
      setFileState(prev => ({ ...prev, status: "error", errorMessage: "일괄 변환 통신 중 오류가 발생했습니다." }));
    }
  };

  const openFolderPath = async (folderPath: string) => {
    if (!folderPath) return;
    try {
      await fetch("/api/open-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: folderPath }),
      });
    } catch (e) {
      console.error(e);
    }
  };

  // --- Per-column resets ---
  const resetOptions = () => {
    setDpi(300);
    setUppercase(false);
    setOptions({ jpg: false, bmp: false, emf: false });
  };

  const resetFiles = () => {
    setFileState({ status: "idle", files: [], totalCount: 0, successCount: 0, failCount: 0, outputDir: "", logs: [] });
    setLastRawPaths([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const resetResult = () => {
    setFileState(prev =>
      prev.files.length
        ? { ...prev, status: "selected", successCount: 0, failCount: 0, outputDir: "", errorMessage: undefined, logs: [] }
        : { status: "idle", files: [], totalCount: 0, successCount: 0, failCount: 0, outputDir: "", logs: [] }
    );
  };

  const busy = fileState.status === "converting";
  const donePct = fileState.totalCount
    ? Math.round(((fileState.successCount + fileState.failCount) / fileState.totalCount) * 100)
    : 0;
  const R = 34;
  const C = 2 * Math.PI * R;

  return (
    <div className="grid grid-cols-[3fr_7fr] gap-4 flex-1 min-h-0 relative z-10">

      {/* ===== 옵션 ===== */}
      <section className="bg-black/20 border border-white/5 rounded-2xl p-4 flex flex-col min-h-0">
        <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-3 flex items-center gap-2 shrink-0">
          <Sliders className="w-3.5 h-3.5 text-indigo-400" /> 옵션
        </h3>

        <div className="flex-1 overflow-auto terminal-scroll pr-1 -mr-1 space-y-5">
          {/* 출력 DPI */}
          <div>
            <div className="grid grid-cols-1 gap-1.5">
              {DPI_OPTIONS.map((option) => (
                <button
                  key={option.id}
                  onClick={() => setDpi(option.id)}
                  disabled={busy}
                  className={`px-3 py-2 rounded-lg border transition-all flex items-center justify-between gap-2 no-drag disabled:opacity-50 ${dpi === option.id
                      ? "bg-indigo-500/20 border-indigo-400/30 text-indigo-400 font-semibold shadow-md shadow-indigo-500/15"
                      : "bg-white/5 border-transparent hover:bg-white/10 text-slate-300 font-medium"
                    }`}
                >
                  <span className="text-sm">{option.label}</span>
                  <span className="text-[10px] opacity-60">{option.badge}</span>
                </button>
              ))}
            </div>

            <div className="mt-3 flex items-center gap-3">
              <button
                onClick={() => setUppercase(!uppercase)}
                disabled={busy}
                className={`w-12 h-6 rounded-full p-1 transition-colors no-drag shrink-0 ${uppercase ? "bg-indigo-500" : "bg-slate-700"}`}
              >
                <div className={`w-4 h-4 bg-white rounded-full shadow-md transition-transform ${uppercase ? "translate-x-6" : "translate-x-0"}`} />
              </button>
              <span className="text-xs text-slate-300">대문자(.JPEG)로 저장</span>
            </div>
          </div>

          {/* 포함시킬 파일 옵션 */}
          <div>
            <p className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-2 flex items-center gap-2">
              <Settings className="w-3 h-3 text-indigo-400" /> 포함시킬 파일
            </p>
            <div className="flex flex-col gap-2">
              <label className="flex items-center gap-2 text-slate-300 text-sm cursor-pointer no-drag">
                <input type="checkbox" checked={options.jpg} onChange={e => setOptions({ ...options, jpg: e.target.checked })} className="accent-indigo-500" />
                jpg/jpeg 포함
              </label>
              <label className="flex items-center gap-2 text-slate-300 text-sm cursor-pointer no-drag">
                <input type="checkbox" checked={options.bmp} onChange={e => setOptions({ ...options, bmp: e.target.checked })} className="accent-indigo-500" />
                bmp 포함
              </label>
              <label className="flex items-center gap-2 text-slate-300 text-sm cursor-pointer no-drag">
                <input type="checkbox" checked={options.emf} onChange={e => setOptions({ ...options, emf: e.target.checked })} className="accent-indigo-500" />
                emf 포함
              </label>
            </div>
          </div>
        </div>

        <button
          onClick={resetOptions}
          disabled={busy}
          className="mt-3 shrink-0 w-full py-2 rounded-lg bg-white/5 hover:bg-white/10 disabled:opacity-40 disabled:cursor-not-allowed text-slate-400 hover:text-slate-200 text-xs font-medium flex items-center justify-center gap-1.5 transition-colors no-drag"
        >
          <RotateCcw className="w-3 h-3" /> 옵션 초기화
        </button>
      </section>

      {/* ===== Right: 파일(2) / 결과(3) ===== */}
      <div className="grid grid-rows-[2fr_3fr] gap-4 min-h-0">

        {/* ----- 파일 / 폴더 ----- */}
        <section className="bg-white/[0.03] border border-white/5 rounded-2xl p-4 flex flex-col min-h-0">
          <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-2 flex items-center gap-2 shrink-0">
            <UploadCloud className="w-3.5 h-3.5 text-indigo-400" /> 파일 / 폴더
          </h3>

          <div className="flex-1 flex flex-col min-h-0">
            {fileState.files.length === 0 ? (
              <div
                id="dropzone"
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`flex-1 min-h-[70px] border-2 border-dashed rounded-2xl flex flex-col items-center justify-center text-center px-3 transition-all cursor-pointer no-drag ${dragActive
                    ? "border-indigo-400 bg-indigo-500/10"
                    : "border-white/10 hover:border-white/20 hover:bg-white/[0.03]"
                  }`}
              >
                <input
                  id="file-input"
                  type="file"
                  ref={fileInputRef}
                  onChange={handleFileChange}
                  multiple
                  // @ts-ignore
                  webkitdirectory="true"
                  className="hidden"
                />
                <UploadCloud className="w-7 h-7 text-indigo-400 mb-1.5" />
                <p className="text-white text-xs font-medium">폴더/파일 드래그</p>
                <p className="text-slate-500 text-[10px] mt-0.5">PNG · GIF · TIFF · SVG · WMF · WEBP 등</p>
              </div>
            ) : (
              <div className="flex-1 flex flex-col gap-2 min-h-0 justify-center">
                <div className="bg-emerald-500/5 border border-emerald-500/40 rounded-xl p-2.5 text-center">
                  <div className="flex items-center justify-center gap-1.5 text-emerald-400 font-bold text-sm">
                    <CheckCircle className="w-4 h-4" /> {fileState.totalCount}개 준비
                  </div>
                  <div className="text-slate-400 text-[10px] mt-1 flex flex-wrap justify-center gap-x-2 gap-y-0.5">
                    {Object.entries(fileState.extCounts || {}).map(([ext, count]) => (
                      <span key={ext}>{ext.replace(".", "")}:{count}</span>
                    ))}
                  </div>
                </div>
                {fileState.status === "selected" && (
                  <button
                    onClick={triggerConversion}
                    className="w-full bg-amber-500 hover:bg-amber-400 text-slate-900 font-bold py-2.5 rounded-xl shadow-lg shadow-amber-900/30 transition-all active:scale-95 no-drag"
                  >
                    변환 시작
                  </button>
                )}
                {fileState.status === "converting" && (
                  <p className="text-amber-400 text-xs text-center animate-pulse">변환 진행 중…</p>
                )}
                {fileState.status === "success" && (
                  <p className="text-emerald-400 text-xs text-center flex items-center justify-center gap-1">
                    <CheckCircle className="w-3.5 h-3.5" /> 완료
                  </p>
                )}
              </div>
            )}
          </div>

          <button
            onClick={resetFiles}
            disabled={busy}
            className="mt-2 shrink-0 w-full py-2 rounded-lg bg-white/5 hover:bg-white/10 disabled:opacity-40 disabled:cursor-not-allowed text-slate-400 hover:text-slate-200 text-xs font-medium flex items-center justify-center gap-1.5 transition-colors no-drag"
          >
            <RotateCcw className="w-3 h-3" /> 파일 초기화
          </button>
        </section>

        {/* ----- 진행 및 결과 ----- */}
        <section className="bg-white/[0.03] border border-white/5 rounded-2xl p-4 flex flex-col min-h-0">
          <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-2 flex items-center gap-2 shrink-0">
            <Terminal className="w-3.5 h-3.5 text-indigo-400" /> 진행 및 결과
          </h3>

          {/* status block (compact) */}
          <div className="shrink-0 mb-3 flex items-center justify-center min-h-[56px]">
            {fileState.status === "converting" && (
              <div className="flex items-center gap-4">
                <div className="relative w-20 h-20">
                  <svg className="w-full h-full transform -rotate-90">
                    <circle cx="40" cy="40" r={R} className="stroke-white/10" strokeWidth="6" fill="transparent" />
                    <circle cx="40" cy="40" r={R} className="stroke-amber-400" strokeWidth="6" fill="transparent" strokeDasharray={C} strokeDashoffset={C * (1 - donePct / 100)} strokeLinecap="round" />
                  </svg>
                  <div className="absolute inset-0 flex items-center justify-center font-bold text-base">{donePct}%</div>
                </div>
                <div className="text-left">
                  <p className="text-amber-400 text-sm font-semibold animate-pulse">변환 중…</p>
                  <p className="text-slate-400 text-xs mt-1">전체 {fileState.totalCount} · 완료 {fileState.successCount + fileState.failCount}</p>
                </div>
              </div>
            )}
            {fileState.status === "success" && (
              <div className="flex flex-wrap items-center justify-center gap-2">
                <span className={`flex items-center gap-1 font-bold text-sm ${fileState.failCount === 0 ? "text-emerald-400" : "text-amber-400"}`}>
                  {fileState.failCount === 0 ? <CheckCircle className="w-5 h-5" /> : <AlertCircle className="w-5 h-5" />} 변환 종료
                </span>
                <span className="bg-emerald-500/10 text-emerald-300 px-2.5 py-1 rounded-lg text-xs font-semibold">성공 {fileState.successCount}</span>
                {fileState.failCount > 0 && (
                  <span className="bg-amber-500/10 text-amber-300 px-2.5 py-1 rounded-lg text-xs font-semibold">실패 {fileState.failCount}</span>
                )}
              </div>
            )}
            {fileState.status === "error" && (
              <div className="flex flex-col items-center gap-1 text-center">
                <AlertCircle className="w-7 h-7 text-red-400" />
                <p className="text-slate-400 text-xs px-2">{fileState.errorMessage}</p>
              </div>
            )}
            {(fileState.status === "idle" || fileState.status === "selected") && (
              <p className="text-slate-600 text-xs text-center px-2">
                {fileState.status === "selected" ? "‘변환 시작’을 누르면 진행 상황이 표시됩니다" : "폴더/파일을 추가하면 시작할 수 있습니다"}
              </p>
            )}
          </div>

          {/* log window */}
          <div className="flex-1 min-h-0 bg-black/30 border border-white/5 rounded-xl p-3 overflow-auto terminal-scroll font-mono text-[11px] leading-relaxed">
            {fileState.logs && fileState.logs.length ? (
              fileState.logs.map((l, i) => (
                <div key={i} className="text-slate-300 whitespace-pre-wrap break-all">{l}</div>
              ))
            ) : (
              <span className="text-slate-600">로그가 여기에 표시됩니다…</span>
            )}
          </div>

          {/* actions */}
          <div className="flex gap-2 mt-3 shrink-0">
            {fileState.status === "success" && fileState.outputDir && (
              <button
                onClick={() => openFolderPath(fileState.outputDir)}
                className="flex-1 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors no-drag"
              >
                <FolderOpen className="w-3.5 h-3.5" /> 폴더 열기
              </button>
            )}
            <button
              onClick={resetResult}
              disabled={busy}
              className="flex-1 py-2 rounded-lg bg-white/5 hover:bg-white/10 disabled:opacity-40 disabled:cursor-not-allowed text-slate-400 hover:text-slate-200 text-xs font-medium flex items-center justify-center gap-1.5 transition-colors no-drag"
            >
              <RotateCcw className="w-3 h-3" /> 결과 초기화
            </button>
          </div>
        </section>

      </div>
    </div>
  );
}
