// Ambient (global) types for the preload bridge and build-time defines.
// No imports/exports here on purpose: this file is a global script so every
// declaration below is visible everywhere without importing.

type UpdateStatus =
  | { state: "checking" }
  | { state: "none" }
  | { state: "available"; version: string }
  | { state: "downloading"; percent: number }
  | { state: "downloaded"; version: string }
  | { state: "error"; message: string };

interface ElectronAPI {
  /** Absolute path of a dropped File (Electron webUtils). Empty string in a browser. */
  getPathForFile: (file: File) => string;
  /** Subscribe to update status; returns an unsubscribe function. */
  onUpdateStatus: (callback: (status: UpdateStatus) => void) => () => void;
  /** Trigger an update check manually. */
  checkForUpdate: () => Promise<void>;
  /** Quit and install a downloaded update. */
  installUpdate: () => Promise<void>;
}

interface Window {
  electronAPI?: ElectronAPI;
}

/** App version injected by Vite at build time (from package.json). */
declare const __APP_VERSION__: string;
