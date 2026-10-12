import {
  presentationInverseTransform,
  type PresentationGroupTransform,
} from "./presentationOoxml";
import {
  rebasePresentationTextSourceMap,
  type PresentationTextSourceMap,
} from "./presentationTextEdits";

/**
 * Pure state helpers for the native PowerPoint editor. They live outside
 * DocEditor.tsx so the save/undo/editability invariants can be unit tested.
 */

export interface PresentationStateSource {
  part: string;
  kind: string;
  objectId: string;
  cloneOfObjectId?: string;
  editable: boolean;
  groupTransform?: PresentationGroupTransform;
}

export interface PresentationStateText {
  text: string;
  sourceMap?: PresentationTextSourceMap;
}

export interface PresentationStateShape {
  id: string;
  type?: string;
  x: number;
  y: number;
  w: number;
  h: number;
  texts: PresentationStateText[];
  tableRows?: PresentationStateText[][];
  source?: PresentationStateSource;
}

export interface PresentationStateSlide<Shape extends PresentationStateShape = PresentationStateShape> {
  id: string;
  shapes: Shape[];
  sourcePart?: string;
  notesPart?: string;
  aspectRatio?: string;
  heightPoints?: number;
}

export interface PresentationSlideSize {
  width: number;
  height: number;
}

export const DEFAULT_PRESENTATION_SLIDE_SIZE: PresentationSlideSize = Object.freeze({
  width: 12192000,
  height: 6858000,
});

const EMU_PER_POINT = 12700;

/**
 * Slide size in EMU for one slide. Parsed slides carry the exact package size
 * as `aspectRatio = "<cx>/<cy>"`; editor-created slides may only carry a ratio
 * such as "16/9", in which case `heightPoints` (or the default height) is used.
 */
export function presentationSlideSize(
  slide?: { aspectRatio?: string; heightPoints?: number } | null,
): PresentationSlideSize {
  const [rawWidth, rawHeight] = (slide?.aspectRatio || "").split("/").map((part) => Number(part.trim()));
  const hasRatio = Number.isFinite(rawWidth) && Number.isFinite(rawHeight) && rawWidth > 0 && rawHeight > 0;
  if (hasRatio && rawWidth >= 1000 && rawHeight >= 1000) return { width: rawWidth, height: rawHeight };
  const height = slide?.heightPoints && slide.heightPoints > 0
    ? slide.heightPoints * EMU_PER_POINT
    : DEFAULT_PRESENTATION_SLIDE_SIZE.height;
  const ratio = hasRatio ? rawWidth / rawHeight : DEFAULT_PRESENTATION_SLIDE_SIZE.width / DEFAULT_PRESENTATION_SLIDE_SIZE.height;
  return { width: height * ratio, height };
}

/**
 * Convert a slide-space delta (percent of the slide) into the shape's own
 * coordinate space. Group members store child coordinates, so a scaled,
 * rotated or flipped group needs the inverse group transform.
 */
export function presentationShapeLocalDelta(
  shape: { source?: { groupTransform?: PresentationGroupTransform } },
  dx: number,
  dy: number,
  size: PresentationSlideSize,
): { dx: number; dy: number } {
  const groupTransform = shape.source?.groupTransform;
  if (!groupTransform) return { dx, dy };
  const inverse = presentationInverseTransform(groupTransform);
  if (!inverse) return { dx: 0, dy: 0 };
  const deltaX = (dx / 100) * size.width;
  const deltaY = (dy / 100) * size.height;
  return {
    dx: ((inverse.a * deltaX + inverse.c * deltaY) / size.width) * 100,
    dy: ((inverse.b * deltaX + inverse.d * deltaY) / size.height) * 100,
  };
}

/**
 * Move a shape by a slide-space delta (percent). Top-level shapes stay on the
 * slide; group members keep their child coordinates unclamped (the group
 * transform decides where they land), matching mouse dragging.
 */
export function presentationMovedShape<Shape extends PresentationStateShape>(
  shape: Shape,
  origin: { x: number; y: number },
  dx: number,
  dy: number,
  size: PresentationSlideSize,
): Shape {
  const local = presentationShapeLocalDelta(shape, dx, dy, size);
  const x = origin.x + local.dx;
  const y = origin.y + local.dy;
  if (shape.source?.groupTransform) return { ...shape, x, y };
  return {
    ...shape,
    x: Math.max(0, Math.min(100 - shape.w, x)),
    y: Math.max(0, Math.min(100 - shape.h, y)),
  };
}

/**
 * Apply a property edit to a shape, bounding only the geometry fields the edit
 * actually sets. Untouched geometry keeps its exact source value, so restyling
 * a zero-height connector or a full-bleed/off-slide object never reshapes or
 * pulls it inward. Group members stay in child coordinates (no slide clamp).
 */
export function presentationShapeWithProps<Shape extends PresentationStateShape & { presetGeom?: string }>(
  shape: Shape,
  update: Partial<Shape>,
): Shape {
  const merged = { ...shape, ...update };
  const isLine = merged.presetGeom === "line" || merged.source?.kind === "cxnSp";
  const minExtent = isLine ? 0 : 1;
  const inGroup = Boolean(merged.source?.groupTransform);
  const sets = (key: "x" | "y" | "w" | "h") => key in update && Number.isFinite(update[key] as number);
  if (sets("w")) merged.w = Math.max(minExtent, inGroup ? merged.w : Math.min(100, merged.w));
  else merged.w = shape.w;
  if (sets("h")) merged.h = Math.max(minExtent, inGroup ? merged.h : Math.min(100, merged.h));
  else merged.h = shape.h;
  if (!inGroup) {
    if (sets("x")) merged.x = Math.max(0, Math.min(Math.max(0, 100 - merged.w), merged.x));
    else merged.x = shape.x;
    if (sets("y")) merged.y = Math.max(0, Math.min(Math.max(0, 100 - merged.h), merged.y));
    else merged.y = shape.y;
  }
  return merged;
}

// ---------------------------------------------------------------------------
// Text input sanitising
// ---------------------------------------------------------------------------

// XML 1.0 forbids C0 controls other than TAB/LF/CR, plus U+FFFE/U+FFFF and
// unpaired surrogates. PowerPoint/Word clipboards use U+000B (vertical tab)
// for a soft line break, which the editor models as "\n" (<a:br/>).
const PRESENTATION_SOFT_BREAKS = /[\u000B\u000C\u2028\u2029]/g;
const PRESENTATION_INVALID_XML_CHARACTERS = /[\u0000-\u0008\u000E-\u001F\uFFFE\uFFFF]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;

export function sanitizePresentationInputText(value: string): string {
  return value
    .replace(/\r\n?/g, "\n")
    .replace(PRESENTATION_SOFT_BREAKS, "\n")
    .replace(PRESENTATION_INVALID_XML_CHARACTERS, "");
}

/**
 * A contenteditable whose content ends in a line break keeps a placeholder
 * <br> after it so the caret has a line to sit on; `innerText` reports that
 * placeholder as one extra trailing "\n". It is not user text and must not be
 * saved as a trailing <a:br/>.
 */
export function withoutEditablePlaceholderBreak(text: string, endsWithBreakElement: boolean): string {
  return endsWithBreakElement && text.endsWith("\n") ? text.slice(0, -1) : text;
}

// ---------------------------------------------------------------------------
// Post-save identity merge
// ---------------------------------------------------------------------------

const PRESENTATION_GEOMETRY_KEYS = ["x", "y", "w", "h", "rotation", "flipH", "flipV"] as const;

function presentationSavedGeometry(shape: PresentationStateShape): Partial<PresentationStateShape> {
  const saved = shape as PresentationStateShape & Record<string, unknown>;
  const geometry: Record<string, unknown> = {};
  for (const key of PRESENTATION_GEOMETRY_KEYS) {
    if (key in saved) geometry[key] = saved[key];
  }
  return geometry as Partial<PresentationStateShape>;
}

/**
 * Carry the identity a save assigned (slide parts, object ids, media parts,
 * rebased text provenance) onto `currentSlides` without touching the
 * content the user sees. `savingSlides` is what was sent to the patcher;
 * `savedSlides` is what the patcher returned and is now the baseline.
 * Unchanged slides/shapes keep object identity where possible.
 */
export function mergePresentationSavedIdentity<Slide extends PresentationStateSlide>(
  currentSlides: Slide[],
  savedSlides: PresentationStateSlide[],
  savingSlides: PresentationStateSlide[],
): Slide[] {
  const savedSlideById = new Map(savedSlides.map((slide) => [slide.id, slide]));
  const savingSlideById = new Map(savingSlides.map((slide) => [slide.id, slide]));
  return currentSlides.map((slide) => {
    const savedSlide = savedSlideById.get(slide.id);
    if (!savedSlide) return slide;
    const savedShapeById = new Map(savedSlide.shapes.map((shape) => [shape.id, shape]));
    const savingShapeById = new Map(
      (savingSlideById.get(slide.id)?.shapes || []).map((shape) => [shape.id, shape]),
    );
    return {
      ...slide,
      sourcePart: savedSlide.sourcePart || slide.sourcePart,
      notesPart: savedSlide.notesPart || slide.notesPart,
      shapes: slide.shapes.map((shape) => {
        const savedShape = savedShapeById.get(shape.id);
        if (!savedShape) return shape;
        const savingShape = savingShapeById.get(shape.id);
        const texts = shape.texts.map((paragraph, index) => {
          const savedParagraph = savedShape.texts[index];
          if (!savedParagraph) return paragraph;
          if (savedParagraph.text !== paragraph.text) {
            return {
              ...paragraph,
              sourceMap: rebasePresentationTextSourceMap(
                savedParagraph.text,
                paragraph.text,
                savingShape?.texts[index]?.sourceMap,
                paragraph.sourceMap,
              ),
            };
          }
          const { sourceMap: _savedEditProvenance, ...savedParagraphState } = paragraph;
          return savedParagraphState;
        });
        const tableRows = shape.tableRows?.map((row, rowIndex) => row.map((cell, cellIndex) => {
          const savedCell = savedShape.tableRows?.[rowIndex]?.[cellIndex];
          if (!savedCell) return cell;
          if (savedCell.text !== cell.text) {
            return {
              ...cell,
              sourceMap: rebasePresentationTextSourceMap(
                savedCell.text,
                cell.text,
                savingShape?.tableRows?.[rowIndex]?.[cellIndex]?.sourceMap,
                cell.sourceMap,
              ),
            };
          }
          const { sourceMap: _savedEditProvenance, ...savedCellState } = cell;
          return savedCellState;
        }));
        // A group member the save had to regenerate as a top-level object
        // now lives in slide coordinates; take the saved geometry with the
        // new source so the editor does not draw it with group-local values.
        const flattened = Boolean(shape.source?.groupTransform) && !savedShape.source?.groupTransform;
        const geometry = flattened ? presentationSavedGeometry(savedShape) : {};
        return {
          ...shape,
          ...geometry,
          texts,
          ...(tableRows ? { tableRows } : {}),
          ...(savedShape.source ? { source: structuredClone(savedShape.source) } : {}),
        };
      }),
    };
  });
}

/**
 * Rebase the provenance of an in-progress text edit (inline text box or
 * table cell) that was opened against the previous baseline.
 */
export function rebasePresentationEditSourceMap(
  savedSlides: PresentationStateSlide[],
  savingSlides: PresentationStateSlide[],
  shapeId: string,
  locator: { textIdx: number } | { rowIdx: number; cellIdx: number },
  editedText: string,
  editedSourceMap: PresentationTextSourceMap,
): PresentationTextSourceMap {
  const find = (slides: PresentationStateSlide[]) => {
    for (const slide of slides) {
      const shape = slide.shapes.find((candidate) => candidate.id === shapeId);
      if (!shape) continue;
      return "textIdx" in locator
        ? shape.texts[locator.textIdx]
        : shape.tableRows?.[locator.rowIdx]?.[locator.cellIdx];
    }
    return undefined;
  };
  const saved = find(savedSlides);
  if (!saved) return editedSourceMap;
  const saving = find(savingSlides);
  return rebasePresentationTextSourceMap(saved.text, editedText, saving?.sourceMap, editedSourceMap);
}

// ---------------------------------------------------------------------------
// Stale source demotion
// ---------------------------------------------------------------------------

function sourceKey(source: PresentationStateSource): string {
  return `${source.part}:${source.kind}:${source.objectId}`;
}

/**
 * Converts a shape whose OOXML object no longer exists into a new, source-less
 * object. Returns null when the object cannot be represented without its
 * source (for example an unrendered chart), in which case it is dropped.
 */
export type PresentationSourceDemotion<Slide extends PresentationStateSlide> = (
  shape: Slide["shapes"][number],
  slide: Slide,
) => Slide["shapes"][number] | null;

export function presentationShapeSourceIsStale(
  shape: PresentationStateShape,
  baselineSlide: PresentationStateSlide | undefined,
): boolean {
  const source = shape.source;
  if (!source) return false;
  if (!baselineSlide) return true;
  if (source.editable) {
    if (source.cloneOfObjectId) {
      return !baselineSlide.shapes.some((candidate) => (
        candidate.source?.part === source.part
        && candidate.source.kind === source.kind
        && candidate.source.objectId === source.cloneOfObjectId
        && candidate.source.part === baselineSlide.sourcePart
      ));
    }
    const key = sourceKey(source);
    return !baselineSlide.shapes.some((candidate) => (
      candidate.source?.editable && sourceKey(candidate.source) === key
    ));
  }
  // Locked objects on the slide itself are regenerated by the patcher when
  // they are missing from the baseline; inherited layout/master objects are
  // compared as a set. Neither makes a save throw, so they are left alone.
  return false;
}

/**
 * After a save rewrote the package, objects restored by undo/redo (or kept in
 * the history) may point at OOXML objects or slide parts that the save
 * removed. The patcher rejects such shapes forever ("This object no longer
 * matches its source slide"), so they become new objects instead.
 *
 * - Slides missing from the baseline are treated like a duplicated slide:
 *   their own objects become source-less copies and inherited layout/master
 *   decorations are dropped (the slide layout draws them).
 * - On slides that still exist, only editable objects whose source is gone
 *   are demoted.
 *
 * Returns the original array when nothing changed.
 */
export function demoteStalePresentationSources<Slide extends PresentationStateSlide>(
  slides: Slide[],
  baselineSlides: PresentationStateSlide[] | null | undefined,
  demote: PresentationSourceDemotion<Slide>,
): Slide[] {
  if (!baselineSlides) return slides;
  const baselineById = new Map(baselineSlides.map((slide) => [slide.id, slide]));
  const baselineNotesParts = new Set(baselineSlides.flatMap((slide) => slide.notesPart ? [slide.notesPart] : []));
  let changed = false;
  const next = slides.map((slide) => {
    const baselineSlide = baselineById.get(slide.id);
    let slideChanged = false;
    const shapes: Slide["shapes"] = [];
    for (const shape of slide.shapes) {
      if (!baselineSlide && shape.source && shape.source.part !== slide.sourcePart) {
        // Inherited layout/master decoration of a slide that no longer exists.
        slideChanged = true;
        continue;
      }
      if (!presentationShapeSourceIsStale(shape, baselineSlide)) {
        shapes.push(shape);
        continue;
      }
      slideChanged = true;
      const adopted = baselineSlide?.shapes.find((candidate) => candidate.id === shape.id)?.source;
      if (adopted && adopted.kind === shape.source!.kind && Boolean(adopted.groupTransform) === Boolean(shape.source!.groupTransform)) {
        // The patcher re-attaches a baseline source by shape id, so keep the
        // two in agreement instead of producing a second copy.
        shapes.push({ ...shape, source: structuredClone(adopted) });
        continue;
      }
      const demoted = demote(shape, slide);
      if (demoted) shapes.push(demoted);
    }
    const staleNotesPart = !baselineSlide && slide.notesPart && !baselineNotesParts.has(slide.notesPart);
    if (!slideChanged && !staleNotesPart) return slide;
    changed = true;
    const nextSlide = { ...slide, shapes };
    if (staleNotesPart) delete nextSlide.notesPart;
    return nextSlide;
  });
  return changed ? next : slides;
}

/**
 * Apply a finished save to the editor's undo/redo history so that an undo
 * after the save neither resurrects stale object references nor loses
 * text provenance.
 */
export function mergePresentationSaveIntoHistory<Slide extends PresentationStateSlide>(
  history: Slide[][],
  savedSlides: PresentationStateSlide[],
  savingSlides: PresentationStateSlide[],
  demote: PresentationSourceDemotion<Slide>,
): Slide[][] {
  return history.map((entry) => demoteStalePresentationSources(
    mergePresentationSavedIdentity(entry, savedSlides, savingSlides),
    savedSlides,
    demote,
  ));
}

// ---------------------------------------------------------------------------
// Save conflicts
// ---------------------------------------------------------------------------

/**
 * True when the server refused a save because the file changed after the
 * editor loaded it (another tab, a collaborator, an agent). Retrying the same
 * snapshot can never succeed.
 */
export function isPresentationStaleSaveError(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const candidate = error as { status?: unknown; code?: unknown; detail?: unknown; message?: unknown };
  if (candidate.status !== 409) return false;
  const detailCode = candidate.detail && typeof candidate.detail === "object"
    ? (candidate.detail as { code?: unknown }).code
    : undefined;
  if (candidate.code === "stale_write_intent" || detailCode === "stale_write_intent") return true;
  const message = typeof candidate.message === "string" ? candidate.message : "";
  return /changed after this editor snapshot|newer save already exists/i.test(message);
}
