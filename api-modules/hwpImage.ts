import type { Express } from "express";
import type { ApiModule } from "./types";
import { getHwpWorkerPath, getResourcePath } from "./shared";
import { spawn, execFile } from "child_process";
import path from "path";
import fs from "fs";
import os from "os";
import util from "util";

const execFileAsync = util.promisify(execFile);

/**
 * HWP/HWPX 문서 내 이미지 변환 유틸리티의 백엔드 API 모듈.
 *
 * 엔드포인트:
 *   POST /api/hwp-image/scan    — 문서 내 이미지 목록 스캔
 *   POST /api/hwp-image/convert — 이미지 JPG 변환 (NDJSON 스트리밍)
 */

/**
 * 개발 모드에서 Python 스크립트를 직접 실행할 수 있는 경로를 반환합니다.
 * 패키지된 앱에서는 PyInstaller exe를 사용합니다.
 */
function getHwpWorkerCommand(): { cmd: string; isPython: boolean } {
  const exePath = getHwpWorkerPath();
  if (fs.existsSync(exePath)) {
    return { cmd: exePath, isPython: false };
  }
  // 개발 모드 fallback: python 직접 실행
  const pyScript = path.join(process.cwd(), "python_worker", "hwp_worker.py");
  if (fs.existsSync(pyScript)) {
    return { cmd: pyScript, isPython: true };
  }
  // 그래도 없으면 exe 경로 반환 (에러는 런타임에 발생)
  return { cmd: exePath, isPython: false };
}

export const hwpImageModule: ApiModule = {
  id: "hwp-image-converter",

  register(app: Express) {
    /**
     * POST /api/hwp-image/scan
     * Body: { path: string }
     * 응답: { success: true, images: [...], fileType: "hwp"|"hwpx" }
     */
    app.post("/api/hwp-image/scan", async (req, res) => {
      try {
        const filePath: string = req.body.path;
        if (!filePath || !fs.existsSync(filePath)) {
          return res.status(400).json({ error: "파일을 찾을 수 없습니다." });
        }

        const ext = path.extname(filePath).toLowerCase();
        if (ext !== ".hwp" && ext !== ".hwpx") {
          return res.status(400).json({ error: "HWP 또는 HWPX 파일만 지원합니다." });
        }

        const worker = getHwpWorkerCommand();
        const args = worker.isPython
          ? [worker.cmd, "--scan", "--input", filePath]
          : ["--scan", "--input", filePath];
        const cmd = worker.isPython ? "python" : worker.cmd;

        // 수백 MB급 HWP는 워커가 임시 복사 후 여는 구조라 30초로는 부족할 수 있다
        const { stdout, stderr } = await execFileAsync(cmd, args, {
          maxBuffer: 1024 * 1024 * 50,
          timeout: 180000,
        });

        // NDJSON에서 scan 이벤트 파싱
        const lines = stdout.split("\n").filter((l: string) => l.trim());
        let scanResult: any = null;

        for (const line of lines) {
          try {
            const data = JSON.parse(line);
            if (data.event === "scan") {
              scanResult = data;
            } else if (data.event === "error") {
              return res.status(500).json({ error: data.error });
            }
          } catch {
            // JSON 파싱 오류 무시
          }
        }

        if (scanResult) {
          res.json({
            success: true,
            images: scanResult.images || [],
            fileType: ext.replace(".", ""),
          });
        } else {
          res.status(500).json({ error: "워커에서 스캔 결과를 받지 못했습니다." });
        }
      } catch (e: any) {
        console.error("HWP scan error:", e);
        // 파이썬 워커가 에러 JSON을 stdout에 출력하고 종료코드 1을 반환한 경우 e.stdout에 결과가 있습니다.
        if (e.stdout) {
          try {
            const lines = e.stdout.split("\n").filter((l: string) => l.trim());
            for (const line of lines) {
              const data = JSON.parse(line);
              if (data.event === "error" && data.error) {
                return res.status(500).json({ error: data.error });
              }
            }
          } catch {
            // ignore JSON parse error
          }
        }
        res.status(500).json({ error: "스캔 중 오류가 발생했습니다: " + e.message });
      }
    });

    /**
     * POST /api/hwp-image/convert
     * Body: { path: string, mode: "selective"|"all" }
     * 응답: NDJSON 스트리밍
     */
    app.post("/api/hwp-image/convert", async (req, res) => {
      try {
        const filePath: string = req.body.path;
        const mode: string = req.body.mode || "selective";
        const sizeAdjust: boolean = req.body.sizeAdjust === true;

        if (!filePath || !fs.existsSync(filePath)) {
          return res.status(400).json({ error: "파일을 찾을 수 없습니다." });
        }

        const ext = path.extname(filePath).toLowerCase();
        if (ext !== ".hwp" && ext !== ".hwpx") {
          return res.status(400).json({ error: "HWP 또는 HWPX 파일만 지원합니다." });
        }

        // 출력 경로: 원본 옆에 "변환_원본이름.hwp/hwpx"
        const dir = path.dirname(filePath);
        const baseName = path.basename(filePath, ext);
        const outputPath = path.join(dir, `변환_${baseName}${ext}`);

        const worker = getHwpWorkerCommand();
        const args = worker.isPython
          ? [worker.cmd, "--input", filePath, "--output", outputPath, "--mode", mode]
          : ["--input", filePath, "--output", outputPath, "--mode", mode];
        if (sizeAdjust) args.push("--size-adjust");
        const cmd = worker.isPython ? "python" : worker.cmd;

        res.setHeader("Content-Type", "application/x-ndjson; charset=utf-8");
        res.setHeader("Transfer-Encoding", "chunked");

        const child = spawn(cmd, args);

        child.stdout.on("data", (data) => {
          res.write(data);
        });

        child.stderr.on("data", (data) => {
          console.error(`HWP Worker stderr: ${data}`);
        });

        child.on("close", (code) => {
          // 워커가 비정상 종료한 경우 에러 이벤트 추가
          if (code !== 0 && code !== null) {
            res.write(
              JSON.stringify({
                event: "error",
                error: `워커가 비정상 종료되었습니다 (코드: ${code})`,
              }) + "\n"
            );
          }
          // 최종 출력 경로 정보 전달
          res.write(
            JSON.stringify({
              event: "complete",
              outputPath,
              outputDir: dir,
            }) + "\n"
          );
          res.end();
        });

        child.on("error", (err) => {
          console.error("HWP Worker spawn error:", err);
          res.write(
            JSON.stringify({
              event: "error",
              error: "워커 실행 실패: " + err.message,
            }) + "\n"
          );
          res.end();
        });
      } catch (e: any) {
        console.error("HWP convert error:", e);
        res.write(
          JSON.stringify({
            event: "error",
            error: "서버 내부 오류: " + e.message,
          }) + "\n"
        );
        res.end();
      }
    });
  },
};
