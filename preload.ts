import { contextBridge, webUtils, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('electronAPI', {
  getPathForFile: (file: any) => {
    try {
      return webUtils.getPathForFile(file);
    } catch (e) {
      return "";
    }
  },

  // --- Auto-update bridge ---------------------------------------------------
  // Subscribe to update status pushes from the main process. Returns an
  // unsubscribe function so React effects can clean up.
  onUpdateStatus: (callback: (status: any) => void) => {
    const listener = (_event: unknown, status: any) => callback(status);
    ipcRenderer.on('update:status', listener);
    return () => ipcRenderer.removeListener('update:status', listener);
  },
  // Manually trigger an update check (also runs automatically on launch).
  checkForUpdate: () => ipcRenderer.invoke('update:check'),
  // Quit and install a downloaded update immediately.
  installUpdate: () => ipcRenderer.invoke('update:install'),
});
