import { OfficeEditorFormat } from "./fileReferences";
import {
  AiEditTargetKind,
  type EditorLiveApplyMeta,
} from "./editorLiveChat";

export enum AiEditFormatKind {
  Text = "text",
  Markdown = "markdown",
  RichText = "rich_text",
  Word = "word",
  DelimitedText = "delimited_text",
  Spreadsheet = "spreadsheet",
  Presentation = "presentation",
  CodeProject = "code_project",
  Diagram = "diagram",
}

export type AiEditEditorMode =
  | "richtext"
  | "markdown"
  | "code"
  | "text"
  | "spreadsheet"
  | "presentation"
  | "diagram";

export type AiEditSnapshotPoint = "baseline" | "current";

export type AiEditTurnTarget = Readonly<{
  documentName: string;
  fileType?: string | null;
  mimeType?: string | null;
  sourcePath?: string | null;
  targetPath?: string;
}>;

export type AiEditFormatImplementation<TPreview> = Readonly<{
  read: (targetPath?: string) => string;
  preview: (
    content: string,
    targetId: string,
    targetPath: string | undefined,
    meta: EditorLiveApplyMeta,
  ) => boolean | void | Promise<boolean | void>;
  restore: (preview: TPreview, point: AiEditSnapshotPoint) => boolean | void;
  persist: (preview: TPreview) => Promise<boolean>;
  turnTarget: (preview: TPreview | null, lockedTargetPath?: string) => AiEditTurnTarget;
  activateTurnTarget?: (target: AiEditTurnTarget) => boolean | void;
}>;

export type AiEditFormatImplementations<TPreview> = Readonly<
  Record<AiEditFormatKind, AiEditFormatImplementation<TPreview>>
>;

export type AiEditFormatAdapter<TPreview> = AiEditFormatImplementation<TPreview> & Readonly<{
  kind: AiEditFormatKind;
  targetKind: AiEditTargetKind;
}>;

export function resolveAiEditFormatKind(
  mode: AiEditEditorMode,
  officeFormat: OfficeEditorFormat,
): AiEditFormatKind {
  if ([OfficeEditorFormat.Doc, OfficeEditorFormat.Docx].includes(officeFormat)) {
    return AiEditFormatKind.Word;
  }
  if ([OfficeEditorFormat.Xls, OfficeEditorFormat.Xlsx].includes(officeFormat)) {
    return AiEditFormatKind.Spreadsheet;
  }
  if (officeFormat === OfficeEditorFormat.Csv) {
    return AiEditFormatKind.DelimitedText;
  }
  if ([OfficeEditorFormat.Ppt, OfficeEditorFormat.Pptx].includes(officeFormat)) {
    return AiEditFormatKind.Presentation;
  }

  switch (mode) {
    case "text":
      return AiEditFormatKind.Text;
    case "markdown":
      return AiEditFormatKind.Markdown;
    case "richtext":
      return AiEditFormatKind.RichText;
    case "spreadsheet":
      return AiEditFormatKind.DelimitedText;
    case "presentation":
      return AiEditFormatKind.Presentation;
    case "code":
      return AiEditFormatKind.CodeProject;
    case "diagram":
      return AiEditFormatKind.Diagram;
  }
}

function aiEditTargetKindForFormat(kind: AiEditFormatKind): AiEditTargetKind {
  if (kind === AiEditFormatKind.CodeProject) return AiEditTargetKind.Project;
  if (kind === AiEditFormatKind.Diagram) return AiEditTargetKind.Diagram;
  return AiEditTargetKind.Document;
}

/** Select one complete format strategy before an AI Edit session starts. */
export function createAiEditFormatAdapter<TPreview>(
  kind: AiEditFormatKind,
  implementations: AiEditFormatImplementations<TPreview>,
): AiEditFormatAdapter<TPreview> {
  const implementation = implementations[kind];
  if (
    !implementation
    || typeof implementation.read !== "function"
    || typeof implementation.preview !== "function"
    || typeof implementation.restore !== "function"
    || typeof implementation.persist !== "function"
    || typeof implementation.turnTarget !== "function"
  ) {
    throw new Error(`AI Edit format ${kind} requires a complete adapter.`);
  }
  return Object.freeze({
    ...implementation,
    kind,
    targetKind: aiEditTargetKindForFormat(kind),
  });
}
