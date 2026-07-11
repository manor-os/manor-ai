import PptxGenJS from "pptxgenjs";

const PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
const DEFAULT_LAYOUT_WIDTH = 13.333;

export interface PresentationInlineRun {
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
}

export interface PresentationTextRun extends PresentationInlineRun {
  align?: string;
  bullet?: string;
  indent?: number;
  lineSpacing?: number;
  spaceBefore?: number;
  spaceAfter?: number;
  runs?: PresentationInlineRun[];
}

export interface PresentationTableCell {
  text: string;
  bold?: boolean;
  color?: string;
  fill?: string;
  gridSpan?: number;
  vMerge?: boolean;
}

export interface PresentationShape {
  id: string;
  type?: "shape" | "table" | "image";
  x: number;
  y: number;
  w: number;
  h: number;
  fill?: string;
  gradFill?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  borderRadius?: number;
  opacity?: number;
  rotation?: number;
  stroke?: string;
  strokeWidth?: number;
  presetGeom?: string;
  flipH?: boolean;
  flipV?: boolean;
  shadow?: { blur: number; dist: number; angle: number; color: string; alpha: number };
  imgCrop?: { l: number; t: number; r: number; b: number };
  vAlign?: "top" | "middle" | "bottom";
  padding?: { l: number; t: number; r: number; b: number };
  texts: PresentationTextRun[];
  imgUrl?: string;
  imageFit?: "cover" | "contain";
  tableRows?: PresentationTableCell[][];
  tableColWidths?: number[];
}

export interface PresentationSlide {
  id: string;
  bg?: string;
  bgGrad?: { angle: number; stops: { pos: number; color: string; alpha: number }[] };
  bgImgUrl?: string;
  aspectRatio?: string;
  notes?: string;
  shapes: PresentationShape[];
}

function parseAspectRatio(value?: string): number {
  const [rawWidth, rawHeight] = (value || "16/9").split("/").map((part) => Number(part.trim()));
  const ratio = rawWidth > 0 && rawHeight > 0 ? rawWidth / rawHeight : 16 / 9;
  return Number.isFinite(ratio) && ratio > 0 ? ratio : 16 / 9;
}

function hexColor(value: string | undefined, fallback = "000000"): string {
  const raw = (value || "").trim().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(raw)) return raw.split("").map((char) => char + char).join("").toUpperCase();
  if (/^[0-9a-f]{6}$/i.test(raw)) return raw.toUpperCase();
  return fallback;
}

function isTransparent(value?: string): boolean {
  return !value || value === "transparent" || value === "none";
}

function toDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("Unable to read image"));
    reader.readAsDataURL(blob);
  });
}

async function resolveImageData(url: string, cache: Map<string, string>): Promise<string> {
  if (cache.has(url)) return cache.get(url)!;
  if (/^data:[^;]+;base64,/i.test(url)) {
    cache.set(url, url);
    return url;
  }
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Unable to read presentation image (${response.status})`);
  const data = await toDataUrl(await response.blob());
  cache.set(url, data);
  return data;
}

function encodeSvg(svg: string): string {
  const bytes = new TextEncoder().encode(svg);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return `data:image/svg+xml;base64,${btoa(binary)}`;
}

function gradientSvg(
  gradient: { angle: number; stops: { pos: number; color: string; alpha: number }[] },
  width: number,
  height: number,
): string {
  const angle = Number.isFinite(gradient.angle) ? gradient.angle : 0;
  const radians = ((angle + 90) * Math.PI) / 180;
  const x = Math.cos(radians);
  const y = Math.sin(radians);
  const x1 = 50 - x * 50;
  const y1 = 50 - y * 50;
  const x2 = 50 + x * 50;
  const y2 = 50 + y * 50;
  const stops = gradient.stops
    .map((stop) => `<stop offset="${Math.max(0, Math.min(100, stop.pos))}%" stop-color="${stop.color}" stop-opacity="${Math.max(0, Math.min(1, stop.alpha))}"/>`)
    .join("");
  return encodeSvg(`<svg xmlns="http://www.w3.org/2000/svg" width="${width * 96}" height="${height * 96}" viewBox="0 0 ${width * 96} ${height * 96}"><defs><linearGradient id="g" x1="${x1}%" y1="${y1}%" x2="${x2}%" y2="${y2}%">${stops}</linearGradient></defs><rect width="100%" height="100%" fill="url(#g)"/></svg>`);
}

function shapeName(presentation: PptxGenJS, preset?: string): PptxGenJS.SHAPE_NAME {
  const candidate = preset || "rect";
  const supported = new Set(Object.values(presentation.ShapeType) as string[]);
  return (supported.has(candidate) ? candidate : "rect") as PptxGenJS.SHAPE_NAME;
}

function position(shape: PresentationShape, layoutWidth: number, layoutHeight: number) {
  return {
    x: (shape.x / 100) * layoutWidth,
    y: (shape.y / 100) * layoutHeight,
    w: Math.max(0.01, (shape.w / 100) * layoutWidth),
    h: Math.max(0.01, (shape.h / 100) * layoutHeight),
  };
}

function shadow(shape: PresentationShape): PptxGenJS.ShadowProps | undefined {
  if (!shape.shadow) return undefined;
  return {
    type: "outer",
    color: hexColor(shape.shadow.color),
    opacity: Math.max(0, Math.min(1, shape.shadow.alpha)),
    blur: Math.max(0, Math.min(100, shape.shadow.blur)),
    angle: ((shape.shadow.angle % 360) + 360) % 360,
    offset: Math.max(0, Math.min(200, shape.shadow.dist)),
  };
}

function fill(shape: PresentationShape): PptxGenJS.ShapeFillProps {
  if (isTransparent(shape.fill) && !shape.gradFill?.stops.length) return { type: "none" };
  const color = shape.fill || shape.gradFill?.stops[0]?.color;
  return {
    color: hexColor(color, "FFFFFF"),
    transparency: Math.round((1 - Math.max(0, Math.min(1, shape.opacity ?? 1))) * 100),
  };
}

function line(shape: PresentationShape): PptxGenJS.ShapeLineProps {
  if (!shape.stroke || (shape.strokeWidth ?? 0) <= 0) return { type: "none" };
  return {
    color: hexColor(shape.stroke),
    width: Math.max(0.25, shape.strokeWidth || 1),
    transparency: Math.round((1 - Math.max(0, Math.min(1, shape.opacity ?? 1))) * 100),
  };
}

function textOptions(run: PresentationInlineRun): PptxGenJS.TextPropsOptions {
  return {
    bold: Boolean(run.bold),
    italic: Boolean(run.italic),
    underline: run.underline ? { style: "sng" } : undefined,
    strike: run.strikethrough ? "sngStrike" : undefined,
    fontSize: Math.max(1, run.fontSize || 16),
    color: hexColor(run.color),
    fontFace: run.fontFamily || undefined,
    superscript: (run.baseline || 0) > 0 || undefined,
    subscript: (run.baseline || 0) < 0 || undefined,
    charSpacing: run.spacing,
  };
}

function textRuns(shape: PresentationShape): PptxGenJS.TextProps[] {
  const output: PptxGenJS.TextProps[] = [];
  shape.texts.forEach((paragraph, paragraphIndex) => {
    const sourceRuns = paragraph.runs?.length ? paragraph.runs : [paragraph];
    sourceRuns.forEach((run, runIndex) => {
      const isFirstRun = runIndex === 0;
      const isLastRun = runIndex === sourceRuns.length - 1;
      const isNumbered = paragraph.bullet === "#." || paragraph.bullet === "a." || paragraph.bullet === "i.";
      const bullet = isFirstRun && paragraph.bullet
        ? isNumbered
          ? { type: "number" as const, numberType: paragraph.bullet === "a." ? "alphaLcPeriod" as const : paragraph.bullet === "i." ? "romanLcPeriod" as const : "arabicPeriod" as const, indent: paragraph.indent }
          : { type: "bullet" as const, characterCode: paragraph.bullet.codePointAt(0)?.toString(16).toUpperCase(), indent: paragraph.indent }
        : undefined;
      output.push({
        text: run.text,
        options: {
          ...textOptions({ ...paragraph, ...run }),
          align: (paragraph.align === "center" || paragraph.align === "right" || paragraph.align === "justify") ? paragraph.align : "left",
          bullet,
          breakLine: isLastRun && paragraphIndex < shape.texts.length - 1,
          lineSpacingMultiple: paragraph.lineSpacing,
          paraSpaceBefore: paragraph.spaceBefore,
          paraSpaceAfter: paragraph.spaceAfter,
        },
      });
    });
  });
  return output;
}

async function addShapeToSlide(
  presentation: PptxGenJS,
  slide: PptxGenJS.Slide,
  model: PresentationShape,
  layoutWidth: number,
  layoutHeight: number,
  imageCache: Map<string, string>,
) {
  const rect = position(model, layoutWidth, layoutHeight);
  const common = {
    ...rect,
    rotate: model.rotation || 0,
    flipH: model.flipH,
    flipV: model.flipV,
    shadow: shadow(model),
    objectName: `Manor ${model.id}`,
  };

  if (model.type === "table" && model.tableRows?.length) {
    const rows: PptxGenJS.TableRow[] = model.tableRows.map((row) => row.map((cell) => ({
      text: cell.vMerge ? "" : cell.text,
      options: {
        bold: Boolean(cell.bold),
        color: hexColor(cell.color),
        fill: cell.fill ? { color: hexColor(cell.fill, "FFFFFF") } : undefined,
        colspan: cell.gridSpan,
        margin: 0.04,
        valign: "middle",
      },
    })));
    const totalColWidth = model.tableColWidths?.reduce((sum, width) => sum + width, 0) || 0;
    slide.addTable(rows, {
      ...rect,
      autoPage: false,
      border: { type: "solid", color: "D6D3D1", pt: 0.5 },
      color: "292524",
      fontSize: 12,
      margin: 0.04,
      valign: "middle",
      colW: totalColWidth > 0
        ? model.tableColWidths!.map((width) => (width / totalColWidth) * rect.w)
        : undefined,
      objectName: `Manor ${model.id}`,
    });
    return;
  }

  if (model.imgUrl) {
    const data = await resolveImageData(model.imgUrl, imageCache);
    slide.addImage({
      data,
      ...common,
      sizing: { type: model.imageFit || (model.imgCrop ? "cover" : "contain"), w: rect.w, h: rect.h },
      transparency: Math.round((1 - Math.max(0, Math.min(1, model.opacity ?? 1))) * 100),
      rounding: model.presetGeom === "ellipse" || model.presetGeom === "oval",
    });
  }

  if (model.texts.length > 0) {
    slide.addText(textRuns(model), {
      ...common,
      shape: shapeName(presentation, model.presetGeom),
      fill: model.imgUrl ? { type: "none" } : fill(model),
      line: model.imgUrl ? { type: "none" } : line(model),
      fit: "shrink",
      wrap: true,
      valign: model.vAlign || "top",
      margin: model.padding ? [model.padding.t, model.padding.r, model.padding.b, model.padding.l] : 0,
    });
    return;
  }

  if (!model.imgUrl) {
    slide.addShape(shapeName(presentation, model.presetGeom), {
      ...common,
      fill: fill(model),
      line: line(model),
      rectRadius: model.borderRadius ? Math.max(0, Math.min(1, model.borderRadius / 100)) : undefined,
    });
  }
}

export async function buildPresentationBlob(slides: PresentationSlide[], title = "Presentation"): Promise<Blob> {
  const presentation = new PptxGenJS();
  const aspectRatio = parseAspectRatio(slides[0]?.aspectRatio);
  const layoutWidth = DEFAULT_LAYOUT_WIDTH;
  const layoutHeight = layoutWidth / aspectRatio;
  presentation.defineLayout({ name: "MANOR_EDITOR", width: layoutWidth, height: layoutHeight });
  presentation.layout = "MANOR_EDITOR";
  presentation.author = "Manor AI";
  presentation.company = "Manor AI";
  presentation.subject = "Edited in Manor AI";
  presentation.title = title.replace(/\.pptx?$/i, "");
  presentation.revision = "1";

  const imageCache = new Map<string, string>();
  for (const model of slides) {
    const slide = presentation.addSlide();
    if (model.notes?.trim()) slide.addNotes(model.notes);
    if (model.bgImgUrl) {
      slide.background = { data: await resolveImageData(model.bgImgUrl, imageCache) };
    } else if (model.bg) {
      slide.background = { color: hexColor(model.bg, "FFFFFF") };
    } else {
      slide.background = { color: "FFFFFF" };
    }
    if (model.bgGrad?.stops.length) {
      slide.addImage({ data: gradientSvg(model.bgGrad, layoutWidth, layoutHeight), x: 0, y: 0, w: layoutWidth, h: layoutHeight });
    }
    for (const modelShape of model.shapes) {
      await addShapeToSlide(presentation, slide, modelShape, layoutWidth, layoutHeight, imageCache);
    }
  }

  const output = await presentation.write({ outputType: "blob", compression: true });
  if (output instanceof Blob) {
    return output.type === PPTX_MIME
      ? output
      : new Blob([await output.arrayBuffer()], { type: PPTX_MIME });
  }
  if (typeof output === "string") return new Blob([output], { type: PPTX_MIME });
  if (output instanceof ArrayBuffer) return new Blob([output], { type: PPTX_MIME });
  return new Blob([new Uint8Array(output).buffer], { type: PPTX_MIME });
}

export async function buildPresentationFile(slides: PresentationSlide[], fileName: string): Promise<File> {
  const safeName = fileName.toLowerCase().endsWith(".pptx") ? fileName : `${fileName.replace(/\.ppt$/i, "")}.pptx`;
  const blob = await buildPresentationBlob(slides, safeName);
  return new File([blob], safeName, { type: PPTX_MIME, lastModified: Date.now() });
}
