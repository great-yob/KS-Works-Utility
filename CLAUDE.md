# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

KS-Works-Utility is a **Windows-only desktop app** (Electron) bundling three office utilities with a Korean-language UI:
- **PDF 압축기** (PDF Compressor) — iteratively re-encodes a PDF with Ghostscript to hit a target file size.
- **이미지 변환기** (Image Converter) — batch-converts images (incl. WMF/EMF via Windows GDI/GDI+) to JPG.
- **삽입그림 정리기** (HWP Image Cleaner) — converts every image *inside* a HWP/HWPX document to JPG in place (fixes 한글 layout breakage from WMF/EMF/PNG-heavy documents) and optionally downsizes oversized images ("사이즈 조정").

UI strings, error messages, and log lines are intentionally in Korean — preserve that when editing.

## Commands

```powershell
npm install              # install Node deps

npm run dev              # dev: tsx runs server.ts, which mounts Vite in middleware mode on http://localhost:3000
npm run lint             # type-check only (tsc --noEmit); there is no separate test suite
npm run build            # vite build (frontend → dist/) + esbuild bundles server/electron-main/preload → dist/*.cjs
npm start                # run the built server standalone (node dist/server.cjs), no Electron
npm run dev:electron     # build, then launch Electron pointing at dist/electron-main.cjs
npm run build:exe        # build, then electron-builder → NSIS installer in release_build/
npm run release          # build, then electron-builder --publish always (uploads to GitHub Releases; needs GH_TOKEN)
```

Notes:
- The default shell is PowerShell. `npm run clean` uses `rm -rf` (bash) and will fail in PowerShell — delete `dist/` manually or run it via the Bash tool.
- There are **no automated tests**; `lint` (tsc) is the only static check. Python workers are tested by hand against real documents in `D:\작업방\`.
- In a sandboxed shell, launch Electron with `env -u ELECTRON_RUN_AS_NODE node_modules/.bin/electron .` — if `ELECTRON_RUN_AS_NODE=1` leaks in, Electron runs as plain Node and crashes on `app.commandLine`.

### Regenerating bundled native binaries

These are committed under `resources/` but are large/generated. Rebuild whenever the corresponding Python source changes (the app uses the **exe even in dev** when it exists):
- **Ghostscript**: `node download_gs.mjs` downloads the GS 10.03.1 Windows installer and 7-Zip-extracts it into `resources/ghostscript/` (provides `bin/gswin64c.exe`). Not wired into any npm script.
- **Image worker**: `cd python_worker && build_worker.bat` → PyInstaller onefile → `resources/image_worker/image_worker.exe`. Requires Python (32-bit 3.11 in use) + Pillow.
- **HWP worker**: `cd python_worker && build_hwp_worker.bat` → `resources/hwp_worker/hwp_worker.exe`. Requires pywin32 + olefile + Pillow. **Hidden imports are load-bearing** — see "Packaging the worker" below. The `.bat` files can garble in non-cp949 shells; the equivalent direct command is `python -m PyInstaller --onefile --name hwp_worker --distpath dist --noconfirm --hidden-import pythoncom --hidden-import pywintypes --hidden-import win32timezone --hidden-import win32com --hidden-import win32com.client --hidden-import worker hwp_worker.py`.

## Architecture

The app has three cooperating process types, all on the local machine — nothing is uploaded to a network.

### 1. Express server (`server.ts`) — the core

Hosts all `/api/*` endpoints and serves the frontend. It runs in two modes via `startServer()`:
- **dev** (`NODE_ENV !== production`): creates a Vite dev server in middleware mode and listens on fixed port **3000**.
- **prod**: serves the static `dist/` build and listens on port **0** (OS-assigned random port, bound to `127.0.0.1`); the chosen port is returned to the Electron main process.

`startServer()` auto-runs at import time *unless* launched inside Electron (`process.versions.electron`), in which case `electron-main.ts` calls it explicitly and passes `app.getAppPath()` so static files resolve inside the asar.

Key endpoints (legacy ones live inline in `server.ts`; new utilities mount via `api-modules/`):
- `POST /api/compress` — multer disk upload; runs the Ghostscript size-targeting loop (see below).
- `POST /api/image/scan` — recursively walks dropped file/folder **paths** and returns matching image files; the `options` flags toggle whether jpg/bmp/emf are *included* (png/tif/svg/wmf/webp etc. are always in).
- `POST /api/image/convert-batch` — spawns the Python image worker and **streams NDJSON** progress lines back (`Content-Type: application/x-ndjson`).
- `POST /api/hwp-image/scan` / `POST /api/hwp-image/convert` (`api-modules/hwpImage.ts`) — spawn the HWP worker; convert streams NDJSON and finishes with a `complete` event carrying `outputPath`/`outputDir`. Output is written beside the original as `변환_<name>.hwp(x)`.
- `POST /api/image/convert` — legacy single-file upload fallback path.
- `GET /api/download/:filename` — serves a temp file once, then deletes it (with a directory-traversal guard against `tempDir`).
- `POST /api/close` / `POST /api/minimize` — window controls; `minimize` dynamically `require("electron")` so the server still runs as plain Node.
- `POST /api/open-folder` — opens a folder in Windows Explorer; used by the green "폴더 열기" result buttons.

All temp work happens in `os.tmpdir()/pdf-compressor-temp`. Upload/body limits are 500 MB.

**NDJSON convention**: workers print one JSON object per line on stdout; the server pipes raw chunks through; the frontend reads with a **line buffer** (chunk boundaries can split a JSON line — both `ImageConverter.tsx` and `HwpImageConverter.tsx` carry the partial last line over to the next chunk; copy that pattern for new streaming utilities).

### 2. Electron shell (`electron-main.ts` + `preload.ts`)

- Frameless, transparent, **non-resizable** `BrowserWindow` (944×704, centered). The app root has rounded corners (`rounded-2xl`) and a global `brightness-125`. Window dragging is done with CSS `-webkit-app-region: drag` (`.draggable` / `.no-drag` in `src/index.css`), not native chrome.
- Forces `NODE_ENV=production` and raises the V8 heap to 8 GB for large PDFs.
- `preload.ts` exposes **`window.electronAPI.getPathForFile(file)`** (Electron `webUtils`). This is load-bearing: it gives the renderer the **absolute path** of dropped files, which is how the app reads originals in place and writes results next to them.

### 3. Python workers (`python_worker/`)

Standalone PyInstaller exes invoked by the server via `execFile`/`spawn`; both print NDJSON to stdout. **Never write relative-path debug/log files from the workers** — the CWD of a spawned worker in an installed app can be read-only, and an unguarded `open("foo.log","w")` kills the whole worker (this happened; the debug logs were removed).

- **`worker.py`** (이미지 변환기): Pillow for normal formats, **Windows GDI/GDI+ via `ctypes`** to rasterize `.wmf`/`.emf` — this is why the app is Windows-only. Accepts `--input` or `--input-json` (batch), `--output`, `--dpi`, `--uppercase`.
- **`hwp_worker.py`** (삽입그림 정리기): `--input`, `--output`, `--mode selective|all`, `--scan`, `--size-adjust`. Two processors chosen by magic bytes: `HwpProcessor` (HWP 5.0 OLE compound file, via pythoncom `StgOpenStorage` on a temp copy) and `HwpxProcessor` (HWPX ZIP, 한글 2022). It imports `worker.py` for all metafile rendering. The frontend default option is **전체 정리 (JPG+사이즈)** (`mode=all`, `sizeAdjust=true`).

### Native binary path resolution (important pattern)

`api-modules/shared.ts` (`getResourcePath`/`getGhostscriptPath`/`getImageWorkerPath`/`getHwpWorkerPath`) detects packaging by checking whether `__dirname` contains `app.asar` / `resources\app`. When packaged it resolves against `process.resourcesPath` + `resources/...`; otherwise against `process.cwd()/resources/...`. electron-builder ships `resources/` as `extraResources` (see `package.json` `build`), so it lands **outside** the asar at runtime. If you add a native dependency, put it under `resources/<name>/` and it ships automatically.

### Frontend (`src/`)

Vite + React 19 + React Router + Tailwind v4 + `lucide-react`. Pages live in `src/pages/`; all talk to the server purely over `fetch` to relative `/api/*` URLs. Each page is a 4-panel layout (sidebar + 옵션 / 파일드롭 / 진행및결과), where the file-drop and progress-result panels are stacked vertically and the progress-result panel has a terminal-style log window; every panel has its own reset button. (`motion` is still a dependency but no longer imported by the pages.)

**Portal module convention (how to add a utility)**: the sidebar + routing in `src/App.tsx` are data-driven from `src/modules/registry.tsx` (one `UtilityModule` entry per utility — `id`/`path`/`label`/`icon`/`accent`/`Component`). Routing uses `useRoutes`. The backend mirror is `api-modules/registry.ts`: each `ApiModule` exposes a `register(app)` that mounts its `/api/*` routes, mounted by `registerModules(app)` in `server.ts`. The 삽입그림 정리기 (`src/pages/HwpImageConverter.tsx` + `api-modules/hwpImage.ts`) is the reference implementation of this convention. Full checklist: `docs/유틸리티_추가_가이드.md`.

**Auto-update**: packaged builds use NSIS (per-user, `oneClick`) and `electron-updater` against GitHub Releases (`great-yob/KS-Works-Utility`, public repo). `electron-main.ts` `setupAutoUpdater()` checks on launch + every 6h, auto-downloads, logs events to `userData/ksworks-update.log`, and pushes status over the `update:status` IPC channel; `preload.ts` exposes `onUpdateStatus`/`checkForUpdate`/`installUpdate`, surfaced by `src/components/UpdateNotice.tsx`. Publishing config: `build.publish[0].releaseType` is `release` (releases go live immediately), `build.nsis.artifactName` is fixed to `KS-Works-Utility-Setup.exe` so the latest installer is always at the stable `/releases/latest/download/KS-Works-Utility-Setup.exe` URL, and `build.directories.output` is `release_build/`. App icon = `assets/icon.ico`. **Bump `version` in `package.json` per release** (sidebar version comes from the Vite `__APP_VERSION__` define). Full steps: `docs/자동업데이트_배포_가이드.md`.

**Result output / "폴더 열기"**: the renderer sends dropped files' absolute paths (from `electronAPI.getPathForFile`). PDF compress writes `압축_<name>.pdf` beside the original; image batch-convert writes JPGs into a `converted_jpg` subfolder next to the sources; HWP convert writes `변환_<name>.hwp(x)` beside the original. Each result panel has a green "폴더 열기" button calling `/api/open-folder`.

### Compression algorithm (`/api/compress`)

Not a single pass: it picks a starting DPI/JPEG-quality config from the target/original size ratio, runs Ghostscript (`-sDEVICE=pdfwrite`, forced DCT/JPEG re-encode), measures the output, then loops up to ~10 times — scaling DPI and JPEG quality **down** when over target and back **up** (within caps: ≤300 DPI, ≤95 quality) when comfortably under — to land just below the target at the best quality. Human-readable Korean step logs are accumulated and returned in `logs[]`.

## HWP worker internals (hard-won knowledge — do not rediscover)

### Metafile rendering fidelity — render like 한글 does, via GDI+ (`worker._render_hemf_gdiplus`)

Office-generated WMFs are usually "EMF-embedded WMFs" (`WMFC` ESCAPE comments carrying the original EMF; `SetWinMetaFileBits` recovers it). Three GDI-playback defects surfaced in sequence, all fixed by rendering the recovered EMF with **GDI+** (`GdipCreateMetafileFromEmf` + `GdipDrawImageRectI`), which is how 한글 renders metafiles:
- **Gray backgrounds**: the embedded EMF often stores rasters as **16bpp RGB555**; GDI expands 5-bit channels by `<<3` only, so white (31) → (248,248,248). GDI+ expands by bit-replication → true 255.
- **Broken/lumpy text**: don't "fix" the gray by playing the raw WMF body records instead — its text/curves are 16-bit-quantized polygons that render lumpy when upscaled; the EMF has smooth vector glyph paths (and GDI+ renders them antialiased).
- **Dotted drop-shadows**: PowerPoint chart EMFs are **EMF+ duals** (same image twice: GDI+-only EMF+ COMMENT records + plain-GDI fallback records). Soft shadows/transparency exist only in the EMF+ half; the GDI fallback approximates them with dithering. GDI `PlayEnhMetaFile` uses the fallback; GDI+ plays the EMF+ records.

If GDI+ fails, the GDI path remains as fallback with a **lossless 16bpp LUT** (`_emf_16bpp_correction`: scan EMF bits for 16bpp DIBs in EMR 76/77/80/81; multiply by 255/248, G by 255/252 for 565 — exact since 248 = 31×8). Direct `.emf` files also go GDI+-first (`render_emf_gdi`). `render_wmf_window_px` (size-adjust re-render) is also GDI+-first with an aspect-ratio guard — only the hidden-margin/placeable cases fall through to raw window-extent playback. The gdiplus ctypes bindings declare full argtypes so a future 64-bit Python rebuild won't truncate handles.

### WMF crop fix — render the declared window extent (`render_wmf_gdi_fullframe`)

한글 applies "그림 자르기" relative to the WMF's **declared full canvas** (`META_SETWINDOWEXT`, margins included), but a tight-bounds render drops those margins, so the crop slices into the real chart after conversion. `render_wmf_gdi_fullframe()` renders the full window when (and only when) there is genuinely hidden whitespace: placeable WMFs delegate to the normal render; window-AR ≈ tight-AR returns the tight render; and a `_nonwhite_bbox` sanity check falls back if content gets squeezed into a corner (mapping failure). No HWP binary mutation, no per-file hardcoding, no Tag 78/85 crop parsing needed.

### Blank-box fix — DocInfo extension length (`HwpProcessor._patch_docinfo`)

Converted streams are always renamed `BINxxxx.jpg`, and the DocInfo `HWPTAG_BIN_DATA` (tag 18) record's extension must match. 한글 2022 saves embedded JPEGs as `.jpeg` (4 chars; 2010 uses `.jpg`), so a length-changing patch is required: the record is rebuilt (`prop(2)+binId(2)+extLen(2)+ext` — ext is the last field) and the 4-byte tag-header size is updated. The old same-length-only patch left `.jpeg`/`.tiff`/`.webp` entries stale → 한글 looked for `BINxxxx.jpeg`, found `.jpg` → **blank box** (the widespread "2022-authored HWP blanks" bug). HWPX never had this problem (it string-replaces the href in section XML and fixes `media-type` in content.hpf).

### Size-adjust (`--size-adjust`) — file-size reduction without visible quality loss

Post-conversion pass (the "JPG+사이즈" options; **default**). It downsizes only images whose **effective display resolution** exceeds 300 dpi, to exactly 300 dpi (print quality). Empirically-validated facts (real 한글 개체속성, both HWPX and OLE):
- 한글 computes a picture's natural size from **JPG pixels ÷ DPI**, not any stored size field. So size-adjust only changes the embedded JPG's pixels/DPI — **never document geometry**. ⚠ **Never rewrite BodyText Tag85 off44/off12/curSz**: off44 is the image's *native coordinate frame* (raster = px×75, WMF = window-extent×75) onto which 한글 maps the JPG; rewriting it to match new pixels blanks uncropped WMFs and mis-crops rasters (tried once, fully reverted).
- The metric is **effective display dpi = visible_px × 7200 / curSz_HWPUNIT** (per axis, take min), where `visible_px = clip_fraction × jpg_px`. The 한글 dialog "확대/축소 비율" is *not* the metric.
- **WMF re-render instead of downsample**: LANCZOS-downsampling a big render fades thin vector lines to gray (black 0 → ~197); `render_wmf_window_px` re-renders the vector small and crisp (display size × 2.0 oversample, DPI set to match).
- **HWPX** (`size_adjust_hwpx`, separate pass on the output zip): `curSz` from `<hp:curSz>`, clip from `<hp:imgClip>`/`<hp:imgDim>` (a resolution-independent fraction → immune to pixel changes).
- **OLE** (`size_adjust_jpg_for_record`, inline during the COM session): `curSz` from BodyText Tag 76 offset 28/32 via read-only `read_ole_picture_info` (olefile) before the COM write. **OLE resizes only *uncropped* pictures** (off44 == full native rect, 3% tolerance): a cropped picture's off44 is in px×75, so shrinking the JPG would point it outside the image. Cropped pictures usually read below 300 dpi anyway. EMF is skipped.

### Packaging the worker (`build_hwp_worker.bat`)

PyInstaller must be given hidden imports the static analyzer misses because they're imported lazily inside functions — `pythoncom`/`pywintypes` and the sibling `worker` module, and critically **`win32timezone`**: enumerating the OLE `BinData` storage (`enum.Next()`) converts STATSTG timestamps and lazily imports `win32timezone`; if it's missing, the enumeration throws, gets swallowed, and **scan/convert silently return 0 images** in the frozen exe while working fine under dev Python.
