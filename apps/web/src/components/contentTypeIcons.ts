import type { FileReferenceKind } from "../lib/fileReferences";
import {
  IconArchive,
  IconAudioWave,
  IconCode,
  IconDocument,
  IconExcelGrid,
  IconGlobe,
  IconImage,
  IconLayers,
  IconPlay,
  IconPresentation,
  IconReport,
  type IconProps,
} from "./icons";

export type ContentTypeIconKey =
  | "image"
  | "video"
  | "audio"
  | "document"
  | "pdf"
  | "slides"
  | "sheet"
  | "website";

export type ContentTypeIcon = (props: IconProps) => JSX.Element;

/**
 * Canonical icon language for output modes and the files those modes create.
 * Keep every shared type here so mode selectors and file cards cannot drift.
 */
export const CONTENT_TYPE_ICONS: Record<ContentTypeIconKey, ContentTypeIcon> = {
  image: IconImage,
  video: IconPlay,
  audio: IconAudioWave,
  document: IconDocument,
  pdf: IconReport,
  slides: IconPresentation,
  sheet: IconExcelGrid,
  website: IconGlobe,
};

const FILE_REFERENCE_ICONS: Record<FileReferenceKind, ContentTypeIcon> = {
  presentation: CONTENT_TYPE_ICONS.slides,
  pdf: CONTENT_TYPE_ICONS.pdf,
  spreadsheet: CONTENT_TYPE_ICONS.sheet,
  diagram: IconLayers,
  code: IconCode,
  page: CONTENT_TYPE_ICONS.website,
  image: CONTENT_TYPE_ICONS.image,
  video: CONTENT_TYPE_ICONS.video,
  audio: CONTENT_TYPE_ICONS.audio,
  archive: IconArchive,
  document: CONTENT_TYPE_ICONS.document,
  file: CONTENT_TYPE_ICONS.document,
};

export function getFileReferenceIcon(kind: FileReferenceKind): ContentTypeIcon {
  return FILE_REFERENCE_ICONS[kind];
}
