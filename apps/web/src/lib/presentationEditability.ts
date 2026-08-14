export interface PresentationEditabilityShape {
  id: string;
  type?: string;
  x: number;
  y: number;
  w: number;
  h: number;
  imgUrl?: string;
  texts?: Array<{ text?: string }>;
  tableRows?: unknown[][];
  source?: { editable: boolean };
}

export interface PresentationSlideEditability {
  editableShapeCount: number;
  lockedShapeCount: number;
  editableTextCount: number;
  editableTableCount: number;
  editableImageCount: number;
  flattenedImageShapeId: string | null;
}

/**
 * Summarize which parts of a parsed PPTX slide can be safely patched back
 * into the original OOXML package. A nearly full-slide picture is called out
 * separately because it looks like editable slide content while behaving as
 * one flattened image.
 */
export function presentationSlideEditability(
  shapes: PresentationEditabilityShape[],
): PresentationSlideEditability {
  const editableShapes = shapes.filter((shape) => !shape.source || shape.source.editable);
  const lockedShapeCount = shapes.filter((shape) => shape.source && !shape.source.editable).length;
  const editableTextCount = editableShapes.filter((shape) =>
    shape.texts?.some((paragraph) => Boolean(paragraph.text?.trim())),
  ).length;
  const editableTableCount = editableShapes.filter((shape) => Boolean(shape.tableRows?.length)).length;
  const editableImageCount = editableShapes.filter((shape) => shape.type === "image" || Boolean(shape.imgUrl)).length;

  const soleEditableShape = editableShapes.length === 1 ? editableShapes[0] : undefined;
  const isNearlyFullSlideImage = Boolean(
    soleEditableShape
    && (soleEditableShape.type === "image" || soleEditableShape.imgUrl)
    && !soleEditableShape.texts?.some((paragraph) => Boolean(paragraph.text?.trim()))
    && !soleEditableShape.tableRows?.length
    && soleEditableShape.x <= 2
    && soleEditableShape.y <= 2
    && soleEditableShape.x + soleEditableShape.w >= 98
    && soleEditableShape.y + soleEditableShape.h >= 98,
  );

  return {
    editableShapeCount: editableShapes.length,
    lockedShapeCount,
    editableTextCount,
    editableTableCount,
    editableImageCount,
    flattenedImageShapeId: isNearlyFullSlideImage ? soleEditableShape?.id || null : null,
  };
}
