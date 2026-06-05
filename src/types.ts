export interface CompressionResult {
  success: boolean;
  fileName: string;
  originalSize: number;
  compressedSize: number;
  iterations?: number;
  imagesOptimized?: number;
  logs: string[];
  downloadUrl?: string; // Legacy
  downloadId?: string; // New temp file ID
}

export interface ActiveFileState {
  file: File;
  name: string;
  size: number;
  status: "idle" | "selected" | "compressing" | "success" | "error";
  errorMessage?: string;
  result?: CompressionResult;
}

export interface TargetOption {
  id: string;
  label: string;
  value: number; // Size in MB
  badge?: string;
}
