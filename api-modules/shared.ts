import path from "path";
import os from "os";
import fs from "fs";
import multer from "multer";

/**
 * Shared backend utilities used by every API module. Centralizes the
 * dev-vs-packaged path resolution that used to be copy-pasted across endpoints.
 */

/** True when running inside a packaged Electron app (asar). */
export function isPackaged(): boolean {
  return (
    __dirname.includes("app.asar") ||
    __dirname.includes("resources\\app") ||
    __dirname.includes("resources/app")
  );
}

/**
 * Resolve a path inside the bundled `resources/` directory. Works in dev
 * (process.cwd()/resources/...) and in a packaged app
 * (process.resourcesPath/resources/...). electron-builder ships `resources/`
 * as extraResources, i.e. outside the asar — see package.json `build`.
 */
export function getResourcePath(...segments: string[]): string {
  if (isPackaged()) {
    const resPath = (process as any).resourcesPath || path.join(__dirname, "..", "..");
    return path.join(resPath, "resources", ...segments);
  }
  return path.join(process.cwd(), "resources", ...segments);
}

/** Absolute path to the bundled Ghostscript console binary. */
export function getGhostscriptPath(): string {
  return getResourcePath("ghostscript", "bin", "gswin64c.exe");
}

/** Absolute path to the bundled Python image worker exe. */
export function getImageWorkerPath(): string {
  return getResourcePath("image_worker", "image_worker.exe");
}

/** Shared scratch directory for all utilities. Created on import. */
export const tempDir = path.join(os.tmpdir(), "pdf-compressor-temp");
if (!fs.existsSync(tempDir)) {
  fs.mkdirSync(tempDir, { recursive: true });
}

/** Shared multer instance: disk storage into tempDir, 500MB cap. */
export const upload = multer({
  storage: multer.diskStorage({
    destination: (_req, _file, cb) => cb(null, tempDir),
    filename: (_req, _file, cb) =>
      cb(null, Date.now() + "-" + Math.round(Math.random() * 1e9) + ".pdf"),
  }),
  limits: {
    fileSize: 500 * 1024 * 1024, // 500MB
  },
});
