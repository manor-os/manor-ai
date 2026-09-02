import {
  reconcilePresentationTextRuns,
  reconcilePresentationTextSourceMap,
  type PresentationTextEditSpan,
  type PresentationTextSourceMap,
} from "./presentationTextEdits";
import { isPresentationEditablePreset } from "./presentationPresetGeometry";
import { isPresentationStrokeDash } from "./presentationShapeStyle";

export const PRESENTATION_LIVE_EDIT_FORMAT = "manor-presentation-edit-v2";

/** Browser-previewable insert subset, using canonical native operation identifiers. */
export enum PresentationLiveInsertOperation {
  TextboxInsert = "textbox.insert",
  ShapeInsert = "shape.insert",
  TableInsert = "table.insert",
}

export interface PresentationLiveEditText {
  text: string;
  sourceMap?: PresentationTextSourceMap;
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number;
  color?: string;
  fontFamily?: string;
  baseline?: number;
  spacing?: number;
  runs?: Array<{
    text: string;
    bold?: boolean;
    italic?: boolean;
    underline?: boolean;
    strikethrough?: boolean;
    fontSize?: number;
    color?: string;
    fontFamily?: string;
    baseline?: number;
    spacing?: number;
  }>;
  align?: string;
  bullet?: string;
  indent?: number;
  indentRight?: number;
  hanging?: number;
  lineSpacing?: number;
  spaceBefore?: number;
  spaceAfter?: number;
}

export interface PresentationLiveEditTableCell {
  text: string;
  sourceMap?: PresentationTextSourceMap;
  bold?: boolean;
  italic?: boolean;
  fontSize?: number;
  fontFamily?: string;
  color?: string;
  fill?: string;
  gridSpan?: number;
  vMerge?: boolean;
}

interface PresentationLiveGradient {
  angle: number;
  stops: Array<{ pos: number; color: string; alpha: number }>;
}

interface PresentationLiveShapeFormat {
  fill?: string | null;
  gradientFill?: PresentationLiveGradient | null;
  opacity?: number;
  stroke?: string | null;
  strokeWidth?: number;
  strokeDash?: string | null;
  preset?: string;
  borderRadius?: number;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number } | null;
  verticalAlignment?: "top" | "middle" | "bottom";
  wordWrap?: boolean;
  padding?: { l: number; t: number; r: number; b: number };
}

interface PresentationLiveTextEdit {
  start: number;
  end: number;
  text: string;
}

interface PresentationLiveTextFormat {
  bold?: boolean;
  italic?: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  fontSize?: number;
  color?: string;
  fontFamily?: string;
  align?: string;
  bullet?: string | null;
  indent?: number;
  indentRight?: number;
  hanging?: number;
  lineSpacing?: number;
  spaceBefore?: number;
  spaceAfter?: number;
  baseline?: number;
  spacing?: number;
}

interface PresentationLiveParagraphState {
  index: number;
  text: string;
  edits?: PresentationLiveTextEdit[];
  format?: PresentationLiveTextFormat;
}

interface PresentationLiveTableCellState {
  row: number;
  column: number;
  text: string;
  edits?: PresentationLiveTextEdit[];
  format?: Pick<PresentationLiveEditTableCell, "bold" | "italic" | "fontSize" | "fontFamily" | "color" | "fill">;
}

export interface PresentationLiveEditShape {
  id: string;
  type?: string;
  x: number;
  y: number;
  w: number;
  h: number;
  rotation?: number;
  flipH?: boolean;
  flipV?: boolean;
  fill?: string;
  gradFill?: PresentationLiveGradient;
  opacity?: number;
  stroke?: string;
  strokeWidth?: number;
  strokeDash?: string;
  presetGeom?: string;
  borderRadius?: number;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number };
  vAlign?: "top" | "middle" | "bottom";
  wordWrap?: boolean;
  padding?: { l: number; t: number; r: number; b: number };
  imgCrop?: { l: number; t: number; r: number; b: number };
  imgUrl?: string;
  graphicPreviewUrl?: string;
  texts: PresentationLiveEditText[];
  tableRows?: PresentationLiveEditTableCell[][];
  source?: {
    editable: boolean;
    kind?: "sp" | "pic" | "cxnSp" | "graphicFrame";
    mediaPart?: string;
  };
}

export interface PresentationLiveEditSlide {
  id: string;
  aspectRatio?: string;
  bg?: string;
  bgGrad?: PresentationLiveGradient;
  notes?: string;
  sourcePart?: string;
  notesPart?: string;
  shapes: PresentationLiveEditShape[];
}

export enum PresentationSlideLayout {
  TitleBody = "title-body",
  TitleOnly = "title-only",
  Section = "section",
  Blank = "blank",
}

export type PresentationSlideFactoryInput = Readonly<{
  id: string;
  layout: PresentationSlideLayout;
  aspectRatio?: string;
  title?: string;
  subtitle?: string;
  body?: string;
}>;

export type GeneratedPresentationSlide = Omit<PresentationLiveEditSlide, "shapes"> & {
  shapes: Array<Omit<PresentationLiveEditShape, "source">>;
};

function generatedPresentationTextShape(
  id: string,
  geometry: Pick<PresentationLiveEditShape, "x" | "y" | "w" | "h">,
  texts: PresentationLiveEditText[],
): Omit<PresentationLiveEditShape, "source"> {
  return { id, type: "shape", ...geometry, texts };
}

/** Shared factory for user-created and AI-created editable slides. */
export function createPresentationSlide(input: PresentationSlideFactoryInput): GeneratedPresentationSlide {
  const title = input.title ?? (
    input.layout === PresentationSlideLayout.Section ? "Section Title" : "New Slide Title"
  );
  const body = input.body ?? (
    input.layout === PresentationSlideLayout.Section ? "Section description" : "Click to edit text"
  );
  const base = {
    id: input.id,
    bg: "#ffffff",
    aspectRatio: input.aspectRatio || "16/9",
  };
  if (input.layout === PresentationSlideLayout.Blank) return { ...base, shapes: [] };
  if (input.layout === PresentationSlideLayout.TitleOnly) {
    return {
      ...base,
      shapes: [generatedPresentationTextShape(
        `${input.id}-title`,
        { x: 10, y: 28, w: 80, h: 28 },
        [{ text: title, bold: true, fontSize: 36, color: "#1c1917", align: "center" }],
      )],
    };
  }
  if (input.layout === PresentationSlideLayout.Section) {
    return {
      ...base,
      shapes: [
        generatedPresentationTextShape(
          `${input.id}-title`,
          { x: 10, y: 28, w: 80, h: 22 },
          [{ text: title, bold: true, fontSize: 38, color: "#1c1917", align: "center" }],
        ),
        generatedPresentationTextShape(
          `${input.id}-subtitle`,
          { x: 18, y: 54, w: 64, h: 14 },
          [{
            text: input.subtitle ?? body,
            fontSize: 18,
            color: "#78716c",
            align: "center",
          }],
        ),
      ],
    };
  }
  const bodyParagraphs = body.split(/\r?\n/).filter(Boolean);
  return {
    ...base,
    shapes: [
      generatedPresentationTextShape(
        `${input.id}-title`,
        { x: 10, y: 12, w: 80, h: 20 },
        [{ text: title, bold: true, fontSize: 32, color: "#1c1917", align: "center" }],
      ),
      generatedPresentationTextShape(
        `${input.id}-body`,
        { x: 10, y: 38, w: 80, h: 46 },
        (bodyParagraphs.length > 0 ? bodyParagraphs : [""]).map((text) => ({
          text,
          fontSize: 18,
          color: "#57534e",
        })),
      ),
    ],
  };
}

export interface PresentationLiveEditTarget {
  activeSlideIndex: number;
  selectedShapeId: string | null;
}

interface PresentationLiveShapeState {
  id: string;
  type: string;
  editable: boolean;
  fullSlide: boolean;
  x: number;
  y: number;
  w: number;
  h: number;
  rotation: number;
  flipH: boolean;
  flipV: boolean;
  create?: { operation: PresentationLiveInsertOperation };
  format?: PresentationLiveShapeFormat;
  paragraphs?: PresentationLiveParagraphState[];
  table?: PresentationLiveTableCellState[][];
  image?: {
    attachedAs?: string;
    crop: { l: number; t: number; r: number; b: number };
  };
}

type PresentationImageCropState = NonNullable<PresentationLiveShapeState["image"]>["crop"];

interface PresentationLiveSlideState {
  id: string;
  slideNumber: number;
  active: boolean;
  notes?: string;
  background?: { color?: string | null; gradient?: PresentationLiveGradient | null };
  create?: {
    layout: PresentationSlideLayout;
    title?: string;
    subtitle?: string;
    body?: string;
    aspectRatio?: string;
  };
  shapes?: PresentationLiveShapeState[];
}

export interface PresentationLiveEditState {
  format: typeof PRESENTATION_LIVE_EDIT_FORMAT;
  documentName: string;
  activeSlide: number;
  targetShapeId: string | null;
  instructions: string[];
  slides: PresentationLiveSlideState[];
}

function finiteNumber(value: unknown, fallback: number): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function clamp(value: unknown, min: number, max: number, fallback: number): number {
  return Math.min(max, Math.max(min, finiteNumber(value, fallback)));
}

function liveEditString(value: unknown, maxLength = 1_000): string | null {
  return typeof value === "string"
    && value.length <= maxLength
    && !/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/.test(value)
    ? value
    : null;
}

function presentationTextFormat(text: PresentationLiveEditText): PresentationLiveTextFormat | undefined {
  const format: PresentationLiveTextFormat = {};
  for (const key of ["bold", "italic", "underline", "strikethrough"] as const) {
    if (typeof text[key] === "boolean") format[key] = text[key];
  }
  for (const key of ["fontSize", "indent", "indentRight", "hanging", "lineSpacing", "spaceBefore", "spaceAfter", "baseline", "spacing"] as const) {
    if (typeof text[key] === "number" && Number.isFinite(text[key])) format[key] = text[key];
  }
  for (const key of ["color", "fontFamily", "align"] as const) {
    if (typeof text[key] === "string") format[key] = text[key];
  }
  if (typeof text.bullet === "string") format.bullet = text.bullet;
  return Object.keys(format).length > 0 ? format : undefined;
}

function presentationTableCellFormat(
  cell: PresentationLiveEditTableCell,
): PresentationLiveTableCellState["format"] | undefined {
  const format: NonNullable<PresentationLiveTableCellState["format"]> = {};
  for (const key of ["bold", "italic"] as const) {
    if (typeof cell[key] === "boolean") format[key] = cell[key];
  }
  if (typeof cell.fontSize === "number" && Number.isFinite(cell.fontSize)) format.fontSize = cell.fontSize;
  for (const key of ["fontFamily", "color", "fill"] as const) {
    if (typeof cell[key] === "string") format[key] = cell[key];
  }
  return Object.keys(format).length > 0 ? format : undefined;
}

function presentationShapeFormat(shape: PresentationLiveEditShape): PresentationLiveShapeFormat {
  return {
    fill: shape.fill ?? null,
    gradientFill: shape.gradFill ?? null,
    ...(shape.opacity == null ? {} : { opacity: shape.opacity }),
    stroke: shape.stroke ?? null,
    ...(shape.strokeWidth == null ? {} : { strokeWidth: shape.strokeWidth }),
    strokeDash: shape.strokeDash ?? null,
    ...(shape.presetGeom == null ? {} : { preset: shape.presetGeom }),
    ...(shape.borderRadius == null ? {} : { borderRadius: shape.borderRadius }),
    shadow: shape.shadow ?? null,
    ...(shape.vAlign == null ? {} : { verticalAlignment: shape.vAlign }),
    ...(shape.wordWrap == null ? {} : { wordWrap: shape.wordWrap }),
    ...(shape.padding == null ? {} : { padding: shape.padding }),
  };
}

function isEditable(shape: PresentationLiveEditShape): boolean {
  return shape.source?.editable !== false;
}

function preservesLockedPresentationShapeOrder(
  shapes: PresentationLiveEditShape[],
  states: PresentationLiveShapeState[],
): boolean {
  const sourceIndex = new Map(shapes.map((shape, index) => [shape.id, index]));
  const nextIndex = new Map(states.map((state, index) => [state.id, index]));
  for (const lockedShape of shapes.filter((shape) => !isEditable(shape))) {
    const lockedSourceIndex = sourceIndex.get(lockedShape.id);
    const lockedNextIndex = nextIndex.get(lockedShape.id);
    if (lockedSourceIndex == null || lockedNextIndex == null) return false;
    for (const shape of shapes) {
      if (shape.id === lockedShape.id) continue;
      const shapeNextIndex = nextIndex.get(shape.id);
      const shapeSourceIndex = sourceIndex.get(shape.id);
      if (shapeNextIndex == null || shapeSourceIndex == null) continue;
      if (
        (shapeSourceIndex < lockedSourceIndex) !== (shapeNextIndex < lockedNextIndex)
      ) return false;
    }
  }
  return true;
}

function isFullSlideImage(shape: PresentationLiveEditShape): boolean {
  return Boolean(
    shape.imgUrl
      && shape.x <= 2
      && shape.y <= 2
      && shape.x + shape.w >= 98
      && shape.y + shape.h >= 98,
  );
}

export function presentationLiveEditTargetShape(
  slides: PresentationLiveEditSlide[],
  target: PresentationLiveEditTarget,
): PresentationLiveEditShape | null {
  const slide = slides[Math.max(0, Math.min(target.activeSlideIndex, slides.length - 1))];
  if (!slide) return null;
  const selected = target.selectedShapeId
    ? slide.shapes.find((shape) => shape.id === target.selectedShapeId && isEditable(shape))
    : null;
  if (selected) return selected;
  return slide.shapes.find((shape) => isEditable(shape) && isFullSlideImage(shape))
    || slide.shapes.find((shape) => isEditable(shape) && Boolean(shape.imgUrl))
    || null;
}

function shapeState(
  shape: PresentationLiveEditShape,
  slideNumber: number,
  attachedShapeId: string | null,
): PresentationLiveShapeState {
  const state: PresentationLiveShapeState = {
    id: shape.id,
    type: shape.imgUrl ? "image" : shape.tableRows ? "table" : shape.type || "shape",
    editable: isEditable(shape),
    fullSlide: isFullSlideImage(shape),
    x: Number(shape.x.toFixed(2)),
    y: Number(shape.y.toFixed(2)),
    w: Number(shape.w.toFixed(2)),
    h: Number(shape.h.toFixed(2)),
    rotation: Number((shape.rotation || 0).toFixed(2)),
    flipH: Boolean(shape.flipH),
    flipV: Boolean(shape.flipV),
    format: presentationShapeFormat(shape),
  };
  if (shape.texts.length > 0) {
    state.paragraphs = shape.texts.map((paragraph, index) => ({
      index,
      text: paragraph.text,
      edits: [],
      ...(presentationTextFormat(paragraph) ? { format: presentationTextFormat(paragraph) } : {}),
    }));
  }
  if (shape.tableRows) {
    state.table = shape.tableRows.map((row, rowIndex) => row.map((cell, columnIndex) => ({
      row: rowIndex,
      column: columnIndex,
      text: cell.text,
      edits: [],
      ...(presentationTableCellFormat(cell) ? { format: presentationTableCellFormat(cell) } : {}),
    })));
  }
  if (shape.imgUrl) {
    state.image = {
      ...(shape.id === attachedShapeId
        ? { attachedAs: `current-slide-${slideNumber}-image${mediaExtension(shape.source?.mediaPart)}` }
        : {}),
      crop: {
        l: Number((shape.imgCrop?.l || 0).toFixed(2)),
        t: Number((shape.imgCrop?.t || 0).toFixed(2)),
        r: Number((shape.imgCrop?.r || 0).toFixed(2)),
        b: Number((shape.imgCrop?.b || 0).toFixed(2)),
      },
    };
  }
  return state;
}

export function buildPresentationLiveEditContent(
  slides: PresentationLiveEditSlide[],
  options: PresentationLiveEditTarget & { documentName: string },
): string {
  const activeSlideIndex = Math.max(0, Math.min(options.activeSlideIndex, Math.max(0, slides.length - 1)));
  const targetShape = presentationLiveEditTargetShape(slides, {
    activeSlideIndex,
    selectedShapeId: options.selectedShapeId,
  });
  const state: PresentationLiveEditState = {
    format: PRESENTATION_LIVE_EDIT_FORMAT,
    documentName: options.documentName,
    activeSlide: activeSlideIndex + 1,
    targetShapeId: targetShape?.id || null,
    instructions: [
      "Preserve slide IDs. Slides and editable shapes may be reordered or removed; shapes with editable=false must remain unchanged.",
      "To add a slide, append one slides item with a unique ai-slide-* id and create:{layout,title,body}; supported layouts are title-body, title-only, section, and blank.",
      "Add one slide scaffold per patch before filling its title/body so the editor can preview creation while the response streams.",
      "This JSON is a browser preview fallback. Native PPTX AI edits use patch_file. If this fallback is used, a new editable object may use only shape.insert, textbox.insert, or table.insert.",
      "Shape format supports fill/gradientFill, stroke/strokeWidth/strokeDash, preset, shadow, text padding/alignment/wrap; opacity is image-only. Table/chart frames use cell/chart-specific formatting. Paragraph and table-cell format may be changed without replacing unrelated objects.",
      "Whenever text changes, update text and add ordered zero-based character edits against the original text as {start,end,text}; use separate edits for separate replacements.",
      "Coordinates and crop values are percentages from 0 to 100.",
      "For semantic image changes, generate a replacement from the attached image instead of adding image bytes here.",
    ],
    slides: slides.map((slide, index) => ({
      id: slide.id,
      slideNumber: index + 1,
      active: index === activeSlideIndex,
      notes: slide.notes || "",
      background: {
        color: slide.bg ?? null,
        gradient: slide.bgGrad ?? null,
      },
      shapes: slide.shapes.map((shape) => shapeState(shape, index + 1, targetShape?.id || null)),
    })),
  };
  return JSON.stringify(state, null, 2);
}

function parseState(content: string): PresentationLiveEditState | null {
  try {
    const parsed = JSON.parse(content) as Partial<PresentationLiveEditState>;
    if (parsed.format !== PRESENTATION_LIVE_EDIT_FORMAT || !Array.isArray(parsed.slides)) return null;
    return parsed as PresentationLiveEditState;
  } catch {
    return null;
  }
}

function normalizedCrop(
  value: PresentationImageCropState,
  fallback: PresentationLiveEditShape["imgCrop"],
): PresentationLiveEditShape["imgCrop"] {
  const crop = {
    l: clamp(value?.l, 0, 95, fallback?.l || 0),
    t: clamp(value?.t, 0, 95, fallback?.t || 0),
    r: clamp(value?.r, 0, 95, fallback?.r || 0),
    b: clamp(value?.b, 0, 95, fallback?.b || 0),
  };
  if (crop.l + crop.r >= 99) crop.r = Math.max(0, 99 - crop.l);
  if (crop.t + crop.b >= 99) crop.b = Math.max(0, 99 - crop.t);
  return crop.l || crop.t || crop.r || crop.b ? crop : undefined;
}

function presentationLiveTextEditSpans(
  originalText: string,
  editedText: string,
  edits: PresentationLiveTextEdit[] | undefined,
): PresentationTextEditSpan[] | null {
  if (originalText === editedText) return [];
  if (!Array.isArray(edits) || edits.length === 0) return null;
  const originalCharacters = Array.from(originalText);
  const rendered: string[] = [];
  const spans: PresentationTextEditSpan[] = [];
  let originalCursor = 0;
  let editedCursor = 0;
  for (const edit of edits) {
    if (
      !Number.isInteger(edit.start)
      || !Number.isInteger(edit.end)
      || edit.start < originalCursor
      || edit.end < edit.start
      || edit.end > originalCharacters.length
      || typeof edit.text !== "string"
    ) return null;
    const unchanged = originalCharacters.slice(originalCursor, edit.start);
    const inserted = Array.from(edit.text);
    rendered.push(...unchanged, ...inserted);
    editedCursor += unchanged.length;
    spans.push({
      originalStart: edit.start,
      originalEnd: edit.end,
      editedStart: editedCursor,
      editedEnd: editedCursor + inserted.length,
    });
    originalCursor = edit.end;
    editedCursor += inserted.length;
  }
  rendered.push(...originalCharacters.slice(originalCursor));
  return rendered.join("") === editedText ? spans : null;
}

function presentationLiveColor(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 64) return null;
  const color = value.trim();
  return color === "transparent"
    || /^#[\da-f]{3}(?:[\da-f]{3})?$/i.test(color)
    || /^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(?:\s*,\s*(?:0|1|0?\.\d+))?\s*\)$/i.test(color)
    ? color
    : null;
}

function normalizedPresentationGradient(value: unknown): PresentationLiveGradient | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<PresentationLiveGradient>;
  if (!Number.isFinite(candidate.angle) || !Array.isArray(candidate.stops)
    || candidate.stops.length < 2 || candidate.stops.length > 16) return null;
  let previous = -1;
  const stops: PresentationLiveGradient["stops"] = [];
  for (const stop of candidate.stops) {
    if (!stop || typeof stop !== "object" || !Number.isFinite(stop.pos) || stop.pos < previous) return null;
    const color = presentationLiveColor(stop.color);
    if (!color) return null;
    const pos = clamp(stop.pos, 0, 100, 0);
    const alpha = clamp(stop.alpha, 0, 1, 1);
    previous = pos;
    stops.push({ pos, color, alpha });
  }
  return { angle: clamp(candidate.angle, 0, 360, 0), stops };
}

function mergePresentationTextFormat(
  text: PresentationLiveEditText,
  format: PresentationLiveTextFormat | undefined,
): PresentationLiveEditText | null {
  if (format == null) return text;
  if (typeof format !== "object") return null;
  const next = { ...text };
  for (const key of ["bold", "italic", "underline", "strikethrough"] as const) {
    if (format[key] !== undefined) {
      if (typeof format[key] !== "boolean") return null;
      next[key] = format[key];
    }
  }
  const numericLimits: Record<string, [number, number]> = {
    fontSize: [1, 409], indent: [0, 4_032], indentRight: [0, 4_032], hanging: [-4_032, 4_032],
    lineSpacing: [0.1, 20], spaceBefore: [0, 4_032], spaceAfter: [0, 4_032], baseline: [-100, 100], spacing: [-100, 100],
  };
  for (const key of Object.keys(numericLimits) as Array<keyof typeof numericLimits>) {
    const value = format[key as keyof PresentationLiveTextFormat];
    if (value !== undefined) {
      if (typeof value !== "number" || !Number.isFinite(value)) return null;
      const [minimum, maximum] = numericLimits[key];
      (next as unknown as Record<string, unknown>)[key] = Math.min(maximum, Math.max(minimum, value));
    }
  }
  if (format.color !== undefined) {
    const color = presentationLiveColor(format.color);
    if (!color) return null;
    next.color = color;
  }
  for (const key of ["fontFamily", "align"] as const) {
    if (format[key] !== undefined) {
      const value = liveEditString(format[key], 255);
      if (value == null) return null;
      next[key] = value;
    }
  }
  if (format.bullet !== undefined) {
    if (format.bullet === null) delete next.bullet;
    else {
      const bullet = liveEditString(format.bullet, 16);
      if (bullet == null) return null;
      next.bullet = bullet;
    }
  }
  return next;
}

function mergePresentationTableCellFormat(
  cell: PresentationLiveEditTableCell,
  format: PresentationLiveTableCellState["format"],
): PresentationLiveEditTableCell | null {
  if (format == null) return cell;
  if (typeof format !== "object") return null;
  const next = { ...cell };
  for (const key of ["bold", "italic"] as const) {
    if (format[key] !== undefined) {
      if (typeof format[key] !== "boolean") return null;
      next[key] = format[key];
    }
  }
  if (format.fontSize !== undefined) {
    if (!Number.isFinite(format.fontSize)) return null;
    next.fontSize = clamp(format.fontSize, 1, 409, cell.fontSize || 12);
  }
  if (format.fontFamily !== undefined) {
    const family = liveEditString(format.fontFamily, 255);
    if (family == null) return null;
    next.fontFamily = family;
  }
  for (const key of ["color", "fill"] as const) {
    if (format[key] !== undefined) {
      const color = presentationLiveColor(format[key]);
      if (!color) return null;
      next[key] = color;
    }
  }
  return next;
}

function mergePresentationShapeFormat(
  shape: PresentationLiveEditShape,
  format: PresentationLiveShapeFormat | undefined,
): PresentationLiveEditShape | null {
  if (format == null) return shape;
  if (typeof format !== "object") return null;
  const next = { ...shape };
  const fillChanged = format.fill !== undefined && format.fill !== (shape.fill ?? null);
  const gradientChanged = format.gradientFill !== undefined
    && JSON.stringify(format.gradientFill) !== JSON.stringify(shape.gradFill ?? null);
  if (fillChanged && gradientChanged && format.fill !== null && format.gradientFill !== null) return null;
  if (fillChanged) {
    if (format.fill === null) delete next.fill;
    else {
      const color = presentationLiveColor(format.fill);
      if (!color) return null;
      next.fill = color;
    }
  }
  if (gradientChanged) {
    if (format.gradientFill === null) delete next.gradFill;
    else {
      const gradient = normalizedPresentationGradient(format.gradientFill);
      if (!gradient) return null;
      next.gradFill = gradient;
    }
  }
  if (gradientChanged && format.gradientFill !== null) delete next.fill;
  if (fillChanged && format.fill !== null) delete next.gradFill;
  const strokeValue = format.stroke;
  if (strokeValue !== undefined) {
    if (strokeValue === null) delete next.stroke;
    else {
      const color = presentationLiveColor(strokeValue);
      if (!color) return null;
      next.stroke = color;
    }
  }
  for (const [stateKey, shapeKey, minimum, maximum] of [
    ["opacity", "opacity", 0, 1],
    ["strokeWidth", "strokeWidth", 0, 72],
    ["borderRadius", "borderRadius", 0, 50],
  ] as const) {
    const value = format[stateKey];
    if (value === undefined) continue;
    if (!Number.isFinite(value)) return null;
    next[shapeKey] = Math.min(maximum, Math.max(minimum, value));
  }
  if (format.strokeDash !== undefined) {
    if (format.strokeDash === null) delete next.strokeDash;
    else {
      if (format.strokeDash !== shape.strokeDash && !isPresentationStrokeDash(format.strokeDash)) return null;
      next.strokeDash = format.strokeDash;
    }
  }
  if (format.preset !== undefined) {
    if (format.preset !== shape.presetGeom && !isPresentationEditablePreset(format.preset)) return null;
    next.presetGeom = format.preset;
  }
  if (format.shadow !== undefined) {
    if (format.shadow === null) delete next.shadow;
    else {
      const color = presentationLiveColor(format.shadow.color);
      if (!color || ![format.shadow.blur, format.shadow.dist, format.shadow.angle, format.shadow.alpha].every(Number.isFinite)) return null;
      next.shadow = {
        blur: clamp(format.shadow.blur, 0, 4_032, 0),
        dist: clamp(format.shadow.dist, 0, 4_032, 0),
        angle: clamp(format.shadow.angle, 0, 360, 0),
        color,
        alpha: clamp(format.shadow.alpha, 0, 1, 1),
      };
    }
  }
  if (format.verticalAlignment !== undefined) {
    if (!["top", "middle", "bottom"].includes(format.verticalAlignment)) return null;
    next.vAlign = format.verticalAlignment;
  }
  if (format.wordWrap !== undefined) {
    if (typeof format.wordWrap !== "boolean") return null;
    next.wordWrap = format.wordWrap;
  }
  if (format.padding !== undefined) {
    if (!format.padding || typeof format.padding !== "object") return null;
    const values = [format.padding.l, format.padding.t, format.padding.r, format.padding.b];
    if (!values.every((value) => Number.isFinite(value) && value >= 0 && value <= 4_032)) return null;
    next.padding = { ...format.padding };
  }
  return next;
}

function presentationShapeFormatHasChange(
  shape: PresentationLiveEditShape,
  format: PresentationLiveShapeFormat | undefined,
): boolean {
  if (!format || typeof format !== "object") return false;
  const current = presentationShapeFormat(shape);
  return (Object.keys(format) as Array<keyof PresentationLiveShapeFormat>).some((key) => (
    format[key] !== undefined
    && JSON.stringify(format[key]) !== JSON.stringify(current[key])
  ));
}

function presentationShapeFormatIsPersistable(
  shape: PresentationLiveEditShape,
  format: PresentationLiveShapeFormat | undefined,
): boolean {
  if (!presentationShapeFormatHasChange(shape, format)) return true;
  if (shape.source?.kind === "graphicFrame" || shape.tableRows) return false;
  if (
    format?.opacity !== undefined
    && format.opacity !== shape.opacity
    && !shape.imgUrl
    && shape.source?.kind !== "pic"
  ) return false;
  return true;
}

function presentationLiveParagraphs(
  states: PresentationLiveParagraphState[] | undefined,
): PresentationLiveEditText[] | null {
  if (!Array.isArray(states) || states.length > 200
    || !states.every((paragraph, index) => paragraph.index === index)) return null;
  const paragraphs: PresentationLiveEditText[] = [];
  for (const state of states) {
    const text = liveEditString(state.text, 50_000);
    if (text == null) return null;
    const paragraph = mergePresentationTextFormat({ text }, state.format);
    if (!paragraph) return null;
    paragraphs.push(paragraph);
  }
  return paragraphs;
}

function mergeShape(
  shape: PresentationLiveEditShape,
  state: PresentationLiveShapeState,
): PresentationLiveEditShape | null {
  if (!isEditable(shape) || state.editable === false) return shape;
  const w = clamp(state.w, 1, 100, shape.w);
  const h = clamp(state.h, 0.5, 100, shape.h);
  let next: PresentationLiveEditShape = {
    ...shape,
    w,
    h,
    x: clamp(state.x, 0, 100 - w, shape.x),
    y: clamp(state.y, 0, 100 - h, shape.y),
    rotation: clamp(state.rotation, -360, 360, shape.rotation || 0),
    flipH: typeof state.flipH === "boolean" ? state.flipH : shape.flipH,
    flipV: typeof state.flipV === "boolean" ? state.flipV : shape.flipV,
  };
  if (!presentationShapeFormatIsPersistable(shape, state.format)) return null;
  const formattedShape = mergePresentationShapeFormat(next, state.format);
  if (!formattedShape) return null;
  next = formattedShape;
  if (shape.imgUrl && state.image) {
    next.imgCrop = normalizedCrop(state.image.crop, shape.imgCrop);
  }
  if (shape.texts.length > 0) {
    if (
      state.paragraphs?.length !== shape.texts.length
      || !state.paragraphs.every((paragraph, index) => paragraph.index === index)
    ) return null;
    next.texts = shape.texts.map((paragraph, index) => {
      const paragraphState = state.paragraphs![index];
      const text = liveEditString(paragraphState.text, 50_000);
      if (text == null) return null;
      let editedParagraph: PresentationLiveEditText = paragraph;
      if (text !== paragraph.text) {
        const editSpans = presentationLiveTextEditSpans(paragraph.text, text, paragraphState.edits);
        if (!editSpans) return null;
        const sourceMap = reconcilePresentationTextSourceMap(
          paragraph.text,
          text,
          paragraph.sourceMap,
          editSpans,
        );
        editedParagraph = {
          ...paragraph,
          text,
          sourceMap,
          runs: reconcilePresentationTextRuns(
            paragraph.runs,
            paragraph.text,
            text,
            sourceMap,
            paragraph.sourceMap,
          ),
        };
      }
      return mergePresentationTextFormat(editedParagraph, paragraphState.format);
    }).filter((paragraph): paragraph is PresentationLiveEditText => paragraph != null);
    if (next.texts.length !== shape.texts.length) return null;
  } else if (state.paragraphs !== undefined) {
    const isNativeTextShape = shape.source
      ? shape.source.kind === "sp"
      : shape.type == null || shape.type === "shape" || shape.type === "textbox";
    if (!isNativeTextShape || shape.imgUrl || shape.tableRows) return null;
    const paragraphs = presentationLiveParagraphs(state.paragraphs);
    if (!paragraphs) return null;
    next.texts = paragraphs;
  }
  if (shape.tableRows) {
    if (state.table?.length !== shape.tableRows.length) return null;
    const sameColumns = shape.tableRows.every((row, rowIndex) => (
      state.table?.[rowIndex]?.length === row.length
      && state.table[rowIndex]!.every((cell, columnIndex) => cell.row === rowIndex && cell.column === columnIndex)
    ));
    if (!sameColumns) return null;
    const rows: PresentationLiveEditTableCell[][] = [];
    for (let rowIndex = 0; rowIndex < shape.tableRows.length; rowIndex += 1) {
      const row: PresentationLiveEditTableCell[] = [];
      for (let columnIndex = 0; columnIndex < shape.tableRows[rowIndex].length; columnIndex += 1) {
        const cell = shape.tableRows[rowIndex][columnIndex];
        const cellState = state.table[rowIndex][columnIndex];
        const text = liveEditString(cellState.text, 50_000);
        if (text == null) return null;
        let editedCell: PresentationLiveEditTableCell = cell;
        if (text !== cell.text) {
          const editSpans = presentationLiveTextEditSpans(cell.text, text, cellState.edits);
          if (!editSpans) return null;
          editedCell = {
            ...cell,
            text,
            sourceMap: reconcilePresentationTextSourceMap(cell.text, text, cell.sourceMap, editSpans),
          };
        }
        const formattedCell = mergePresentationTableCellFormat(editedCell, cellState.format);
        if (!formattedCell) return null;
        row.push(formattedCell);
      }
      rows.push(row);
    }
    next.tableRows = rows;
  }
  return next;
}

function createPresentationLiveShape(state: PresentationLiveShapeState): PresentationLiveEditShape | null {
  if (!state.id.startsWith("ai-shape-") || !state.create || state.editable === false) return null;
  if (!Object.values(PresentationLiveInsertOperation).includes(state.create.operation)) return null;
  if (![state.x, state.y, state.w, state.h, state.rotation].every((value) => Number.isFinite(Number(value)))) return null;
  const w = clamp(state.w, 1, 100, 20);
  const h = clamp(state.h, 0.5, 100, 10);
  let shape: PresentationLiveEditShape = {
    id: state.id,
    type: state.create.operation === PresentationLiveInsertOperation.TableInsert
      ? "table"
      : state.create.operation === PresentationLiveInsertOperation.TextboxInsert
        ? "textbox"
        : "shape",
    x: clamp(state.x, 0, 100 - w, 0),
    y: clamp(state.y, 0, 100 - h, 0),
    w,
    h,
    rotation: clamp(state.rotation, -360, 360, 0),
    flipH: Boolean(state.flipH),
    flipV: Boolean(state.flipV),
    presetGeom: "rect",
    texts: [],
  };
  if (!presentationShapeFormatIsPersistable(shape, state.format)) return null;
  if (
    state.create.operation === PresentationLiveInsertOperation.TableInsert
    && presentationShapeFormatHasChange(shape, state.format)
  ) return null;
  const formatted = mergePresentationShapeFormat(shape, state.format);
  if (!formatted) return null;
  shape = formatted;

  if (state.create.operation === PresentationLiveInsertOperation.TableInsert) {
    if (!Array.isArray(state.table) || state.table.length < 1 || state.table.length > 200) return null;
    const width = state.table[0]?.length || 0;
    if (width < 1 || width > 50 || state.table.some((row) => row.length !== width)) return null;
    const rows: PresentationLiveEditTableCell[][] = [];
    for (let rowIndex = 0; rowIndex < state.table.length; rowIndex += 1) {
      const row: PresentationLiveEditTableCell[] = [];
      for (let columnIndex = 0; columnIndex < width; columnIndex += 1) {
        const cellState = state.table[rowIndex][columnIndex];
        if (cellState.row !== rowIndex || cellState.column !== columnIndex) return null;
        const text = liveEditString(cellState.text, 50_000);
        if (text == null) return null;
        const cell = mergePresentationTableCellFormat({ text }, cellState.format);
        if (!cell) return null;
        row.push(cell);
      }
      rows.push(row);
    }
    shape.tableRows = rows;
    return shape;
  }
  if (state.table !== undefined) return null;
  if (state.paragraphs !== undefined) {
    const paragraphs = presentationLiveParagraphs(state.paragraphs);
    if (!paragraphs) return null;
    shape.texts = paragraphs;
  }
  return shape;
}

function mergePresentationSlideBackground<T extends PresentationLiveEditSlide>(
  slide: T,
  background: PresentationLiveSlideState["background"],
): T | null {
  if (background == null) return slide;
  if (typeof background !== "object") return null;
  const next = { ...slide };
  const colorChanged = background.color !== undefined && background.color !== (slide.bg ?? null);
  const gradientChanged = background.gradient !== undefined
    && JSON.stringify(background.gradient) !== JSON.stringify(slide.bgGrad ?? null);
  if (colorChanged && gradientChanged && background.color !== null && background.gradient !== null) return null;
  if (colorChanged) {
    if (background.color === null) delete next.bg;
    else {
      const color = presentationLiveColor(background.color);
      if (!color) return null;
      next.bg = color;
    }
  }
  if (gradientChanged) {
    if (background.gradient === null) delete next.bgGrad;
    else {
      const gradient = normalizedPresentationGradient(background.gradient);
      if (!gradient) return null;
      next.bgGrad = gradient;
    }
  }
  if (gradientChanged && background.gradient !== null) delete next.bg;
  if (colorChanged && background.color !== null) delete next.bgGrad;
  return next;
}

export function applyPresentationLiveEditContent<T extends PresentationLiveEditSlide>(
  slides: T[],
  content: string,
): T[] | null {
  const state = parseState(content);
  if (!state || state.slides.length === 0) return null;
  if (state.slides.some((slide) => (
    !slide
    || typeof slide !== "object"
    || typeof slide.id !== "string"
    || !slide.id.trim()
  ))) return null;
  const stateIds = new Set(state.slides.map((slide) => slide.id));
  if (stateIds.size !== state.slides.length) return null;
  const slideById = new Map(slides.map((slide) => [slide.id, slide]));
  const mergedSlides: T[] = [];
  for (const slideState of state.slides) {
    if (slideState.create) {
      if (!slideState.id.startsWith("ai-slide-")) return null;
      if (slideById.get(slideState.id)?.sourcePart) return null;
      if (!Object.values(PresentationSlideLayout).includes(slideState.create.layout)) return null;
      if ([
        slideState.create.title,
        slideState.create.subtitle,
        slideState.create.body,
        slideState.create.aspectRatio,
      ].some((value) => value !== undefined && typeof value !== "string")) return null;
      let created = createPresentationSlide({
        id: slideState.id,
        layout: slideState.create.layout,
        aspectRatio: slideState.create.aspectRatio || slides[0]?.aspectRatio,
        title: slideState.create.title,
        subtitle: slideState.create.subtitle,
        body: slideState.create.body,
      });
      if (slideState.notes !== undefined) {
        const notes = liveEditString(slideState.notes, 100_000);
        if (notes == null) return null;
        created = { ...created, notes };
      }
      const createdWithBackground = mergePresentationSlideBackground(created, slideState.background);
      if (!createdWithBackground) return null;
      created = createdWithBackground;
      if (slideState.shapes !== undefined) {
        if (!Array.isArray(slideState.shapes) || slideState.shapes.length > 500) return null;
        const ids = new Set<string>();
        const addedShapes: PresentationLiveEditShape[] = [];
        for (const shapeState of slideState.shapes) {
          if (!shapeState || typeof shapeState.id !== "string" || ids.has(shapeState.id)) return null;
          ids.add(shapeState.id);
          const added = createPresentationLiveShape(shapeState);
          if (!added) return null;
          addedShapes.push(added);
        }
        created = { ...created, shapes: [...created.shapes, ...addedShapes] };
      }
      mergedSlides.push(created as T);
      continue;
    }
    const slide = slideById.get(slideState.id);
    if (!slide || !Array.isArray(slideState.shapes)) return null;
    if (slideState.shapes.length > 500) return null;
    const shapeStateById = new Map(slideState.shapes.map((shape) => [shape.id, shape]));
    if (shapeStateById.size !== slideState.shapes.length) return null;
    if (slide.shapes.some((shape) => !isEditable(shape) && !shapeStateById.has(shape.id))) return null;
    if (!preservesLockedPresentationShapeOrder(slide.shapes, slideState.shapes)) return null;
    const shapeById = new Map(slide.shapes.map((shape) => [shape.id, shape]));
    const shapes: PresentationLiveEditShape[] = [];
    for (const nextShapeState of slideState.shapes) {
      if (!nextShapeState || typeof nextShapeState.id !== "string" || !nextShapeState.id.trim()) return null;
      const shape = shapeById.get(nextShapeState.id);
      if (shape && nextShapeState.create) return null;
      const merged = shape
        ? mergeShape(shape, nextShapeState)
        : createPresentationLiveShape(nextShapeState);
      if (!merged) return null;
      shapes.push(merged);
    }
    if (slideState.notes !== undefined && liveEditString(slideState.notes, 100_000) == null) return null;
    let next = {
      ...slide,
      ...(typeof slideState.notes === "string" ? { notes: slideState.notes } : {}),
      shapes,
    };
    const backgroundChanged = slideState.background !== undefined
      && JSON.stringify(slideState.background) !== JSON.stringify({
        color: slide.bg ?? null,
        gradient: slide.bgGrad ?? null,
      });
    if (backgroundChanged && (slide as T & { bgImgUrl?: string }).bgImgUrl) return null;
    const backgroundMerged = mergePresentationSlideBackground(next, slideState.background);
    if (!backgroundMerged) return null;
    next = backgroundMerged;
    mergedSlides.push(next as T);
  }
  return mergedSlides;
}

export function presentationImageMime(mediaPart?: string): string {
  const extension = mediaPart?.split(".").pop()?.toLowerCase();
  if (extension === "jpg" || extension === "jpeg") return "image/jpeg";
  if (extension === "webp") return "image/webp";
  if (extension === "gif") return "image/gif";
  if (extension === "svg") return "image/svg+xml";
  return "image/png";
}

export function mediaExtension(mediaPart?: string): string {
  const extension = mediaPart?.split(".").pop()?.toLowerCase();
  if (!extension) return ".png";
  return `.${extension === "jpeg" ? "jpg" : extension}`;
}
