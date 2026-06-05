import express from "express";
import path from "path";
import { fileURLToPath } from "url";
import { execFile, spawn } from "child_process";
import util from "util";

const execFileAsync = util.promisify(execFile);
import fs from "fs";
import os from "os";
import { tempDir, upload, getGhostscriptPath, getImageWorkerPath } from "./api-modules/shared";
import { registerModules } from "./api-modules/registry";
// Multi-environment path safety
let currentDirName = "";
try {
  const filename = fileURLToPath(import.meta.url);
  currentDirName = path.dirname(filename);
} catch (e) {
  currentDirName = process.cwd();
}

const app = express();
const PORT = 3000;

// Body size configuration for handling PDF files
app.use(express.json({ limit: "500mb" }));
app.use(express.urlencoded({ limit: "500mb", extended: true }));

// tempDir, the multer `upload` instance, and native-binary path helpers now live
// in api-modules/shared.ts so every utility module resolves them the same way.

// REST API endpoint for iterative PDF auto-compression
app.post("/api/compress", upload.single("file"), async (req, res) => {
  try {
    const file = req.file;
    if (!file) {
      return res.status(400).json({ error: "PDF 파일을 업로드해주세요." });
    }

    const originalSize = file.size;
    const targetMB = parseFloat((req.body.targetSize || "5").toString());
    const targetBytes = targetMB * 1024 * 1024;

    const logs: string[] = [];
    logs.push(`[1단계] Ghostscript PDF 엔진 분석 시작 (원본 대략: ${(originalSize / (1024 * 1024)).toFixed(2)} MB)`);

    const ratio = targetBytes / originalSize;
    
    let config;
    if (ratio >= 0.5) {
      config = { name: "150 DPI / Color / JPEG 70 (아크로뱃 품질)", dpi: 150, jpegQ: 70 };
    } else if (ratio >= 0.2) {
      config = { name: "120 DPI / Color / JPEG 50 (컬러 화질 타협)", dpi: 120, jpegQ: 50 };
    } else if (ratio >= 0.05) {
      config = { name: "100 DPI / Color / JPEG 40 (컬러 최소 가독성)", dpi: 100, jpegQ: 40 };
    } else {
      config = { name: "72 DPI / Color / JPEG 20 (컬러 강제 초고압축)", dpi: 72, jpegQ: 20 };
    }

    const gsPath = getGhostscriptPath();
    let currentSize = originalSize;
    let outputPath = "";
    
    let currentDpi = config.dpi;
    let currentJpegQ = config.jpegQ;
    let attempt = 0;

    while (attempt < 10) {
      attempt++;
      logs.push(`[설정] 렌더링 시도 ${attempt}: ${currentDpi} DPI / Color / JPEG ${currentJpegQ}`);

      const outputFilename = `compressed_${Date.now()}_${attempt}.pdf`;
      outputPath = path.join(tempDir, outputFilename);
        
      const gsArgs = [
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        `-dColorImageResolution=${currentDpi}`,
        `-dGrayImageResolution=${currentDpi}`,
        `-dMonoImageResolution=${currentDpi}`,
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dAutoFilterColorImages=false",
        "-dAutoFilterGrayImages=false",
        "-dColorImageFilter=/DCTEncode",
        "-dGrayImageFilter=/DCTEncode",
        `-dJPEGQ=${currentJpegQ}`,
        `-sOutputFile=${outputPath}`,
        file.path
      ];

      try {
        await execFileAsync(gsPath, gsArgs, { maxBuffer: 1024 * 1024 * 100 });
        const stat = fs.statSync(outputPath);
        currentSize = stat.size;
          
        logs.push(`[결과] 변환 후 크기: ${(currentSize / (1024 * 1024)).toFixed(2)} MB`);

        if (currentSize <= targetBytes) {
          const ratio = targetBytes / currentSize;
          // We are under the target. If we are within 10% of the target, or have tried 6 times, or reached max quality (300 DPI), we stop.
          if (ratio < 1.10 || attempt >= 6 || currentDpi >= 300) {
            logs.push(`[성공] 목표 용량(${targetMB} MB) 이하 최고 화질 달성 성공!`);
            break;
          } else {
            logs.push(`[재시도] 여유 용량 있음. 최고 화질을 위해 해상도를 상향 조정하여 다시 렌더링합니다.`);
            // Scale UP safely
            const dpiFactor = 1 + (Math.sqrt(ratio) - 1) * 0.7; // 70% of the calculated increase
            const jpegFactor = 1 + (ratio - 1) * 0.7;
            
            const nextDpi = Math.floor(currentDpi * dpiFactor);
            currentDpi = Math.min(300, nextDpi > currentDpi ? nextDpi : currentDpi + 2);
            
            const nextJpeg = Math.floor(currentJpegQ * jpegFactor);
            currentJpegQ = Math.min(95, nextJpeg > currentJpegQ ? nextJpeg : currentJpegQ + 2);
          }
        } else {
          // Over target. Scale DOWN.
          if (attempt < 8) {
            logs.push(`[재시도] 용량 초과. 목표에 근접하도록 해상도를 하향 조정하여 다시 렌더링합니다.`);
            const ratio = targetBytes / currentSize;
            const dpiFactor = Math.sqrt(ratio) * 0.95; // 5% extra reduction
            const jpegFactor = ratio * 0.95;
            
            const nextDpi = Math.floor(currentDpi * dpiFactor);
            currentDpi = Math.max(10, nextDpi < currentDpi ? nextDpi : currentDpi - 2);
            
            const nextJpeg = Math.floor(currentJpegQ * jpegFactor);
            currentJpegQ = Math.max(5, nextJpeg < currentJpegQ ? nextJpeg : currentJpegQ - 2);
          } else {
            logs.push(`[한계] 컬러 유지 한도 내에서 최대한 압축했으나 ${targetMB}MB에 도달하지 못했습니다.`);
            break;
          }
        }
      } catch (e: any) {
        console.error("Ghostscript execution failed:", e);
        throw new Error("Ghostscript 변환 중 오류가 발생했습니다: " + e.message);
      }
    }

    // Determine final download path and check if originalPath was provided
    let savedDirectly = false;
    let finalFileName = file.originalname.replace(/\.[^/.]+$/, "") + "_compressed.pdf";
    const originalPath = req.body.originalPath;

    if (originalPath && fs.existsSync(path.dirname(originalPath))) {
      try {
        const originalDir = path.dirname(originalPath);
        const originalExt = path.extname(originalPath);
        const baseName = path.basename(originalPath, originalExt);
        const newFileName = `압축_${baseName}${originalExt}`;
        const finalSavedPath = path.join(originalDir, newFileName);
        
        fs.copyFileSync(outputPath, finalSavedPath);
        savedDirectly = true;
        finalFileName = newFileName;
        logs.push(`[저장 완료] 원본 위치에 저장되었습니다: ${newFileName}`);
      } catch (e) {
        console.error("Failed to copy to original dir:", e);
      }
    }

    // Clean up original uploaded file
    if (fs.existsSync(file.path)) {
      fs.unlinkSync(file.path);
    }

    logs.push(`[최종] 산출 결과: ${(currentSize / (1024 * 1024)).toFixed(2)} MB`);

    res.json({
      success: true,
      fileName: finalFileName,
      downloadId: path.basename(outputPath),
      originalSize,
      compressedSize: currentSize,
      savedDirectly,
      logs
    });
  } catch (error: any) {
    console.error("PDF compression core failure:", error);
    res.status(500).json({
      success: false,
      error: error.message || "PDF 압축 도중 오류가 발생했습니다.",
    });
  }
});

// REST API endpoint for Image Conversion (invokes Python worker via file upload - legacy fallback)
app.post("/api/image/convert", upload.single("file"), async (req, res) => {
  try {
    const file = req.file;
    if (!file) {
      return res.status(400).json({ error: "이미지 파일을 업로드해주세요." });
    }

    const { dpi = "300", uppercase = "false", originalPath } = req.body;
    
    // We must ensure the file extension matches the original file so the python worker knows what to do (e.g., .wmf)
    const ext = path.extname(file.originalname).toLowerCase();
    const renamedInput = file.path + ext;
    fs.renameSync(file.path, renamedInput);

    const workerPath = getImageWorkerPath();

    const outputDir = path.join(tempDir, "image_out_" + Date.now());
    
    const args = [
      "--input", renamedInput,
      "--output", outputDir,
      "--dpi", dpi.toString()
    ];
    
    if (uppercase === "true") {
      args.push("--uppercase");
    }

    execFile(workerPath, args, { maxBuffer: 1024 * 1024 * 50 }, (error, stdout, stderr) => {
      // Cleanup renamed input
      if (fs.existsSync(renamedInput)) {
        try { fs.unlinkSync(renamedInput); } catch(e){}
      }
      
      if (error) {
        console.error("Image Worker Error:", error, stderr);
        return res.status(500).json({ error: "이미지 변환 중 오류가 발생했습니다." });
      }
      
      try {
        const result = JSON.parse(stdout.trim());
        if (!result.success) {
          return res.status(500).json({ error: result.error });
        }
        
        const outFilePath = result.outputFile;
        const outFileName = path.basename(outFilePath);
        
        // Handle original path copy
        let savedDirectly = false;
        let finalFileName = file.originalname.replace(/\.[^/.]+$/, "") + (uppercase === "true" ? ".JPEG" : ".jpg");
        
        if (originalPath && fs.existsSync(path.dirname(originalPath))) {
          try {
            const originalDir = path.dirname(originalPath);
            const baseName = path.basename(originalPath, ext);
            finalFileName = `변환_${baseName}${uppercase === "true" ? ".JPEG" : ".jpg"}`;
            const finalSavedPath = path.join(originalDir, finalFileName);
            
            fs.copyFileSync(outFilePath, finalSavedPath);
            savedDirectly = true;
          } catch (e) {
            console.error("Failed to copy to original dir:", e);
          }
        }
        
        // Return downloadId so frontend can download if direct save failed
        const downloadId = "img_" + Date.now() + "_" + outFileName;
        const tempCopyPath = path.join(tempDir, downloadId);
        fs.copyFileSync(outFilePath, tempCopyPath);
        
        // Clean up output dir
        fs.rmSync(outputDir, { recursive: true, force: true });
        
        res.json({
          success: true,
          fileName: finalFileName,
          downloadId: downloadId,
          savedDirectly: savedDirectly
        });
        
      } catch (e) {
        console.error("Failed to parse worker output:", e);
        res.status(500).json({ error: "워커 응답을 파싱할 수 없습니다." });
      }
    });

  } catch (e: any) {
    console.error("Server error:", e);
    res.status(500).json({ error: "서버 내부 오류: " + e.message });
  }
});

// REST API for scanning dropped files/folders
app.post("/api/image/scan", async (req, res) => {
  try {
    const { paths, options } = req.body;
    if (!paths || !Array.isArray(paths)) {
      return res.status(400).json({ error: "경로 목록이 없습니다." });
    }

    const supportedExts = new Set([".png", ".tmp", ".gif", ".tif", ".tiff", ".svg", ".wmf", ".webp"]);
    if (options.jpg) { supportedExts.add(".jpg"); supportedExts.add(".jpeg"); }
    if (options.bmp) supportedExts.add(".bmp");
    if (options.emf) supportedExts.add(".emf");

    const resultFiles: { path: string; name: string; ext: string; size: number }[] = [];

    const scanRecursively = (targetPath: string) => {
      try {
        const stat = fs.statSync(targetPath);
        if (stat.isDirectory()) {
          const items = fs.readdirSync(targetPath);
          for (const item of items) {
            scanRecursively(path.join(targetPath, item));
          }
        } else {
          const ext = path.extname(targetPath).toLowerCase();
          if (supportedExts.has(ext)) {
            resultFiles.push({
              path: targetPath,
              name: path.basename(targetPath),
              ext: ext,
              size: stat.size
            });
          }
        }
      } catch (e) {
        console.error("Failed to stat path:", targetPath, e);
      }
    };

    for (const p of paths) {
      scanRecursively(p);
    }

    res.json({ success: true, files: resultFiles });
  } catch (e: any) {
    console.error("Scan error:", e);
    res.status(500).json({ error: "스캔 오류: " + e.message });
  }
});

// REST API for batch converting local files via streaming response
app.post("/api/image/convert-batch", async (req, res) => {
  try {
    const { paths, outputDir, dpi = "300", uppercase = "false" } = req.body;
    
    if (!paths || !paths.length || !outputDir) {
      return res.status(400).json({ error: "필수 파라미터가 없습니다." });
    }

    if (!fs.existsSync(outputDir)) {
      fs.mkdirSync(outputDir, { recursive: true });
    }

    const workerPath = getImageWorkerPath();

    const tempJson = path.join(os.tmpdir(), `paths_${Date.now()}.json`);
    fs.writeFileSync(tempJson, JSON.stringify(paths));

    const args = [
      "--input-json", tempJson,
      "--output", outputDir,
      "--dpi", dpi.toString()
    ];
    
    if (uppercase === "true") {
      args.push("--uppercase");
    }

    res.setHeader("Content-Type", "application/x-ndjson; charset=utf-8");
    res.setHeader("Transfer-Encoding", "chunked");

    const child = spawn(workerPath, args);

    child.stdout.on("data", (data) => {
      res.write(data);
    });
    
    child.stderr.on("data", (data) => {
      console.error(`Worker stderr: ${data}`);
    });

    child.on("close", (code) => {
      try { fs.unlinkSync(tempJson); } catch (e) {}
      res.end();
    });

  } catch (e: any) {
    res.write(JSON.stringify({ event: "error", error: "서버 내부 오류: " + e.message }) + "\n");
    res.end();
  }
});

app.post("/api/close", (req, res) => {
  res.json({ success: true });
  setTimeout(() => process.exit(0), 100);
});

app.post("/api/minimize", (req, res) => {
  res.json({ success: true });
  try {
    // Import electron dynamically so it doesn't crash pure Node.js environments
    const { BrowserWindow } = require("electron");
    const win = BrowserWindow.getFocusedWindow();
    if (win) win.minimize();
  } catch (error) {
    console.error("Not running inside Electron, cannot minimize window");
  }
});

// Download endpoint for the compressed file
app.get("/api/download/:filename", (req, res) => {
  const filename = req.params.filename;
  const originalFileName = (req.query.filename as string) || filename;
  const filePath = path.join(tempDir, filename);
  
  // Basic security check to prevent directory traversal
  if (!filePath.startsWith(tempDir)) {
    return res.status(403).send("Forbidden");
  }

  if (fs.existsSync(filePath)) {
    res.download(filePath, originalFileName, (err) => {
      // Clean up the file after successful or failed download
      try {
        if (fs.existsSync(filePath)) {
          fs.unlinkSync(filePath);
        }
      } catch (e) {
        console.error("Cleanup error:", e);
      }
    });
  } else {
    res.status(404).send("File not found or already downloaded.");
  }
});

// Mount modular utility API routes (see api-modules/registry.ts). Registered
// before the static catch-all in startServer() so /api/* always wins.
registerModules(app);

// Configure development and production bundling environments
export async function startServer(appPath?: string): Promise<number> {
  if (process.env.NODE_ENV !== "production") {
    const { createServer: createViteServer } = await import("vite");
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    // Determine path to static files based on execution environment
    let distPath = appPath ? path.join(appPath, "dist") : path.join(process.cwd(), "dist");
    // If running from packaged app (e.g. inside app.asar/dist), currentDirName will be dist
    if (!appPath && currentDirName && currentDirName.endsWith("dist")) {
      distPath = currentDirName;
    }
    app.use(express.static(distPath));
    app.get("*", (req, res) => {
      res.sendFile(path.join(distPath, "index.html"));
    });
  }

  const portToUse = process.env.NODE_ENV === "production" ? 0 : PORT;

  return new Promise((resolve) => {
    const server = app.listen(portToUse, "127.0.0.1", () => {
      const addr = server.address();
      const actualPort = typeof addr === 'string' ? portToUse : addr?.port || portToUse;
      console.log(`[서버 구동] Port: ${actualPort}`);
      resolve(actualPort);
    });
  });
}

// Start automatically if not imported by Electron main process
if (!process.versions?.electron) {
  startServer();
}
