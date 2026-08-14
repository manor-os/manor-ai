export interface PdfViewportLike {
  width: number;
  height: number;
  convertToPdfPoint(x: number, y: number): [number, number];
}

export interface NormalizedPdfRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PdfOverlayPlacement {
  origin: { x: number; y: number };
  topLeft: { x: number; y: number };
  bottomRight: { x: number; y: number };
  xAxis: { x: number; y: number };
  yAxis: { x: number; y: number };
  width: number;
  height: number;
  rotation: number;
}

function point(viewport: PdfViewportLike, x: number, y: number) {
  const [pdfX, pdfY] = viewport.convertToPdfPoint(x, y);
  return { x: pdfX, y: pdfY };
}

function normalize(vector: { x: number; y: number }) {
  const length = Math.max(1e-9, Math.hypot(vector.x, vector.y));
  return { x: vector.x / length, y: vector.y / length, length };
}

/**
 * Convert a top-left, normalized PDF.js overlay rectangle into the original
 * page user space. This respects CropBox offsets and /Rotate values because
 * PDF.js owns the exact viewport transform.
 */
export function pdfOverlayPlacement(
  viewport: PdfViewportLike,
  rect: NormalizedPdfRect,
): PdfOverlayPlacement {
  const left = rect.x * viewport.width;
  const top = rect.y * viewport.height;
  const right = (rect.x + rect.width) * viewport.width;
  const bottom = (rect.y + rect.height) * viewport.height;
  const origin = point(viewport, left, bottom);
  const bottomRight = point(viewport, right, bottom);
  const topLeft = point(viewport, left, top);
  const horizontal = normalize({ x: bottomRight.x - origin.x, y: bottomRight.y - origin.y });
  const vertical = normalize({ x: topLeft.x - origin.x, y: topLeft.y - origin.y });
  return {
    origin,
    topLeft,
    bottomRight,
    xAxis: { x: horizontal.x, y: horizontal.y },
    yAxis: { x: vertical.x, y: vertical.y },
    width: horizontal.length,
    height: vertical.length,
    rotation: Math.atan2(horizontal.y, horizontal.x) * (180 / Math.PI),
  };
}

export function pdfOverlayPoint(
  viewport: PdfViewportLike,
  normalizedPoint: { x: number; y: number },
): { x: number; y: number } {
  return point(viewport, normalizedPoint.x * viewport.width, normalizedPoint.y * viewport.height);
}

export function offsetPdfPlacement(
  placement: PdfOverlayPlacement,
  alongX: number,
  alongY: number,
): { x: number; y: number } {
  return {
    x: placement.origin.x + placement.xAxis.x * alongX + placement.yAxis.x * alongY,
    y: placement.origin.y + placement.xAxis.y * alongX + placement.yAxis.y * alongY,
  };
}
