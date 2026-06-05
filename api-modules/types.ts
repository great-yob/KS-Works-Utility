import type { Express } from "express";

/**
 * A self-contained group of /api routes for one utility — the backend
 * counterpart to a UtilityModule in src/modules. Keeping each utility's
 * endpoints behind a register() call lets the portal grow without server.ts
 * becoming a monolith.
 */
export interface ApiModule {
  /** Unique id (kebab-case), used for logging. */
  id: string;
  /** Register this module's routes on the shared Express app. */
  register(app: Express): void;
}
