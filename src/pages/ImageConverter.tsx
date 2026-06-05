import React, { useState, useRef, DragEvent, useEffect } from "react";
import { motion, AnimatePresence } from "motion/react";
import { UploadCloud, Image as ImageIcon, Settings, RefreshCw, Terminal, Download, AlertCircle, CheckCircle } from "lucide-react";

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
};

const DPI_OPTIONS = [
  { id: 72, label: "72 DPI", badge: "웹용 (빠름)" },
  { id: 100, label: "100 DPI", badge: "화면용" },
  { id: 150, label: "150 DPI", badge: "일반 품질" },
  { id: 200, label: "200 DPI", badge: "고품질" },
  { id: 300, label: "300 DPI", badge: "인쇄용 (최고 화질)" },
];

export default function ImageConverter() {
  const [fileState, setFileState] = useState<ConvertState>({
    status: "idle", files: [], totalCount: 0, successCount: 0, failCount: 0, outputDir: ""
  });
  
  const [dpi, setDpi] = useState<number>(300);
  const [uppercase, setUppercase] = useState<boolean>(false);
  const [options, setOptions] = useState({ jpg: false, bmp: false, emf: false });
  const [dragActive, setDragActive] = useState<boolean>(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  
  // Store raw paths for re-scanning if options change
  const [lastRawPaths, setLastRawPaths] = useState<string[]>([]);

  const handleDrag = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

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
          extCounts
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

  // Re-scan if options change while in "selected" state
  useEffect(() => {
    if (fileState.status === "selected" && lastRawPaths.length > 0) {
      scanFiles(lastRawPaths, options);
    }
  }, [options]);

  const triggerConversion = async () => {
    if (!fileState.files.length) return;

    setFileState(prev => ({ ...prev, status: "converting", successCount: 0, failCount: 0 }));

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
              // Update state without waiting for render loop
              setFileState(prev => ({ ...prev, successCount: success, failCount: fail, outputDir }));
            }
          } catch (e) {
            // ignore JSON parse errors from partial chunks if any
          }
        }
      }
      
      setFileState(prev => ({ ...prev, status: "success", outputDir, successCount: success, failCount: fail }));
    } catch (err: any) {
      setFileState(prev => ({ ...prev, status: "error", errorMessage: "일괄 변환 통신 중 오류가 발생했습니다." }));
    }
  };

  const openFolder = async () => {
    if (!fileState.outputDir) return;
    // We could expose an endpoint to open explorer if needed, but for now we just show path
    alert(`결과물 위치:\n${fileState.outputDir}`);
  };

  const resetState = () => {
    setFileState({ status: "idle", files: [], totalCount: 0, successCount: 0, failCount: 0, outputDir: "" });
    setLastRawPaths([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <div className="max-w-4xl mx-auto w-full space-y-6 relative z-10 flex-1 flex flex-col pt-4">
      {/* Settings Panel */}
      <div className="bg-black/20 p-6 border border-white/5 rounded-2xl">
        <div className="space-y-6">
          <div>
            <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-4 flex items-center gap-2">
              <Settings className="w-3.5 h-3.5 text-indigo-400" />
              포함시킬 파일 옵션
            </h3>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 text-slate-300 text-sm cursor-pointer">
                <input type="checkbox" checked={options.jpg} onChange={e => setOptions({...options, jpg: e.target.checked})} className="accent-indigo-500" />
                jpg/jpeg 포함
              </label>
              <label className="flex items-center gap-2 text-slate-300 text-sm cursor-pointer">
                <input type="checkbox" checked={options.bmp} onChange={e => setOptions({...options, bmp: e.target.checked})} className="accent-indigo-500" />
                bmp 포함
              </label>
              <label className="flex items-center gap-2 text-slate-300 text-sm cursor-pointer">
                <input type="checkbox" checked={options.emf} onChange={e => setOptions({...options, emf: e.target.checked})} className="accent-indigo-500" />
                emf 포함
              </label>
            </div>
          </div>

          <div>
            <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-4 flex items-center gap-2">
              <Settings className="w-3.5 h-3.5 text-indigo-400" />
              출력 설정
            </h3>
            
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              {DPI_OPTIONS.map((option) => (
                <button
                  key={option.id}
                  onClick={() => setDpi(option.id)}
                  disabled={fileState.status === "converting"}
                  className={`px-4 py-3 rounded-xl border transition-all duration-200 cursor-pointer ${
                    dpi === option.id
                      ? "bg-indigo-500/20 border-indigo-400/30 text-indigo-400 font-semibold shadow-md shadow-indigo-500/15"
                      : "bg-white/5 border-transparent hover:bg-white/10 text-slate-300 font-medium"
                  }`}
                >
                  <div className="text-left">
                    <span className="block text-sm">{option.label}</span>
                    <span className="text-[10px] opacity-70">{option.badge}</span>
                  </div>
                </button>
              ))}
            </div>

            <div className="mt-4 flex items-center gap-3">
              <button
                onClick={() => setUppercase(!uppercase)}
                disabled={fileState.status === "converting"}
                className={`w-12 h-6 rounded-full p-1 transition-colors ${uppercase ? 'bg-indigo-500' : 'bg-slate-700'}`}
              >
                <motion.div
                  className="w-4 h-4 bg-white rounded-full shadow-md"
                  animate={{ x: uppercase ? 24 : 0 }}
                  transition={{ type: "spring", stiffness: 500, damping: 30 }}
                />
              </button>
              <span className="text-sm text-slate-300">확장자를 대문자(.JPEG)로 저장</span>
            </div>
          </div>
        </div>
      </div>

      {/* Main Work Workbench */}
      <div className="bg-white/5 border border-white/5 rounded-3xl p-8 flex-1">
        <AnimatePresence mode="wait">
          
          {/* IDLE state view: Drop files/folders */}
          {fileState.status === "idle" && (
            <motion.div
              key="idle-view"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              className="flex flex-col items-center justify-center min-h-[300px]"
            >
              <div
                id="dropzone"
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`w-full max-w-lg h-64 border-2 border-dashed rounded-3xl flex flex-col items-center justify-center transition-all duration-300 cursor-pointer ${
                  dragActive
                    ? "border-indigo-400 bg-indigo-500/10"
                    : "border-white/10 hover:border-white/20 hover:bg-white/[0.03]"
                }`}
              >
                {/* allow directories selection in input if user clicks */}
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

                <div className="w-16 h-16 rounded-full bg-indigo-500/20 flex items-center justify-center mb-4">
                  <UploadCloud className="w-8 h-8 text-indigo-400" />
                </div>

                <p className="text-white font-medium">폴더 전체 또는 파일들을 여기에 드래그</p>
                <p className="text-slate-500 text-xs mt-1">지원: PNG, GIF, TIF, SVG, WMF, WEBP 등</p>
              </div>
            </motion.div>
          )}

          {/* SELECTED state view: Ready to convert */}
          {fileState.status === "selected" && (
            <motion.div
              key="selected-view"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="flex flex-col justify-center items-center min-h-[300px] text-center"
            >
              <div className="w-full max-w-lg border border-emerald-500/50 bg-emerald-500/5 rounded-2xl p-6 mb-8 flex flex-col items-center">
                <div className="flex items-center gap-3 mb-2">
                  <CheckCircle className="w-6 h-6 text-emerald-400" />
                  <h3 className="text-2xl font-bold text-emerald-400">
                    {fileState.totalCount}개 파일 준비 완료
                  </h3>
                </div>
                <div className="text-slate-400 text-sm mt-2 flex flex-wrap justify-center gap-3">
                  {Object.entries(fileState.extCounts || {}).map(([ext, count]) => (
                    <span key={ext}>{ext.replace('.', '')}:{count}</span>
                  ))}
                </div>
              </div>

              <button
                onClick={triggerConversion}
                className="bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-4 px-12 rounded-2xl shadow-lg shadow-indigo-900/50 transition-all active:scale-95"
              >
                🚀 JPG 변환 시작
              </button>
              <button
                onClick={resetState}
                className="mt-4 text-xs text-slate-500 hover:text-slate-300 transition-colors"
              >
                취소하고 새로 고침
              </button>
            </motion.div>
          )}

          {/* CONVERTING state view */}
          {fileState.status === "converting" && (
            <motion.div
              key="converting-view"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex flex-col justify-center items-center min-h-[300px] gap-8 w-full max-w-lg mx-auto"
            >
              <div className="relative w-24 h-24 flex items-center justify-center">
                <RefreshCw className="w-12 h-12 text-indigo-400 animate-spin" />
              </div>
              <div className="text-center space-y-2 w-full">
                <h3 className="text-white font-semibold animate-pulse">일괄 변환 중...</h3>
                <p className="text-slate-400 text-xs">전체 {fileState.totalCount} | 완료 {fileState.successCount + fileState.failCount}</p>
                
                {/* Progress Bar */}
                <div className="w-full bg-slate-800 rounded-full h-3 mt-4 overflow-hidden border border-slate-700">
                  <div 
                    className="bg-emerald-500 h-3 transition-all duration-300"
                    style={{ width: `${Math.round(((fileState.successCount + fileState.failCount) / fileState.totalCount) * 100)}%` }}
                  />
                </div>
              </div>
            </motion.div>
          )}

          {/* SUCCESS view */}
          {fileState.status === "success" && (
            <motion.div
              key="success-view"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="flex flex-col gap-8 items-center text-center"
            >
              <div className="space-y-2">
                <div className={`w-16 h-16 rounded-full flex items-center justify-center mx-auto mb-4 ${fileState.failCount === 0 ? 'bg-emerald-500/20' : 'bg-amber-500/20'}`}>
                  {fileState.failCount === 0 ? (
                    <CheckCircle className="w-8 h-8 text-emerald-400" />
                  ) : (
                    <AlertCircle className="w-8 h-8 text-amber-400" />
                  )}
                </div>
                <h3 className="text-2xl font-bold text-white">변환 작업 종료</h3>
                <p className="text-slate-400">
                  성공: {fileState.successCount}건 | 실패: {fileState.failCount}건
                </p>
              </div>

              <div className="bg-white/5 border border-indigo-500/30 text-indigo-400 text-sm py-4 px-6 rounded-xl w-full max-w-lg cursor-pointer hover:bg-white/10 transition-colors" onClick={openFolder}>
                 저장 위치:<br/>
                 <span className="font-bold break-all text-xs text-slate-300">{fileState.outputDir}</span>
              </div>

              <button
                onClick={resetState}
                className="w-full max-w-lg bg-white/5 border border-white/10 text-slate-300 py-3 rounded-xl font-semibold hover:bg-white/10 transition-colors text-sm"
              >
                🧹 초기화 후 다른 폴더 열기
              </button>
            </motion.div>
          )}

          {/* ERROR state view */}
          {fileState.status === "error" && (
            <motion.div
              key="error-view"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex flex-col items-center justify-center min-h-[300px] text-center"
            >
              <AlertCircle className="w-16 h-16 text-red-400 mb-4" />
              <h3 className="text-xl font-bold text-red-400">오류 발생</h3>
              <p className="text-slate-400 text-sm mt-2 max-w-sm">{fileState.errorMessage}</p>
              <button
                onClick={resetState}
                className="mt-8 text-indigo-400 underline text-sm"
              >
                다시 시도하기
              </button>
            </motion.div>
          )}

        </AnimatePresence>
      </div>

    </div>
  );
}
