import type { FilePreviewReference } from "../../lib/filePreviewKind";

export interface PreviewPage {
  index: number;
  url: string;
  width?: number | null;
  height?: number | null;
}

export interface ReadOnlyPreviewSource {
  contentUrl: string;
  getPages?: () => Promise<{ pages: PreviewPage[]; total?: number; version?: string }>;
  getSlides?: () => Promise<{ slides: PreviewPage[]; total?: number; version?: string }>;
  fetchBinary?: () => Promise<Blob>;
}

export interface ReadOnlyFilePreviewProps {
  file: FilePreviewReference;
  source: ReadOnlyPreviewSource;
  content?: string | null;
  blob?: Blob | null;
  canDownload?: boolean;
  onDownload?: () => void;
}
