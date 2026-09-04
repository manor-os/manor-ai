import {
  reconcilePresentationTextRuns,
  reconcilePresentationTextSourceMap,
  type PresentationTextEditSpan,
  type PresentationTextSourceMap,
} from "./presentationTextEdits";

export const PRESENTATION_LIVE_EDIT_FORMAT = "manor-presentation-edit-v1";

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
}

export interface PresentationLiveEditTableCell {
  text: string;
  sourceMap?: PresentationTextSourceMap;
}

interface PresentationLiveTextEdit {
  start: number;
  end: number;
  text: string;
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
  imgCrop?: { l: number; t: number; r: number; b: number };
  imgUrl?: string;
  graphicPreviewUrl?: string;
  texts: PresentationLiveEditText[];
  tableRows?: PresentationLiveEditTableCell[][];
  source?: { editable: boolean; mediaPart?: string };
}

export interface PresentationLiveEditSlide {
  id: string;
  aspectRatio?: string;
  bg?: string;
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
  paragraphs?: Array<{ index: number; text: string; edits?: PresentationLiveTextEdit[] }>;
  table?: Array<Array<{ row: number; column: number; text: string; edits?: PresentationLiveTextEdit[] }>>;
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

function isEditable(shape: PresentationLiveEditShape): boolean {
  return shape.source?.editable !== false;
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
  };
  if (shape.texts.length > 0) {
    state.paragraphs = shape.texts.map((paragraph, index) => ({ index, text: paragraph.text, edits: [] }));
  }
  if (shape.tableRows) {
    state.table = shape.tableRows.map((row, rowIndex) => row.map((cell, columnIndex) => ({
      row: rowIndex,
      column: columnIndex,
      text: cell.text,
      edits: [],
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
      "Preserve IDs and shape/paragraph structure for existing slides. Slides may be reordered or removed.",
      "To add a slide, append one slides item with a unique ai-slide-* id and create:{layout,title,body}; supported layouts are title-body, title-only, section, and blank.",
      "Add one slide scaffold per patch before filling its title/body so the editor can preview creation while the response streams.",
      "Whenever text changes, update text and add ordered zero-based character edits against the original text as {start,end,text}; use separate edits for separate replacements.",
      "Coordinates and crop values are percentages from 0 to 100.",
      "For semantic image changes, generate a replacement from the attached image instead of adding image bytes here.",
    ],
    slides: slides.map((slide, index) => ({
      id: slide.id,
      slideNumber: index + 1,
      active: index === activeSlideIndex,
      ...(slide.notesPart ? { notes: slide.notes || "" } : {}),
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

function mergeShape(
  shape: PresentationLiveEditShape,
  state: PresentationLiveShapeState,
): PresentationLiveEditShape | null {
  if (!isEditable(shape) || state.editable === false) return shape;
  const w = clamp(state.w, 1, 100, shape.w);
  const h = clamp(state.h, 0.5, 100, shape.h);
  const next: PresentationLiveEditShape = {
    ...shape,
    w,
    h,
    x: clamp(state.x, 0, 100 - w, shape.x),
    y: clamp(state.y, 0, 100 - h, shape.y),
    rotation: clamp(state.rotation, -360, 360, shape.rotation || 0),
    flipH: typeof state.flipH === "boolean" ? state.flipH : shape.flipH,
    flipV: typeof state.flipV === "boolean" ? state.flipV : shape.flipV,
  };
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
      const text = typeof paragraphState.text === "string"
        ? paragraphState.text
        : paragraph.text;
      if (text === paragraph.text) return paragraph;
      const editSpans = presentationLiveTextEditSpans(paragraph.text, text, paragraphState.edits);
      if (!editSpans) return null;
      const sourceMap = reconcilePresentationTextSourceMap(
        paragraph.text,
        text,
        paragraph.sourceMap,
        editSpans,
      );
      return {
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
    }).filter((paragraph): paragraph is PresentationLiveEditText => paragraph != null);
    if (next.texts.length !== shape.texts.length) return null;
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
        const text = typeof cellState.text === "string" ? cellState.text : cell.text;
        if (text === cell.text) {
          row.push(cell);
          continue;
        }
        const editSpans = presentationLiveTextEditSpans(cell.text, text, cellState.edits);
        if (!editSpans) return null;
        row.push({
          ...cell,
          text,
          sourceMap: reconcilePresentationTextSourceMap(cell.text, text, cell.sourceMap, editSpans),
        });
      }
      rows.push(row);
    }
    next.tableRows = rows;
  }
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
      const created = createPresentationSlide({
        id: slideState.id,
        layout: slideState.create.layout,
        aspectRatio: slideState.create.aspectRatio || slides[0]?.aspectRatio,
        title: slideState.create.title,
        subtitle: slideState.create.subtitle,
        body: slideState.create.body,
      });
      mergedSlides.push(created as T);
      continue;
    }
    const slide = slideById.get(slideState.id);
    if (!slide || !Array.isArray(slideState.shapes)) return null;
    if (slideState.shapes.length !== slide.shapes.length) return null;
    const shapeStateById = new Map(slideState.shapes.map((shape) => [shape.id, shape]));
    if (shapeStateById.size !== slide.shapes.length) return null;
    const shapes: PresentationLiveEditShape[] = [];
    for (const shape of slide.shapes) {
      const nextShapeState = shapeStateById.get(shape.id);
      if (!nextShapeState) return null;
      const merged = mergeShape(shape, nextShapeState);
      if (!merged) return null;
      shapes.push(merged);
    }
    const next = {
      ...slide,
      ...(slide.notesPart && typeof slideState.notes === "string" ? { notes: slideState.notes } : {}),
      shapes,
    };
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
