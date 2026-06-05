import React, { useState, useRef, DragEvent } from "react";
import { motion, AnimatePresence } from "motion/react";
import { UploadCloud, FileText, Settings, Shield, Sliders, RefreshCw, Terminal, Download, AlertCircle, Info, FileUp, CheckCircle } from "lucide-react";
import { ActiveFileState, TargetOption } from "../types";

const TARGET_OPTIONS: TargetOption[] = [
  { id: "2mb", label: "2 MB", value: 2, badge: "가벼운 전송" },
  { id: "5mb", label: "5 MB", value: 5, badge: "이메일 적합" },
  { id: "10mb", label: "10 MB", value: 10, badge: "포털 대용량" },
  { id: "custom", label: "직접 입력", value: 15, badge: "목표 직접 지정" },
];

export default function PdfCompressor() {
  const [targetSizeId, setTargetSizeId] = useState<string>("5mb");
  const [customSize, setCustomSize] = useState<string>("7");
  const [dragActive, setDragActive] = useState<boolean>(false);
  const [fileState, setFileState] = useState<ActiveFileState>({
    file: null as any,
    name: "",
    size: 0,
    status: "idle",
  });
  
  const [progress, setProgress] = useState<number>(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Determine current active target size in MB
  const getSelectedTargetSizeMB = (): number => {
    if (targetSizeId === "custom") {
      const parsed = parseFloat(customSize);
      return isNaN(parsed) || parsed <= 0 ? 5 : parsed;
    }
    const option = TARGET_OPTIONS.find((o) => o.id === targetSizeId);
    return option ? option.value : 5;
  };

  // Simulated progress simulation for target limit iterative compiler
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
          return prev + Math.floor(Math.random() * 4) + 2; // Jump quickly to 30%
        } else if (prev < 70) {
          return prev + Math.floor(Math.random() * 2) + 1; // Standard speed up to 70%
        } else if (prev < 95) {
          return prev + (Math.random() > 0.65 ? 1 : 0); // slow down as it gets closer
        }
        return prev;
      });

      const nextDelay = Math.floor(Math.random() * 150) + 100;
      timer = setTimeout(updateProgress, nextDelay);
    };

    timer = setTimeout(updateProgress, 120);

    return () => {
      clearTimeout(timer);
    };
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
    setFileState({
      file,
      name: file.name,
      size: file.size,
      status: "selected",
    });
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
    
    setFileState(prev => ({
      ...prev,
      status: "compressing",
    }));

    const targetMB = getSelectedTargetSizeMB();
    const formData = new FormData();
    formData.append("targetSize", targetMB.toString());
    
    // Pass the absolute file path if running in Electron (append BEFORE file)
    let originalPath = "";
    // @ts-ignore
    if (window.electronAPI) {
      // @ts-ignore
      originalPath = window.electronAPI.getPathForFile(file);
    } else if (file.path) {
      // @ts-ignore
      originalPath = file.path;
    }
    
    if (originalPath) {
      formData.append("originalPath", originalPath);
    }
    
    formData.append("file", file);

    try {
      const response = await fetch("/api/compress", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.error || "압축 중 알 수 없는 오류 발생");
      }

      const result = await response.json();
      if (result.success) {
        setProgress(100);
        // Let user see 100% progress before transition
        await new Promise((resolve) => setTimeout(resolve, 600));
        setFileState((prev) => ({
          ...prev,
          status: "success",
          result: result,
        }));
        
        // If it was saved directly by backend, we don't need to trigger download
        if (!result.savedDirectly) {
            const link = document.createElement("a");
            // @ts-ignore
            link.href = `/api/download/${result.downloadId}?filename=${encodeURIComponent(result.fileName)}`;
            link.download = result.fileName;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }
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

  const downloadCompressedFile = () => {
    if (!fileState.result || !fileState.result.downloadId) return;
    
    const a = document.createElement("a");
    // @ts-ignore
    a.href = `/api/download/${fileState.result.downloadId}?filename=${encodeURIComponent(fileState.result.fileName)}`;
    a.download = fileState.result.fileName || "compressed.pdf";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  const resetState = () => {
    setFileState({
      file: null as any,
      name: "",
      size: 0,
      status: "idle",
    });
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const formatSize = (bytes: number): string => {
    if (bytes === 0) return "0 Bytes";
    const k = 1024;
    const sizes = ["Bytes", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
  };

  return (
    <div className="max-w-4xl mx-auto w-full space-y-6 relative z-10 flex-1 flex flex-col pt-4">
      
      {/* Target Size Selector */}
      <div className="bg-black/20 p-6 border border-white/5 rounded-2xl">
        <div className="space-y-6">
          {/* Section Title */}
          <div>
            <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-4 flex items-center gap-2">
              <Sliders className="w-3.5 h-3.5 text-blue-400" />
              목표 파일 크기 설정
            </h3>
            
            {/* Target Options Selector */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {TARGET_OPTIONS.map((option) => (
                <button
                  key={option.id}
                  id={`target-opt-${option.id}`}
                  onClick={() => setTargetSizeId(option.id)}
                  disabled={fileState.status === "compressing"}
                  className={`px-4 py-3 rounded-xl border transition-all duration-200 cursor-pointer ${
                    targetSizeId === option.id
                      ? "bg-blue-500/20 border-blue-400/30 text-blue-400 font-semibold shadow-md shadow-blue-500/15"
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

            {/* Custom field rendering */}
            <AnimatePresence>
              {targetSizeId === "custom" && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <div className="mt-3 bg-white/5 border border-white/10 p-3 rounded-xl flex items-center gap-3">
                    <input
                      id="custom-size-input"
                      type="number"
                      step="0.1"
                      min="0.5"
                      max="500"
                      value={customSize}
                      disabled={fileState.status === "compressing"}
                      onChange={(e) => setCustomSize(e.target.value)}
                      className="bg-black/40 border border-white/10 rounded-lg py-1.5 px-3 text-sm focus:outline-none focus:border-blue-400 text-blue-300 w-32"
                      placeholder="용량 입력"
                    />
                    <span className="text-xs text-slate-400">MB 단위로 입력하세요</span>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </div>

      {/* Main Work Workbench */}
      <div className="bg-white/5 border border-white/5 rounded-3xl p-8 flex-1">
        <AnimatePresence mode="wait">
          
          {/* IDLE state view: Drop files */}
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
                    ? "border-blue-400 bg-blue-500/10"
                    : "border-white/10 hover:border-white/20 hover:bg-white/[0.03]"
                }`}
              >
                <input
                  id="file-input"
                  type="file"
                  ref={fileInputRef}
                  onChange={handleFileChange}
                  accept=".pdf,application/pdf"
                  className="hidden"
                />

                <div className="w-16 h-16 rounded-full bg-blue-500/20 flex items-center justify-center mb-4">
                  <FileUp className="w-8 h-8 text-blue-400" />
                </div>

                <p className="text-white font-medium">PDF 파일을 여기에 드래그하거나 클릭</p>
                <p className="text-slate-500 text-xs mt-1">최대 500MB까지 지원</p>
              </div>
            </motion.div>
          )}

          {/* SELECTED state view: Ready to compress */}
          {fileState.status === "selected" && (
            <motion.div
              key="selected-view"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="flex flex-col justify-center items-center min-h-[300px] text-center"
            >
              <div className="w-20 h-20 rounded-full bg-blue-500/20 flex items-center justify-center mb-6">
                <FileText className="w-10 h-10 text-blue-400" />
              </div>
              <h3 className="text-xl font-bold text-white mb-2">{fileState.name}</h3>
              <p className="text-slate-400 text-sm mb-8">
                원본 크기: {formatSize(fileState.size)}
              </p>
              <button
                onClick={triggerCompression}
                className="bg-blue-600 hover:bg-blue-500 text-white font-bold py-4 px-12 rounded-2xl shadow-lg shadow-blue-900/50 transition-all active:scale-95"
              >
                압축 시작하기
              </button>
              <button
                onClick={resetState}
                className="mt-4 text-xs text-slate-500 hover:text-slate-300 transition-colors"
              >
                취소
              </button>
            </motion.div>
          )}

          {/* COMPRESSING state view */}
          {fileState.status === "compressing" && (
            <motion.div
              key="compressing-view"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex flex-col justify-center items-center min-h-[300px] gap-8"
            >
              <div className="relative w-32 h-32">
                 <svg className="w-full h-full transform -rotate-90">
                    <circle cx="64" cy="64" r="58" className="stroke-white/10" strokeWidth="8" fill="transparent" />
                    <circle cx="64" cy="64" r="58" className="stroke-blue-400" strokeWidth="8" fill="transparent" strokeDasharray={2 * Math.PI * 58} strokeDashoffset={2 * Math.PI * 58 * (1 - progress / 100)} strokeLinecap="round" />
                 </svg>
                 <div className="absolute inset-0 flex items-center justify-center font-bold text-2xl tracking-tighter">
                   {progress}%
                 </div>
              </div>
              <div className="text-center space-y-2">
                <h3 className="text-white font-semibold animate-pulse">PDF 압축 최적화 엔진 실행 중...</h3>
                <p className="text-slate-400 text-xs text-center max-w-xs leading-relaxed">복잡한 PDF 구조를 분석하고 리인코딩하는 중입니다. 잠시만 기다려주세요.</p>
              </div>
            </motion.div>
          )}

          {/* SUCCESS view */}
          {fileState.status === "success" && fileState.result && (
            <motion.div
              key="success-view"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="flex flex-col gap-8"
            >
              <div className="text-center space-y-2">
                <div className="w-16 h-16 bg-emerald-500/20 rounded-full flex items-center justify-center mx-auto mb-4">
                  <CheckCircle className="w-8 h-8 text-emerald-400" />
                </div>
                <h3 className="text-2xl font-bold text-white">압축 성공</h3>
                <p className="text-slate-400">PDF 최적화 작업이 완료되었습니다.</p>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="bg-white/5 p-4 rounded-xl text-center border border-white/5">
                  <div className="text-slate-400 text-[10px] uppercase">원본</div>
                  <div className="text-white font-bold">{formatSize(fileState.result.originalSize)}</div>
                </div>
                <div className="bg-emerald-500/10 p-4 rounded-xl text-center border border-emerald-500/20">
                  <div className="text-emerald-400 text-[10px] uppercase">결과</div>
                  <div className="text-emerald-300 font-bold">{formatSize(fileState.result.compressedSize)}</div>
                </div>
              </div>

              <button
                onClick={downloadCompressedFile}
                className="w-full bg-blue-600 text-white py-4 rounded-xl font-bold flex items-center justify-center gap-2 hover:bg-blue-500 transition-colors"
              >
                <Download className="w-5 h-5" />
                다운로드
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
                className="mt-8 text-blue-400 underline text-sm"
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
