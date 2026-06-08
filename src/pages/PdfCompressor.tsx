import React, { useState, useRef, DragEvent } from "react";
import { FileText, Sliders, Terminal, AlertCircle, FileUp, CheckCircle, RotateCcw, FolderOpen } from "lucide-react";
import { ActiveFileState, TargetOption } from "../types";

const TARGET_OPTIONS: TargetOption[] = [
  { id: "2mb", label: "2 MB", value: 2, badge: "가벼운 전송" },
  { id: "5mb", label: "5 MB", value: 5, badge: "이메일 적합" },
  { id: "10mb", label: "10 MB", value: 10, badge: "포털 대용량" },
  { id: "custom", label: "직접 입력", value: 15, badge: "목표 직접 지정" },
];

const dirOf = (p: string) => {
  const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  return i >= 0 ? p.substring(0, i) : p;
};

export default function PdfCompressor() {
  const [targetSizeId, setTargetSizeId] = useState<string>("5mb");
  const [customSize, setCustomSize] = useState<string>("7");
  const [dragActive, setDragActive] = useState<boolean>(false);
  const [savedFolder, setSavedFolder] = useState<string>("");
  const [fileState, setFileState] = useState<ActiveFileState>({
    file: null as any,
    name: "",
    size: 0,
    status: "idle",
  });

  const [progress, setProgress] = useState<number>(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const getSelectedTargetSizeMB = (): number => {
    if (targetSizeId === "custom") {
      const parsed = parseFloat(customSize);
      return isNaN(parsed) || parsed <= 0 ? 5 : parsed;
    }
    const option = TARGET_OPTIONS.find((o) => o.id === targetSizeId);
    return option ? option.value : 5;
  };

  React.useEffect(() => {
    if (fileState.status !== "compressing") {
      setProgress(0);
      return;
    }

    setProgress(0);
    let timer: NodeJS.Timeout;

    const updateProgress = () => {
      setProgress((prev) => {
        if (prev < 30) {
          return prev + Math.floor(Math.random() * 4) + 2;
        } else if (prev < 70) {
          return prev + Math.floor(Math.random() * 2) + 1;
        } else if (prev < 95) {
          return prev + (Math.random() > 0.65 ? 1 : 0);
        }
        return prev;
      });
      const nextDelay = Math.floor(Math.random() * 150) + 100;
      timer = setTimeout(updateProgress, nextDelay);
    };

    timer = setTimeout(updateProgress, 120);
    return () => clearTimeout(timer);
  }, [fileState.status]);

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
  };

  const selectFile = (file: File) => {
    setSavedFolder("");
    setFileState({ file, name: file.name, size: file.size, status: "selected" });
  };

  const handleDrop = async (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      const file = e.dataTransfer.files[0];
      if (file.type === "application/pdf" || file.name.endsWith(".pdf")) {
        selectFile(file);
      } else {
        setFileState({
          file: null as any,
          name: "",
          size: 0,
          status: "error",
          errorMessage: "PDF 형식의 파일만 업로드할 수 있습니다.",
        });
      }
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      selectFile(e.target.files[0]);
    }
  };

  const triggerCompression = async () => {
    const file = fileState.file;
    if (!file) return;

    setFileState((prev) => ({ ...prev, status: "compressing" }));

    const targetMB = getSelectedTargetSizeMB();
    const formData = new FormData();
    formData.append("targetSize", targetMB.toString());

    let originalPath = "";
    if (window.electronAPI) {
      originalPath = window.electronAPI.getPathForFile(file);
    } else if ((file as any).path) {
      originalPath = (file as any).path;
    }

    if (originalPath) {
      formData.append("originalPath", originalPath);
    }

    formData.append("file", file);

    try {
      const response = await fetch("/api/compress", { method: "POST", body: formData });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || "압축 중 알 수 없는 오류 발생");
      }

      const result = await response.json();
      if (result.success) {
        setProgress(100);
        await new Promise((resolve) => setTimeout(resolve, 600));
        if (originalPath && result.savedDirectly) setSavedFolder(dirOf(originalPath));
        setFileState((prev) => ({ ...prev, status: "success", result: result }));
      } else {
        throw new Error(result.error || "압축을 수행할 수 없습니다.");
      }
    } catch (err: any) {
      setFileState((prev) => ({
        ...prev,
        status: "error",
        errorMessage: err.message || "서버 통신 중 에러가 발생했습니다.",
      }));
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
    setTargetSizeId("5mb");
    setCustomSize("7");
  };

  const resetFiles = () => {
    setSavedFolder("");
    setFileState({ file: null as any, name: "", size: 0, status: "idle" });
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const resetResult = () => {
    setProgress(0);
    setSavedFolder("");
    setFileState((prev) =>
      prev.file
        ? { ...prev, status: "selected", result: undefined, errorMessage: undefined }
        : { file: null as any, name: "", size: 0, status: "idle" }
    );
  };

  const formatSize = (bytes: number): string => {
    if (bytes === 0) return "0 Bytes";
    const k = 1024;
    const sizes = ["Bytes", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
  };

  const busy = fileState.status === "compressing";
  const R = 34;
  const C = 2 * Math.PI * R;

  return (
    <div className="grid grid-cols-[3fr_7fr] gap-4 flex-1 min-h-0 relative z-10">

      {/* ===== 옵션 ===== */}
      <section className="bg-black/20 border border-white/5 rounded-2xl p-4 flex flex-col min-h-0">
        <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-3 flex items-center gap-2 shrink-0">
          <Sliders className="w-3.5 h-3.5 text-blue-400" /> 옵션
        </h3>
        <div className="flex-1 overflow-auto terminal-scroll pr-1 -mr-1">
          <div className="grid grid-cols-1 gap-2">
            {TARGET_OPTIONS.map((option) => (
              <button
                key={option.id}
                id={`target-opt-${option.id}`}
                onClick={() => setTargetSizeId(option.id)}
                disabled={busy}
                className={`px-3 py-2.5 rounded-xl border transition-all text-left no-drag disabled:opacity-50 ${targetSizeId === option.id
                    ? "bg-blue-500/20 border-blue-400/30 text-blue-400 font-semibold shadow-md shadow-blue-500/15"
                    : "bg-white/5 border-transparent hover:bg-white/10 text-slate-300 font-medium"
                  }`}
              >
                <span className="block text-sm">{option.label}</span>
                <span className="text-[10px] opacity-70">{option.badge}</span>
              </button>
            ))}
          </div>

          {targetSizeId === "custom" && (
            <div className="mt-2 bg-white/5 border border-white/10 p-3 rounded-xl">
              <input
                id="custom-size-input"
                type="number"
                step="0.1"
                min="0.5"
                max="500"
                value={customSize}
                disabled={busy}
                onChange={(e) => setCustomSize(e.target.value)}
                className="bg-black/40 border border-white/10 rounded-lg py-1.5 px-3 text-sm focus:outline-none focus:border-blue-400 text-blue-300 w-full no-drag"
                placeholder="MB"
              />
              <span className="text-[10px] text-slate-400 mt-1 block">MB 단위로 입력</span>
            </div>
          )}
        </div>
        <button
          onClick={resetOptions}
          className="mt-3 shrink-0 w-full py-2 rounded-lg bg-white/5 hover:bg-white/10 text-slate-400 hover:text-slate-200 text-xs font-medium flex items-center justify-center gap-1.5 transition-colors no-drag"
        >
          <RotateCcw className="w-3 h-3" /> 옵션 초기화
        </button>
      </section>

      {/* ===== Right: 파일(2) / 결과(3) ===== */}
      <div className="grid grid-rows-[2fr_3fr] gap-4 min-h-0">

        {/* ----- 파일 ----- */}
        <section className="bg-white/[0.03] border border-white/5 rounded-2xl p-4 flex flex-col min-h-0">
          <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-2 flex items-center gap-2 shrink-0">
            <FileText className="w-3.5 h-3.5 text-blue-400" /> 파일
          </h3>

          <div className="flex-1 flex flex-col min-h-0">
            {!fileState.file ? (
              <div
                id="dropzone"
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`flex-1 min-h-[70px] border-2 border-dashed rounded-2xl flex flex-col items-center justify-center text-center px-3 transition-all cursor-pointer no-drag ${dragActive
                    ? "border-blue-400 bg-blue-500/10"
                    : "border-white/10 hover:border-white/20 hover:bg-white/[0.03]"
                  }`}
              >
                <input id="file-input" type="file" ref={fileInputRef} onChange={handleFileChange} accept=".pdf,application/pdf" className="hidden" />
                <FileUp className="w-7 h-7 text-blue-400 mb-1.5" />
                <p className="text-white text-xs font-medium">PDF 드래그 또는 클릭</p>
                <p className="text-slate-500 text-[10px] mt-0.5">최대 500MB</p>
              </div>
            ) : (
              <div className="flex-1 flex flex-col gap-2 min-h-0 justify-center">
                <div className="flex items-center gap-2 bg-white/5 rounded-xl p-2.5 min-w-0">
                  <FileText className="w-7 h-7 text-blue-400 shrink-0" />
                  <div className="min-w-0 text-left">
                    <p className="text-white text-xs font-semibold truncate">{fileState.name}</p>
                    <p className="text-slate-400 text-[10px]">{formatSize(fileState.size)}</p>
                  </div>
                </div>
                {fileState.status === "selected" && (
                  <button
                    onClick={triggerCompression}
                    className="w-full bg-amber-500 hover:bg-amber-400 text-slate-900 font-bold py-2.5 rounded-xl shadow-lg shadow-amber-900/30 transition-all active:scale-95 no-drag"
                  >
                    압축 시작
                  </button>
                )}
                {fileState.status === "compressing" && (
                  <p className="text-amber-400 text-xs text-center animate-pulse">압축 진행 중…</p>
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
            <Terminal className="w-3.5 h-3.5 text-blue-400" /> 진행 및 결과
          </h3>

          {/* status block (compact) */}
          <div className="shrink-0 mb-3 flex items-center justify-center min-h-[64px]">
            {fileState.status === "compressing" && (
              <div className="flex items-center gap-4">
                <div className="relative w-20 h-20">
                  <svg className="w-full h-full transform -rotate-90">
                    <circle cx="40" cy="40" r={R} className="stroke-white/10" strokeWidth="6" fill="transparent" />
                    <circle cx="40" cy="40" r={R} className="stroke-amber-400" strokeWidth="6" fill="transparent" strokeDasharray={C} strokeDashoffset={C * (1 - progress / 100)} strokeLinecap="round" />
                  </svg>
                  <div className="absolute inset-0 flex items-center justify-center font-bold text-base">{progress}%</div>
                </div>
                <p className="text-amber-400 text-sm font-semibold animate-pulse">최적화 엔진 실행 중…</p>
              </div>
            )}
            {fileState.status === "success" && fileState.result && (
              <div className="flex flex-wrap items-center justify-center gap-2">
                <span className="flex items-center gap-1 text-emerald-400 font-bold text-sm"><CheckCircle className="w-5 h-5" /> 압축 성공</span>
                <span className="bg-white/5 px-2.5 py-1 rounded-lg text-xs text-slate-300">원본 {formatSize(fileState.result.originalSize)}</span>
                <span className="bg-emerald-500/10 text-emerald-300 px-2.5 py-1 rounded-lg text-xs font-semibold">결과 {formatSize(fileState.result.compressedSize)}</span>
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
                {fileState.status === "selected" ? "‘압축 시작’을 누르면 결과가 표시됩니다" : "파일을 추가하면 시작할 수 있습니다"}
              </p>
            )}
          </div>

          {/* log window */}
          <div className="flex-1 min-h-0 bg-black/30 border border-white/5 rounded-xl p-3 overflow-auto terminal-scroll font-mono text-[11px] leading-relaxed">
            {fileState.result?.logs && fileState.result.logs.length ? (
              fileState.result.logs.map((l, i) => (
                <div key={i} className="text-slate-300 whitespace-pre-wrap break-all">{l}</div>
              ))
            ) : (
              <span className="text-slate-600">로그가 여기에 표시됩니다…</span>
            )}
          </div>

          {/* actions */}
          <div className="flex gap-2 mt-3 shrink-0">
            {fileState.status === "success" && savedFolder && (
              <button
                onClick={() => openFolderPath(savedFolder)}
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
