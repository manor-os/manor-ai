export function containedMediaSize(
  container: { width: number; height: number },
  media: { width: number; height: number },
): { width: number; height: number } {
  if (container.width <= 0 || container.height <= 0 || media.width <= 0 || media.height <= 0) {
    return { width: 0, height: 0 };
  }
  const scale = Math.min(container.width / media.width, container.height / media.height);
  return {
    width: media.width * scale,
    height: media.height * scale,
  };
}

export function fittedMediaSize(
  container: { width: number; height: number },
  media: { width: number; height: number },
  fit: "contain" | "cover",
): { width: number; height: number } {
  if (container.width <= 0 || container.height <= 0 || media.width <= 0 || media.height <= 0) {
    return { width: 0, height: 0 };
  }
  const scale = fit === "cover"
    ? Math.max(container.width / media.width, container.height / media.height)
    : Math.min(container.width / media.width, container.height / media.height);
  return {
    width: media.width * scale,
    height: media.height * scale,
  };
}

export function captionAnchorTransform(align: CanvasTextAlign, fontSize: number): string {
  const edgeInset = Math.max(0, fontSize * 0.5);
  if (align === "left" || align === "start") return `translate(${-edgeInset}px, -50%)`;
  if (align === "right" || align === "end") return `translate(calc(-100% + ${edgeInset}px), -50%)`;
  return "translate(-50%, -50%)";
}
