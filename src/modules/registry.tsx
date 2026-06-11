import { FileText, Image as ImageIcon, FileType } from "lucide-react";
import PdfCompressor from "../pages/PdfCompressor";
import ImageConverter from "../pages/ImageConverter";
import HwpImageConverter from "../pages/HwpImageConverter";
import type { UtilityModule } from "./types";

/**
 * The portal's utility registry — the single source of truth for navigation and
 * routing. App.tsx derives the sidebar and <Routes> entirely from this array.
 *
 * ▶ To add a new utility:
 *   1. Create a page component under src/pages/<Name>.tsx
 *   2. (Optional) add a matching API module under server/modules/ (see registry there)
 *   3. Append ONE entry below.
 * See docs/유틸리티_추가_가이드.md for the full checklist.
 */
export const modules: UtilityModule[] = [
  {
    id: "hwp-image-converter",
    path: "/",
    label: "삽입그림 정리기",
    icon: FileType,
    accent: "teal",
    Component: HwpImageConverter,
  },
  {
    id: "image-converter",
    path: "/image",
    label: "이미지 변환기",
    icon: ImageIcon,
    accent: "indigo",
    Component: ImageConverter,
  },
  {
    id: "pdf-compressor",
    path: "/pdf",
    label: "PDF 압축기",
    icon: FileText,
    accent: "blue",
    Component: PdfCompressor,
  },
];
