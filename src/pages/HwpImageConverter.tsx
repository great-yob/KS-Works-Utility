import React, { useState, useRef, DragEvent } from "react";
import {
  UploadCloud,
  Sliders,
  Terminal,
  AlertCircle,
  CheckCircle,
  RotateCcw,
  FolderOpen,
} from "lucide-react";

/* ─────────────── 타입 ─────────────── */

/** used=false면 문서가 참조하지 않는 그림이라 변환에서 제외됩니다. */
type ImageInfo = { name: string; format: string; size: number; used?: boolean };

type ConvertMode = "selective" | "all";

/**
 * 로그 한 줄.
 *  - tone  : 색상 계열. 실패/오류는 붉은 계열로 한눈에 띄게 합니다.
 *  - page  : 그림이 원본 문서에서 놓인 쪽 번호(워커가 레이아웃 정보로 계산).
 *            쪽도 쓰임새도 없으면 칩을 아예 그리지 않습니다.
 */
type LogTone = "info" | "success" | "fail" | "size";

type LogEntry = {
  tag: string;
  text: string;
  tone: LogTone;
  withPage?: boolean;
  page?: number;          // 문서에 인쇄되는 쪽 번호
  physicalPage?: number;  // 문서의 몇 번째 쪽 ('새 번호로 시작' 때문에 다를 때만)
  pages?: number[];       // 같은 그림이 여러 곳에 쓰인 경우
  pageNote?: string;      // 쪽을 못 매기는 쓰임새('채우기' 등)
};

const log = (
  tag: string,
  text: string,
  tone: LogTone = "info",
  extra: Partial<LogEntry> = {}
): LogEntry => ({ tag, text, tone, ...extra });

type HwpState = {
  status: "idle" | "ready" | "scanned" | "converting" | "success" | "error";
  filePath: string;
  fileName: string;
  fileType: string; // "hwp" | "hwpx"
  images: ImageInfo[];
  targetCount: number;
  totalCount: number;
  convertedCount: number;
  skippedCount: number;
  outputDir: string;
  outputPath: string;
  errorMessage?: string;
  logs: LogEntry[];
};

const INITIAL_STATE: HwpState = {
  status: "idle",
  filePath: "",
  fileName: "",
  fileType: "",
  images: [],
  targetCount: 0,
  totalCount: 0,
  convertedCount: 0,
  skippedCount: 0,
  outputDir: "",
  outputPath: "",
  logs: [],
};

/* ─────────────── 정리 옵션 정의 ─────────────── */

type OptionId = "all-size" | "all-format" | "sel-size" | "sel-format";

/**
 * 정리 옵션은 두 축의 조합입니다.
 *   범위: 전체(all) / 선택(selective, 문제 이미지만 — jpg/bmp/emf 제외)
 *   방식: JPG+사이즈(변환 후 사이즈 조정까지) / JPG(형식만 변환)
 * mode·sizeAdjust 둘 다 백엔드 /api/hwp-image/convert 로 전달됩니다.
 */
const CLEANUP_OPTIONS: {
  id: OptionId;
  label: string;
  desc: string;
  mode: ConvertMode;
  sizeAdjust: boolean;
}[] = [
  { id: "all-size", label: "전체 정리 (JPG+사이즈)", desc: "모든 이미지 한번에 정리", mode: "all", sizeAdjust: true },
  { id: "all-format", label: "전체 정리 (JPG)", desc: "모든 이미지 형식만 정리", mode: "all", sizeAdjust: false },
  { id: "sel-size", label: "선택 정리 (JPG+사이즈)", desc: "문제 이미지만 한번에 정리", mode: "selective", sizeAdjust: true },
  { id: "sel-format", label: "선택 정리 (JPG)", desc: "문제 이미지 형식만 정리", mode: "selective", sizeAdjust: false },
];

const DEFAULT_OPTION: OptionId = "all-size";

/** 선택 정리 시 제외할 형식(이미 정상인 형식) */
const EXCLUDE_SELECTIVE = new Set(["jpg", "jpeg", "bmp", "emf"]);

function countTargets(images: ImageInfo[], mode: ConvertMode): number {
  // 문서가 참조하지 않는 그림은 워커가 건드리지 않으므로 대상 수에서도 뺀다.
  const used = images.filter((img) => img.used !== false);
  if (mode === "all") return used.length;
  return used.filter((img) => !EXCLUDE_SELECTIVE.has(img.format)).length;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/* ─────────────── 로그 표시 ─────────────── */

/** 로그 줄 전체 / 대괄호 태그 / 쪽 칩의 색상 (실패는 붉은 계열로 즉시 눈에 띄게) */
const LOG_STYLES: Record<LogTone, { row: string; tag: string; chip: string }> = {
  info: { row: "text-slate-300", tag: "text-slate-500", chip: "bg-white/10 ring-white/15" },
  success: { row: "text-slate-300", tag: "text-teal-400/90", chip: "bg-teal-400/20 ring-teal-400/30" },
  size: { row: "text-slate-300", tag: "text-amber-400/90", chip: "bg-amber-400/20 ring-amber-400/30" },
  fail: {
    row: "text-red-300 bg-red-500/10 border-l-2 border-red-400/70 pl-1.5 -ml-1.5 rounded-r",
    tag: "text-red-400 font-bold",
    chip: "bg-red-500/30 ring-red-400/40",
  },
};

/** 칩 공통 모양 — 알약형, 볼드 없음, 줄 높이를 고정해 위아래 줄과 겹치지 않게 합니다. */
const CHIP_BASE =
  "shrink-0 inline-block px-1.5 rounded-full ring-1 text-[10px] leading-[15px] tabular-nums text-white";

/**
 * 그림이 원본 한글 문서에서 놓인 쪽 칩("3p").
 * 값은 **문서에 인쇄되는 쪽 번호**입니다('새 번호로 시작' 반영). 같은 그림이 여러 곳에
 * 쓰이면 쪽을 모두 나열합니다("3, 13, 45p").
 */
function PageChip({
  page,
  pages,
  pageNote,
  tone,
}: {
  page?: number;
  pages?: number[];
  pageNote?: string;
  tone: LogTone;
}) {
  if (typeof page !== "number") {
    // 쪽을 매길 수 없는 쓰임새(적용된 곳을 찾지 못한 그림 채우기, 그림 글머리표, 바탕쪽 등).
    // 실제로 쓰이는 그림이므로 변환은 그대로 하고, 어디에 쓰이는지만 알려 줍니다.
    if (!pageNote) return null;
    return <span className={`${CHIP_BASE} bg-sky-400/15 ring-sky-400/25`}>{pageNote}</span>;
  }
  const list = pages && pages.length > 1 ? pages.join(", ") : String(page);
  return <span className={`${CHIP_BASE} ${LOG_STYLES[tone].chip}`}>{list}p</span>;
}

/* ─────────────── 컴포넌트 ─────────────── */

export default function HwpImageConverter() {
  const [state, setState] = useState<HwpState>(INITIAL_STATE);
  const [optionId, setOptionId] = useState<OptionId>(DEFAULT_OPTION);
  const [dragActive, setDragActive] = useState(false);

  // 선택된 정리 옵션에서 백엔드로 보낼 변환 범위(mode)와 사이즈 조정 여부를 파생합니다.
  const selectedOption = CLEANUP_OPTIONS.find((o) => o.id === optionId);
  const mode: ConvertMode = selectedOption?.mode ?? "selective";
  const sizeAdjust: boolean = selectedOption?.sizeAdjust ?? false;
  const fileInputRef = useRef<HTMLInputElement>(null);

  /* ── 파일 드롭/선택 ── */

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

  const selectFile = (filePath: string, fileName: string) => {
    const ext = fileName.split(".").pop()?.toLowerCase() || "";
    if (ext !== "hwp" && ext !== "hwpx") {
      setState((prev) => ({
        ...prev,
        status: "error",
        errorMessage: "HWP 또는 HWPX 파일만 지원합니다.",
      }));
      return;
    }

    setState((prev) => ({
      ...INITIAL_STATE,
      status: "ready",
      filePath,
      fileName,
      fileType: ext,
      logs: [log("준비", `${fileName} 파일이 추가되었습니다. '시작' 버튼을 누르면 작업을 진행합니다.`)],
    }));
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const file = e.dataTransfer.files[0];
      const filePath = (window as any).electronAPI
        ? (window as any).electronAPI.getPathForFile(file)
        : (file as any).path;
      if (filePath) {
        selectFile(filePath, file.name);
      }
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const file = e.target.files[0];
      const filePath = (window as any).electronAPI
        ? (window as any).electronAPI.getPathForFile(file)
        : (file as any).path;
      if (filePath) {
        selectFile(filePath, file.name);
      }
    }
  };

  /* ── 정리 옵션 변경 시 대상 수 재계산 ── */

  const changeOption = (newId: OptionId) => {
    setOptionId(newId);
    const newMode = CLEANUP_OPTIONS.find((o) => o.id === newId)?.mode ?? "selective";
    if (state.images.length > 0) {
      const newTarget = countTargets(state.images, newMode);
      setState((prev) => ({
        ...prev,
        targetCount: newTarget,
        logs: [
          ...prev.logs.filter((l) => l.tag !== "대상"),
          log("대상", `변환 대상: ${newTarget}개`),
        ],
      }));
    }
  };

  /* ── 시작 액션 (스캔 후 변환 자동 진행) ── */

  const startAction = async () => {
    if (!state.filePath) return;

    setState((prev) => ({
      ...prev,
      status: "converting", // 스캔 중에도 진행 중 상태로 표시
      logs: [...prev.logs, log("스캔", "문서 내 이미지를 분석 중...")],
    }));

    try {
      const res = await fetch("/api/hwp-image/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.filePath }),
      });
      const data = await res.json();

      if (data.success && data.images) {
        const targetCount = countTargets(data.images, mode);
        const formatCounts: Record<string, number> = {};
        data.images.forEach((img: ImageInfo) => {
          formatCounts[img.format] = (formatCounts[img.format] || 0) + 1;
        });

        const summary = Object.entries(formatCounts)
          .map(([fmt, cnt]) => `${fmt}:${cnt}`)
          .join(", ");

        // 스캔 결과 저장 후 바로 변환 시작
        setState((prev) => ({
          ...prev,
          status: "scanned", // 임시로 scanned 상태를 거침
          images: data.images,
          targetCount,
          totalCount: data.images.length,
          logs: [
            ...prev.logs,
            log("스캔 완료", `전체 ${data.images.length}개 이미지 (${summary})`),
            log("대상", `변환 대상: ${targetCount}개`),
          ],
        }));

        if (targetCount > 0) {
          // 상태 업데이트를 기다렸다가 변환 트리거 (timeout으로 약간의 딜레이)
          setTimeout(() => triggerConversion(targetCount), 100);
        } else {
          setState((prev) => ({
            ...prev,
            status: "success",
            logs: [...prev.logs, log("안내", "변환 대상 이미지가 없습니다.")],
          }));
        }
      } else {
        setState((prev) => ({
          ...prev,
          status: "error",
          errorMessage: data.error || "이미지를 찾을 수 없습니다.",
        }));
      }
    } catch (err: any) {
      setState((prev) => ({
        ...prev,
        status: "error",
        errorMessage: "스캔 중 오류가 발생했습니다.",
      }));
    }
  };

  /* ── 변환 시작 ── */

  const triggerConversion = async (tCount?: number) => {
    const targets = tCount !== undefined ? tCount : state.targetCount;
    if (!state.filePath || targets === 0) return;

    setState((prev) => ({
      ...prev,
      status: "converting",
      convertedCount: 0,
      skippedCount: 0,
      logs: [...prev.logs, log("시작", `${state.fileName} 이미지 변환을 시작합니다...`)],
    }));

    try {
      const response = await fetch("/api/hwp-image/convert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: state.filePath, mode, sizeAdjust }),
      });

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No reader from response");
      const decoder = new TextDecoder("utf-8");

      let converted = 0;
      let skippedCount = 0;
      let outputDir = "";
      let outputPath = "";
      // 상태가 클로저에 묶이지 않도록 여기서 추가
      let logs = [...state.logs, log("시작", `${state.fileName} 이미지 변환을 시작합니다...`)];

      let buffer = ""; // NDJSON 한 줄이 청크 경계에서 잘려도 이어붙이도록 버퍼링
      while (true) {
        const { done, value } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        const parts = buffer.split("\n");
        buffer = done ? "" : parts.pop() ?? "";
        const lines = parts.filter((l) => l.trim());

        for (const line of lines) {
          try {
            const data = JSON.parse(line);

            if (data.event === "progress") {
              const pageInfo = {
                withPage: true,
                page: data.page as number | undefined,
                physicalPage: data.physicalPage as number | undefined,
                pages: data.pages as number[] | undefined,
                pageNote: data.pageNote as string | undefined,
              };
              if (data.success) {
                converted++;
                logs = [
                  ...logs,
                  log("성공", `${data.name}: ${data.from} → ${data.to}`, "success", pageInfo),
                ];
              } else {
                skippedCount++;
                logs = [
                  ...logs,
                  log("실패", `${data.name}: ${data.message || "알 수 없는 오류"}`, "fail", pageInfo),
                ];
              }
              setState((prev) => ({
                ...prev,
                convertedCount: converted,
                skippedCount: skippedCount,
                logs,
              }));
            } else if (data.event === "done") {
              logs = [
                ...logs,
                log("완료", `변환 ${data.totalConverted}건 / 건너뜀 ${data.totalSkipped}건`),
              ];
              setState((prev) => ({ ...prev, logs }));
            } else if (data.event === "size") {
              logs = [
                ...logs,
                log("사이즈", `${data.name ? `${data.name}: ` : ""}${data.message}`, "size", {
                  withPage: true,
                  page: data.page as number | undefined,
                  physicalPage: data.physicalPage as number | undefined,
                  pages: data.pages as number[] | undefined,
                  pageNote: data.pageNote as string | undefined,
                }),
              ];
              setState((prev) => ({ ...prev, logs }));
            } else if (data.event === "unused") {
              const names = (data.names as string[] | undefined) ?? [];
              const shown = names.slice(0, 5).join(", ");
              const more = data.count > names.length ? ` 외 ${data.count - names.length}개` : "";
              logs = [
                ...logs,
                log(
                  "제외",
                  `문서가 사용하지 않는 그림 ${data.count}개는 변환하지 않았습니다 (${shown}${more})`
                ),
              ];
              setState((prev) => ({ ...prev, logs }));
            } else if (data.event === "sizeDone") {
              logs = [
                ...logs,
                log("사이즈 완료", `조정 ${data.adjusted}건 / 건너뜀 ${data.skipped}건`),
              ];
              setState((prev) => ({ ...prev, logs }));
            } else if (data.event === "complete") {
              outputDir = data.outputDir || "";
              outputPath = data.outputPath || "";
              logs = [...logs, log("저장", outputPath)];
            } else if (data.event === "error") {
              logs = [...logs, log("오류", data.error, "fail")];
            }
          } catch {
            // 잘못된 JSON 라인은 무시
          }
        }
        if (done) break;
      }

      setState((prev) => ({
        ...prev,
        status: "success",
        convertedCount: converted,
        skippedCount: skippedCount,
        outputDir,
        outputPath,
        logs,
      }));
    } catch (err: any) {
      setState((prev) => ({
        ...prev,
        status: "error",
        errorMessage: "변환 통신 중 오류가 발생했습니다.",
      }));
    }
  };

  /* ── 폴더 열기 ── */

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

  /* ── 초기화 ── */

  const resetOptions = () => setOptionId(DEFAULT_OPTION);

  const resetFiles = () => {
    setState(INITIAL_STATE);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const resetResult = () => {
    setState((prev) =>
      prev.images.length
        ? {
            ...prev,
            status: "scanned",
            convertedCount: 0,
            skippedCount: 0,
            outputDir: "",
            outputPath: "",
            errorMessage: undefined,
            logs: prev.logs.slice(0, 2), // 스캔 로그만 유지
          }
        : INITIAL_STATE
    );
  };

  /* ── 렌더링 변수 ── */

  const busy = state.status === "converting";
  const total = state.targetCount || 1;
  const done = state.convertedCount + state.skippedCount;
  const donePct = state.targetCount ? Math.round((done / total) * 100) : 0;
  const R = 34;
  const C = 2 * Math.PI * R;

  return (
    <div className="grid grid-cols-[3fr_7fr] gap-4 flex-1 min-h-0 relative z-10">
      {/* ===== 옵션 ===== */}
      <section className="bg-black/20 border border-white/5 rounded-2xl p-4 flex flex-col min-h-0">
        <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-3 flex items-center gap-2 shrink-0">
          <Sliders className="w-3.5 h-3.5 text-teal-400" /> 옵션
        </h3>

        <div className="flex-1 overflow-auto terminal-scroll pr-1 -mr-1">
          <div className="grid grid-cols-1 gap-2">
            {CLEANUP_OPTIONS.map((opt) => (
              <button
                key={opt.id}
                onClick={() => changeOption(opt.id)}
                disabled={busy}
                className={`px-3 py-2.5 rounded-xl border transition-all text-left no-drag disabled:opacity-50 ${
                  optionId === opt.id
                    ? "bg-teal-500/20 border-teal-400/30 text-teal-400 font-semibold shadow-md shadow-teal-500/15"
                    : "bg-white/5 border-transparent hover:bg-white/10 text-slate-300 font-medium"
                }`}
              >
                <span className="block text-sm">{opt.label}</span>
                <span className="text-[10px] opacity-70">{opt.desc}</span>
              </button>
            ))}
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
        {/* ----- 파일 ----- */}
        <section className="bg-white/[0.03] border border-white/5 rounded-2xl p-4 flex flex-col min-h-0">
          <h3 className="text-white/40 text-[10px] font-bold uppercase tracking-widest mb-2 flex items-center gap-2 shrink-0">
            <UploadCloud className="w-3.5 h-3.5 text-teal-400" /> HWP / HWPX 파일
          </h3>

          <div className="flex-1 flex flex-col min-h-0">
            {state.status === "idle" ? (
              <div
                id="hwp-dropzone"
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`flex-1 min-h-[70px] border-2 border-dashed rounded-2xl flex flex-col items-center justify-center text-center px-3 transition-all cursor-pointer no-drag ${
                  dragActive
                    ? "border-teal-400 bg-teal-500/10"
                    : "border-white/10 hover:border-white/20 hover:bg-white/[0.03]"
                }`}
              >
                <input
                  id="hwp-file-input"
                  type="file"
                  ref={fileInputRef}
                  onChange={handleFileChange}
                  accept=".hwp,.hwpx"
                  className="hidden"
                />
                <UploadCloud className="w-7 h-7 text-teal-400 mb-1.5" />
                <p className="text-white text-xs font-medium">HWP/HWPX 파일 드래그</p>
                <p className="text-slate-500 text-[10px] mt-0.5">한글 2010(.hwp) · 한글 2022(.hwpx)</p>
              </div>
            ) : (
              <div className="flex-1 flex flex-col gap-2 min-h-0 justify-center">
                <div className="bg-teal-500/5 border border-teal-500/40 rounded-xl p-2.5 text-center">
                  <div className="flex items-center justify-center gap-1.5 text-teal-400 font-bold text-sm">
                    <CheckCircle className="w-4 h-4" />
                    {state.fileName}
                  </div>
                  {state.status !== "ready" && (
                    <>
                      <div className="text-slate-400 text-[10px] mt-1">
                        {state.fileType.toUpperCase()} · 전체 {state.totalCount}개 이미지 · 변환 대상{" "}
                        <span className="text-teal-400 font-semibold">{state.targetCount}개</span>
                      </div>
                      {state.images.length > 0 && (
                        <div className="text-slate-500 text-[10px] mt-1 flex flex-wrap justify-center gap-x-2 gap-y-0.5">
                          {(() => {
                            const counts: Record<string, number> = {};
                            state.images.forEach((img) => {
                              counts[img.format] = (counts[img.format] || 0) + 1;
                            });
                            return Object.entries(counts).map(([fmt, cnt]) => (
                              <span key={fmt}>{fmt}:{cnt}</span>
                            ));
                          })()}
                        </div>
                      )}
                    </>
                  )}
                  {state.status === "ready" && (
                    <div className="text-slate-400 text-[10px] mt-1">
                      {state.fileType.toUpperCase()} 문서 대기 중
                    </div>
                  )}
                </div>
                {state.status === "ready" && (
                  <button
                    onClick={startAction}
                    className="w-full bg-amber-500 hover:bg-amber-400 text-slate-900 font-bold py-2.5 rounded-xl shadow-lg shadow-amber-900/30 transition-all active:scale-95 no-drag"
                  >
                    변환 시작
                  </button>
                )}
                {state.status === "scanned" && state.targetCount === 0 && (
                  <p className="text-slate-500 text-xs text-center">변환 대상 이미지가 없습니다</p>
                )}
                {state.status === "converting" && (
                  <p className="text-amber-400 text-xs text-center animate-pulse">
                    {state.targetCount > 0 ? "변환 진행 중…" : "분석 진행 중…"}
                  </p>
                )}
                {state.status === "success" && (
                  <p className="text-teal-400 text-xs text-center flex items-center justify-center gap-1">
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
            <Terminal className="w-3.5 h-3.5 text-teal-400" /> 진행 및 결과
          </h3>

          {/* status block */}
          <div className="shrink-0 mb-3 flex items-center justify-center min-h-[56px]">
            {state.status === "converting" && (
              <div className="flex items-center gap-4">
                <div className="relative w-20 h-20">
                  <svg className="w-full h-full transform -rotate-90">
                    <circle cx="40" cy="40" r={R} className="stroke-white/10" strokeWidth="6" fill="transparent" />
                    <circle
                      cx="40"
                      cy="40"
                      r={R}
                      className="stroke-amber-400"
                      strokeWidth="6"
                      fill="transparent"
                      strokeDasharray={C}
                      strokeDashoffset={C * (1 - donePct / 100)}
                      strokeLinecap="round"
                    />
                  </svg>
                  <div className="absolute inset-0 flex items-center justify-center font-bold text-base">{donePct}%</div>
                </div>
                <div className="text-left">
                  <p className="text-amber-400 text-sm font-semibold animate-pulse">변환 중…</p>
                  <p className="text-slate-400 text-xs mt-1">
                    대상 {state.targetCount} · 완료 {done}
                  </p>
                </div>
              </div>
            )}
            {state.status === "success" && (
              <div className="flex flex-wrap items-center justify-center gap-2">
                <span
                  className={`flex items-center gap-1 font-bold text-sm ${
                    state.skippedCount === 0 ? "text-teal-400" : "text-red-400"
                  }`}
                >
                  {state.skippedCount === 0 ? (
                    <CheckCircle className="w-5 h-5" />
                  ) : (
                    <AlertCircle className="w-5 h-5" />
                  )}{" "}
                  변환 종료
                </span>
                <span className="bg-teal-500/10 text-teal-300 px-2.5 py-1 rounded-lg text-xs font-semibold">
                  성공 {state.convertedCount}
                </span>
                {state.skippedCount > 0 && (
                  <span className="bg-red-500/15 text-red-300 ring-1 ring-red-400/30 px-2.5 py-1 rounded-lg text-xs font-semibold">
                    실패 {state.skippedCount}
                  </span>
                )}
              </div>
            )}
            {state.status === "error" && (
              <div className="flex flex-col items-center gap-1 text-center">
                <AlertCircle className="w-7 h-7 text-red-400" />
                <p className="text-slate-400 text-xs px-2">{state.errorMessage}</p>
              </div>
            )}
            {(state.status === "idle" || state.status === "scanned") && (
              <p className="text-slate-600 text-xs text-center px-2">
                {state.status === "scanned"
                  ? "'변환 시작'을 누르면 진행 상황이 표시됩니다"
                  : "HWP/HWPX 파일을 추가하면 시작할 수 있습니다"}
              </p>
            )}
          </div>

          {/* log window */}
          <div className="flex-1 min-h-0 bg-black/30 border border-white/5 rounded-xl p-3 overflow-auto terminal-scroll font-mono text-[11px] leading-5 space-y-1">
            {state.logs.length ? (
              state.logs.map((l, i) => (
                <div key={i} className={`flex items-baseline gap-1.5 ${LOG_STYLES[l.tone].row}`}>
                  <span className={`shrink-0 ${LOG_STYLES[l.tone].tag}`}>[{l.tag}]</span>
                  {l.withPage && (
                    <PageChip
                      page={l.page}
                      pages={l.pages}
                      pageNote={l.pageNote}
                      tone={l.tone}
                    />
                  )}
                  <span className="whitespace-pre-wrap break-all">{l.text}</span>
                </div>
              ))
            ) : (
              <span className="text-slate-600">로그가 여기에 표시됩니다…</span>
            )}
          </div>

          {/* actions */}
          <div className="flex gap-2 mt-3 shrink-0">
            {state.status === "success" && state.outputDir && (
              <button
                onClick={() => openFolderPath(state.outputDir)}
                className="flex-1 py-2 rounded-lg bg-teal-600 hover:bg-teal-500 text-white text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors no-drag"
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
