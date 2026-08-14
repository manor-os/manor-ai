export function normalizeImageQuarterTurn(rotation: number): 0 | 90 | 180 | 270 {
  const normalized = ((Math.round(rotation / 90) * 90) % 360 + 360) % 360;
  return normalized as 0 | 90 | 180 | 270;
}

export function imageOutputSize(
  sourceWidth: number,
  sourceHeight: number,
  rotation: number,
): { width: number; height: number } {
  const normalized = normalizeImageQuarterTurn(rotation);
  return normalized === 90 || normalized === 270
    ? { width: sourceHeight, height: sourceWidth }
    : { width: sourceWidth, height: sourceHeight };
}

export function imageCanvasPoint(
  clientX: number,
  clientY: number,
  bounds: { left: number; top: number; width: number; height: number },
  output: { width: number; height: number },
): { x: number; y: number } {
  if (bounds.width <= 0 || bounds.height <= 0) return { x: 0, y: 0 };
  return {
    x: ((clientX - bounds.left) / bounds.width) * output.width,
    y: ((clientY - bounds.top) / bounds.height) * output.height,
  };
}
