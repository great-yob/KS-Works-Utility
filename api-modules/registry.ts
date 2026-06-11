import type { Express } from "express";
import type { ApiModule } from "./types";
import { appInfoModule } from "./appInfo";
import { hwpImageModule } from "./hwpImage";

/**
 * Backend counterpart to src/modules/registry.tsx. Each entry owns a slice of
 * the /api surface.
 *
 * ▶ To add a utility's backend: create a file exporting an ApiModule (copy
 *   appInfo.ts), then append it to this array.
 *
 * NOTE: The original /api/compress and /api/image/* endpoints still live inline
 * in server.ts for now. New utilities should be added here as modules.
 */
export const apiModules: ApiModule[] = [
  appInfoModule,
  hwpImageModule,
];

/** Mount every registered module on the shared Express app. */
export function registerModules(app: Express): void {
  for (const mod of apiModules) {
    mod.register(app);
    console.log(`[모듈 등록] ${mod.id}`);
  }
}
