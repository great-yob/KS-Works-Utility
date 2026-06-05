import type { Express } from "express";
import type { ApiModule } from "./types";

/**
 * Example / template API module. Exposes lightweight app metadata used for
 * health checks and diagnostics. Copy this file as the starting point for a new
 * utility's backend, then register it in registry.ts.
 */
export const appInfoModule: ApiModule = {
  id: "app-info",
  register(app: Express) {
    app.get("/api/app/health", (_req, res) => {
      res.json({
        ok: true,
        platform: process.platform,
        ts: Date.now(),
      });
    });
  },
};
