import { app, BrowserWindow, ipcMain } from 'electron';
import path from 'path';
import os from 'os';
import fs from 'fs';
import { autoUpdater } from 'electron-updater';

// Append-only diagnostic log for auto-update events (handy for support/QA).
// Lives in userData (e.g. %APPDATA%\ks-works-utility\ksworks-update.log) once the
// app is ready, falling back to the OS temp dir before then.
function updateLogPath(): string {
  try {
    return path.join(app.getPath('userData'), 'ksworks-update.log');
  } catch {
    return path.join(os.tmpdir(), 'ksworks-update.log');
  }
}
function logUpdate(line: string) {
  try {
    fs.appendFileSync(updateLogPath(), `[${new Date().toISOString()}] ${line}\n`);
  } catch {
    /* logging must never crash the app */
  }
}

// Force production environment in Electron so Vite dev server isn't started
process.env.NODE_ENV = 'production';

// Increase max heap size to 8GB for handling very large PDF files
app.commandLine.appendSwitch('js-flags', '--max-old-space-size=8192');

// We import the bundled server.cjs in production
import { startServer } from './server';

// Re-check for updates every 6 hours while the app stays open (utility apps are long-lived).
const UPDATE_POLL_MS = 6 * 60 * 60 * 1000;

/**
 * Wires electron-updater to the renderer. All user-facing update state is pushed
 * over the `update:status` channel so the UI can show Korean status text, and the
 * renderer can trigger a check / install via `update:check` / `update:install`.
 */
function setupAutoUpdater(win: BrowserWindow) {
  // Download in the background; if the user never clicks "재시작", install on quit.
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  // Capture electron-updater's internal logs to the diagnostic file too.
  autoUpdater.logger = {
    info: (m?: any) => logUpdate('[info] ' + m),
    warn: (m?: any) => logUpdate('[warn] ' + m),
    error: (m?: any) => logUpdate('[error] ' + m),
  };
  logUpdate(`updater started, current version=${app.getVersion()} packaged=${app.isPackaged}`);

  const send = (payload: Record<string, unknown>) => {
    if (!win.isDestroyed()) win.webContents.send('update:status', payload);
  };

  autoUpdater.on('checking-for-update', () => { logUpdate('checking-for-update'); send({ state: 'checking' }); });
  autoUpdater.on('update-available', (info) => { logUpdate('update-available ' + info.version); send({ state: 'available', version: info.version }); });
  autoUpdater.on('update-not-available', () => { logUpdate('update-not-available'); send({ state: 'none' }); });
  autoUpdater.on('download-progress', (p) => send({ state: 'downloading', percent: Math.round(p.percent) }));
  autoUpdater.on('update-downloaded', (info) => { logUpdate('update-downloaded ' + info.version); send({ state: 'downloaded', version: info.version }); });
  autoUpdater.on('error', (err) => { logUpdate('error ' + String(err?.message || err)); send({ state: 'error', message: String(err?.message || err) }); });

  // Renderer-initiated actions.
  ipcMain.handle('update:check', () => autoUpdater.checkForUpdates().catch((e) => {
    console.error('Update check failed:', e);
  }));
  ipcMain.handle('update:install', () => autoUpdater.quitAndInstall());

  // Auto-update only makes sense for a packaged build pulling from GitHub Releases.
  if (!app.isPackaged) return;

  const check = () => autoUpdater.checkForUpdates().catch((e) => console.error('Update check failed:', e));
  check();
  setInterval(check, UPDATE_POLL_MS);
}

async function createWindow() {
  try {
    // Start the express server
    const port = await startServer(app.getAppPath());

    const win = new BrowserWindow({
      width: 944,
      height: 880,
      frame: false,
      transparent: true,
      resizable: false,
      maximizable: false,
      fullscreenable: false,
      center: true,
      autoHideMenuBar: true,
      webPreferences: {
        nodeIntegration: false,
        contextIsolation: true,
        preload: path.join(__dirname, 'preload.cjs')
      }
    });

    win.loadURL(`http://localhost:${port}`);

    // Kick off the updater once the page is ready to receive status events.
    win.webContents.once('did-finish-load', () => setupAutoUpdater(win));
  } catch (error) {
    console.error("Failed to start server or create window:", error);
  }
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
