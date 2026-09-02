import type { SpreadsheetImageModel } from "../lib/spreadsheetOoxml";

function axisOffset(sizes: number[], index: number, fallback: number): number {
  let offset = 0;
  for (let current = 0; current < index; current += 1) offset += sizes[current] || fallback;
  return offset;
}

export default function SpreadsheetImageLayer({
  images,
  columnWidths,
  rowHeights,
  rowHeaderWidth = 48,
  columnHeaderHeight = 31,
}: {
  images: SpreadsheetImageModel[];
  columnWidths: number[];
  rowHeights: number[];
  rowHeaderWidth?: number;
  columnHeaderHeight?: number;
}) {
  if (images.length === 0) return null;
  return (
    <div
      className="spreadsheet-native-image-layer"
      aria-label="Native worksheet pictures"
      style={{ position: "absolute", inset: 0, pointerEvents: "none", zIndex: 3 }}
    >
      {images.map((image) => {
        const left = rowHeaderWidth + axisOffset(columnWidths, image.anchor.c, 112) + image.offsetX;
        const top = columnHeaderHeight + axisOffset(rowHeights, image.anchor.r, 32) + image.offsetY;
        const right = image.end
          ? rowHeaderWidth + axisOffset(columnWidths, image.end.c, 112) + (image.endOffsetX || 0)
          : undefined;
        const bottom = image.end
          ? columnHeaderHeight + axisOffset(rowHeights, image.end.r, 32) + (image.endOffsetY || 0)
          : undefined;
        const width = image.width || (right != null ? Math.max(1, right - left) : undefined);
        const height = image.height || (bottom != null ? Math.max(1, bottom - top) : undefined);
        return (
          <img
            key={image.id}
            src={image.src}
            alt={image.altText}
            title={image.name}
            draggable={false}
            data-spreadsheet-native-picture="true"
            style={{
              position: "absolute",
              left,
              top,
              width,
              height,
              maxWidth: "none",
              objectFit: "fill",
              display: "block",
            }}
          />
        );
      })}
    </div>
  );
}
