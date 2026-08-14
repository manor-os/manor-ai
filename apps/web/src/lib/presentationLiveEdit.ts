export const PRESENTATION_LIVE_EDIT_FORMAT = "manor-presentation-edit-v1";

export interface PresentationLiveEditText {
  text: string;
}

export interface PresentationLiveEditTableCell {
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
  texts: PresentationLiveEditText[];
  tableRows?: PresentationLiveEditTableCell[][];
  source?: { editable: boolean; mediaPart?: string };
}

export interface PresentationLiveEditSlide {
  id: string;
  notes?: string;
  notesPart?: string;
  shapes: PresentationLiveEditShape[];
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
  paragraphs?: Array<{ index: number; text: string }>;
  table?: Array<Array<{ row: number; column: number; text: string }>>;
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
  shapes: PresentationLiveShapeState[];
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
    state.paragraphs = shape.texts.map((paragraph, index) => ({ index, text: paragraph.text }));
  }
  if (shape.tableRows) {
    state.table = shape.tableRows.map((row, rowIndex) => row.map((cell, columnIndex) => ({
      row: rowIndex,
      column: columnIndex,
      text: cell.text,
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
      "Edit existing shape fields only; preserve slide and shape IDs and array lengths.",
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

function mergeShape(
  shape: PresentationLiveEditShape,
  state: PresentationLiveShapeState,
): PresentationLiveEditShape {
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
  if (
    state.paragraphs?.length === shape.texts.length
    && state.paragraphs.every((paragraph, index) => paragraph.index === index)
  ) {
    next.texts = shape.texts.map((paragraph, index) => ({
      ...paragraph,
      text: typeof state.paragraphs?.[index]?.text === "string"
        ? state.paragraphs[index]!.text
        : paragraph.text,
      ...(state.paragraphs?.[index]?.text !== paragraph.text ? { runs: undefined } : {}),
    }));
  }
  if (shape.tableRows && state.table?.length === shape.tableRows.length) {
    const sameColumns = shape.tableRows.every((row, rowIndex) => (
      state.table?.[rowIndex]?.length === row.length
      && state.table[rowIndex]!.every((cell, columnIndex) => cell.row === rowIndex && cell.column === columnIndex)
    ));
    if (sameColumns) {
      next.tableRows = shape.tableRows.map((row, rowIndex) => row.map((cell, columnIndex) => ({
        ...cell,
        text: typeof state.table?.[rowIndex]?.[columnIndex]?.text === "string"
          ? state.table[rowIndex]![columnIndex]!.text
          : cell.text,
      })));
    }
  }
  return next;
}

export function applyPresentationLiveEditContent<T extends PresentationLiveEditSlide>(
  slides: T[],
  content: string,
): T[] | null {
  const state = parseState(content);
  if (!state || state.slides.length !== slides.length) return null;
  const stateById = new Map(state.slides.map((slide) => [slide.id, slide]));
  if (slides.some((slide) => !stateById.has(slide.id))) return null;
  return slides.map((slide) => {
    const slideState = stateById.get(slide.id)!;
    const shapeStateById = new Map(slideState.shapes.map((shape) => [shape.id, shape]));
    const next = {
      ...slide,
      ...(slide.notesPart && typeof slideState.notes === "string" ? { notes: slideState.notes } : {}),
      shapes: slide.shapes.map((shape) => {
        const nextShapeState = shapeStateById.get(shape.id);
        return nextShapeState ? mergeShape(shape, nextShapeState) : shape;
      }),
    };
    return next as T;
  });
}

function requestedPercent(request: string, fallback: number): number {
  const match = request.match(/(\d+(?:\.\d+)?)\s*%/);
  return match ? clamp(match[1], 0, 90, fallback) : fallback;
}

export function localPresentationLiveEditContent(userRequest: string, currentContent: string): string | null {
  const state = parseState(currentContent);
  if (!state) return null;
  const request = userRequest.toLowerCase();
  if (/replace|regenerate|redraw|recreate|换图|替换图片|重新生成|重绘|改图中文字|修改图中文字/.test(request)) {
    return null;
  }
  const slide = state.slides.find((candidate) => candidate.slideNumber === state.activeSlide);
  const shape = slide?.shapes.find((candidate) => candidate.id === state.targetShapeId)
    || slide?.shapes.find((candidate) => candidate.editable && candidate.fullSlide)
    || slide?.shapes.find((candidate) => candidate.editable);
  if (!shape) return null;
  let changed = false;
  const set = <K extends keyof PresentationLiveShapeState>(key: K, value: PresentationLiveShapeState[K]) => {
    if (JSON.stringify(shape[key]) === JSON.stringify(value)) return;
    shape[key] = value;
    changed = true;
  };
  const amount = requestedPercent(request, 5);

  const marginIntent = /margin|留白|边距/.test(request);
  if (marginIntent) {
    const margin = clamp(requestedPercent(request, 5), 0, 45, 5);
    set("x", margin);
    set("y", margin);
    set("w", 100 - margin * 2);
    set("h", 100 - margin * 2);
  } else if (/full.?bleed|fill.*(?:slide|page)|fit.*(?:slide|page)|铺满|填满|全屏|充满整页/.test(request)) {
    set("x", 0);
    set("y", 0);
    set("w", 100);
    set("h", 100);
  }

  if (!marginIntent && /shrink|smaller|缩小/.test(request)) {
    const factor = 1 - amount / 100;
    const w = Math.max(1, shape.w * factor);
    const h = Math.max(0.5, shape.h * factor);
    set("w", Number(w.toFixed(2)));
    set("h", Number(h.toFixed(2)));
    set("x", Number(((100 - w) / 2).toFixed(2)));
    set("y", Number(((100 - h) / 2).toFixed(2)));
  } else if (!marginIntent && /enlarge|bigger|放大/.test(request)) {
    const factor = 1 + amount / 100;
    const w = Math.min(100, shape.w * factor);
    const h = Math.min(100, shape.h * factor);
    set("w", Number(w.toFixed(2)));
    set("h", Number(h.toFixed(2)));
    set("x", Number(((100 - w) / 2).toFixed(2)));
    set("y", Number(((100 - h) / 2).toFixed(2)));
  }

  if (/center|居中/.test(request)) {
    set("x", Number(((100 - shape.w) / 2).toFixed(2)));
    set("y", Number(((100 - shape.h) / 2).toFixed(2)));
  }
  if (/move.*up|上移|向上移/.test(request)) set("y", Math.max(0, shape.y - amount));
  if (/move.*down|下移|向下移/.test(request)) set("y", Math.min(100 - shape.h, shape.y + amount));
  if (/move.*left|左移|向左移/.test(request)) set("x", Math.max(0, shape.x - amount));
  if (/move.*right|右移|向右移/.test(request)) set("x", Math.min(100 - shape.w, shape.x + amount));

  if (shape.image) {
    const crop = { ...shape.image.crop };
    if (/reset.*crop|remove.*crop|重置裁切|取消裁切|恢复裁切/.test(request)) {
      shape.image.crop = { l: 0, t: 0, r: 0, b: 0 };
      changed = true;
    } else if (/crop|裁切|裁剪|裁掉/.test(request)) {
      if (/top|顶部|上方/.test(request)) crop.t = amount;
      if (/bottom|底部|下方/.test(request)) crop.b = amount;
      if (/left|左侧|左边/.test(request)) crop.l = amount;
      if (/right|右侧|右边/.test(request)) crop.r = amount;
      if (JSON.stringify(crop) !== JSON.stringify(shape.image.crop)) {
        shape.image.crop = crop;
        changed = true;
      }
    }
  }
  if (/rotate.*left|counterclockwise|逆时针|向左旋转/.test(request)) set("rotation", shape.rotation - 90);
  else if (/rotate|clockwise|旋转|顺时针/.test(request)) set("rotation", shape.rotation + 90);
  if (/flip.*horizontal|horizontal.*flip|水平翻转/.test(request)) set("flipH", !shape.flipH);
  if (/flip.*vertical|vertical.*flip|垂直翻转/.test(request)) set("flipV", !shape.flipV);

  return changed ? JSON.stringify(state, null, 2) : null;
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
