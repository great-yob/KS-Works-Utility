# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

KS-Works-Utility is a **Windows-only desktop app** (Electron) bundling two office utilities with a Korean-language UI:
- **PDF 압축기** (PDF Compressor) — iteratively re-encodes a PDF with Ghostscript to hit a target file size.
- **이미지 변환기** (Image Converter) — batch-converts images (incl. WMF/EMF via Windows GDI) to JPG.

UI strings, error messages, and log lines are intentionally in Korean — preserve that when editing.

## Commands

```powershell
npm install              # install Node deps

npm run dev              # dev: tsx runs server.ts, which mounts Vite in middleware mode on http://localhost:3000
npm run lint             # type-check only (tsc --noEmit); there is no separate test suite
npm run build            # vite build (frontend → dist/) + esbuild bundles server/electron-main/preload → dist/*.cjs
npm start                # run the built server standalone (node dist/server.cjs), no Electron
npm run dev:electron     # build, then launch Electron pointing at dist/electron-main.cjs
npm run build:exe        # build, then electron-builder → NSIS installer in release/
npm run release          # build, then electron-builder --publish always (uploads to GitHub Releases; needs GH_TOKEN)
```

Notes:
- The default shell is PowerShell. `npm run clean` uses `rm -rf` (bash) and will fail in PowerShell — delete `dist/` manually or run it via the Bash tool.
- There are **no automated tests**; `lint` (tsc) is the only static check.

### Regenerating bundled native binaries

These are committed under `resources/` but are large/generated. Rebuild only when needed:
- **Ghostscript**: `node download_gs.mjs` downloads the GS 10.03.1 Windows installer and 7-Zip-extracts it into `resources/ghostscript/` (provides `bin/gswin64c.exe`). Not wired into any npm script.
- **Image worker**: `cd python_worker && build_worker.bat` runs PyInstaller (`worker.py` → onefile exe) and copies it to `resources/image_worker/image_worker.exe`. Requires Python + Pillow.

## Architecture

The app has three cooperating processes, all on the local machine — nothing is uploaded to a network.

### 1. Express server (`server.ts`) — the core

Hosts all `/api/*` endpoints and serves the frontend. It runs in two modes via `startServer()`:
- **dev** (`NODE_ENV !== production`): creates a Vite dev server in middleware mode and listens on fixed port **3000**.
- **prod**: serves the static `dist/` build and listens on port **0** (OS-assigned random port, bound to `127.0.0.1`); the chosen port is returned to the Electron main process.

`startServer()` auto-runs at import time *unless* launched inside Electron (`process.versions.electron`), in which case `electron-main.ts` calls it explicitly and passes `app.getAppPath()` so static files resolve inside the asar.

Key endpoints:
- `POST /api/compress` — multer disk upload; runs the Ghostscript size-targeting loop (see below).
- `POST /api/image/scan` — recursively walks dropped file/folder **paths** and returns matching image files; the `options` flags toggle whether jpg/bmp/emf are *included* (png/tif/svg/wmf/webp etc. are always in).
- `POST /api/image/convert-batch` — spawns the Python worker and **streams NDJSON** progress lines back (`Content-Type: application/x-ndjson`); the frontend reads the response body incrementally.
- `POST /api/image/convert` — legacy single-file upload fallback path.
- `GET /api/download/:filename` — serves a temp file once, then deletes it (with a directory-traversal guard against `tempDir`).
- `POST /api/close` / `POST /api/minimize` — window controls; `minimize` dynamically `require("electron")` so the server still runs as plain Node.

All temp work happens in `os.tmpdir()/pdf-compressor-temp`. Upload/body limits are 500 MB.

### 2. Electron shell (`electron-main.ts` + `preload.ts`)

- Frameless, transparent `BrowserWindow` (944×688). Window dragging is done with CSS `-webkit-app-region: drag` (`.draggable` / `.no-drag` in `src/index.css`), not native chrome.
- Forces `NODE_ENV=production` and raises the V8 heap to 8 GB for large PDFs.
- `preload.ts` exposes **`window.electronAPI.getPathForFile(file)`** (Electron `webUtils`). This is load-bearing: it gives the renderer the **absolute path** of dropped files, which is how the app reads originals in place and writes results next to them.

### 3. Python image worker (`python_worker/worker.py`)

Standalone PyInstaller exe invoked by the server via `execFile`/`spawn`. Uses Pillow for normal formats and **raw Windows GDI through `ctypes` (gdi32/user32)** to rasterize `.wmf`/`.emf` — this is why the app is Windows-only. Accepts `--input` or `--input-json` (batch), `--output`, `--dpi`, `--uppercase`; prints one JSON object per file (`{"event":"progress",...}`) then `{"event":"done"}`.

### Native binary path resolution (important pattern)

Both `getGhostscriptPath()` and the worker-path logic detect packaging by checking whether `__dirname` contains `app.asar` / `resources\app`. When packaged they resolve against `process.resourcesPath` + `resources/...`; otherwise against `process.cwd()/resources/...`. electron-builder ships `resources/` as `extraResources` (see `package.json` `build`), so it lands **outside** the asar at runtime. If you add or move a native dependency, update both the path detection in `server.ts` and the `build` config.

### Frontend (`src/`)

Vite + React 19 + React Router + Tailwind v4 + `motion` + `lucide-react`. Pages live in `src/pages/`; both talk to the server purely over `fetch` to relative `/api/*` URLs.

**Portal module convention (how to add a utility)**: the sidebar + routing in `src/App.tsx` are data-driven from `src/modules/registry.tsx` (one `UtilityModule` entry per utility — `id`/`path`/`label`/`icon`/`accent`/`Component`). Routing uses `useRoutes`. The backend mirror is `api-modules/registry.ts`: each `ApiModule` exposes a `register(app)` that mounts its `/api/*` routes, mounted by `registerModules(app)` in `server.ts`. Shared backend helpers (`tempDir`, multer `upload`, `getResourcePath`/`getGhostscriptPath`/`getImageWorkerPath`) live in `api-modules/shared.ts`. The original `/api/compress` and `/api/image/*` endpoints still live inline in `server.ts`. Full checklist: `docs/유틸리티_추가_가이드.md`.

**Auto-update**: packaged builds use NSIS (per-user, `oneClick`) and `electron-updater` against GitHub Releases. `electron-main.ts` `setupAutoUpdater()` checks on launch + every 6h, auto-downloads, and pushes status over the `update:status` IPC channel; `preload.ts` exposes `onUpdateStatus`/`checkForUpdate`/`installUpdate`, surfaced by `src/components/UpdateNotice.tsx`. Set `build.publish[0].owner` (currently `__GITHUB_OWNER__`) and bump `version` per release. Full steps: `docs/자동업데이트_배포_가이드.md`. The app version shown in the sidebar comes from the Vite `__APP_VERSION__` define (single source = `package.json`).

**Direct-save vs download behavior**: the renderer sends the file's absolute path (from `electronAPI.getPathForFile`) as `originalPath`. When present, the server writes the result *beside the original* (prefixed `압축_` / `변환_`) and reports `savedDirectly: true`; the UI then skips the browser download. Without a path (plain browser context), it falls back to the `/api/download/:id` flow.

### Compression algorithm (`/api/compress`)

Not a single pass: it picks a starting DPI/JPEG-quality config from the target/original size ratio, runs Ghostscript (`-sDEVICE=pdfwrite`, forced DCT/JPEG re-encode), measures the output, then loops up to ~10 times — scaling DPI and JPEG quality **down** when over target and back **up** (within caps: ≤300 DPI, ≤95 quality) when comfortably under — to land just below the target at the best quality. Human-readable Korean step logs are accumulated and returned in `logs[]`.
