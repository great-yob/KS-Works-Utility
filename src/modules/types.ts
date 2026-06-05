import type { ComponentType } from "react";
import type { LucideIcon } from "lucide-react";

/** Accent colors supported by the sidebar. Add new ones to ACCENT_ACTIVE in App.tsx. */
export type AccentColor = "blue" | "indigo" | "emerald" | "amber" | "rose";

/**
 * A single utility in the portal. One module === one sidebar entry + one route.
 * This is the contract every utility must satisfy to plug into the portal.
 */
export interface UtilityModule {
  /** Unique, kebab-case id. Used as the React key. */
  id: string;
  /** Router path. Exactly one module must use "/" (the landing utility). */
  path: string;
  /** Sidebar label (Korean UI). */
  label: string;
  /** lucide-react icon shown in the sidebar. */
  icon: LucideIcon;
  /** Accent color for the active nav state. */
  accent: AccentColor;
  /** The page component rendered at `path`. */
  Component: ComponentType;
}
