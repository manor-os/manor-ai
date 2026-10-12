export const SIGNATURE_FIELD_TYPES = ["signature", "name", "date", "text", "checkbox"] as const;

export type SignatureFieldType = typeof SIGNATURE_FIELD_TYPES[number];

/** Coordinates are relative to the visible, rotated PDF page, from its top left. */
export interface DocumentSignatureField {
  id: string;
  type: SignatureFieldType;
  label: string;
  required: boolean;
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

type Placement = Pick<DocumentSignatureField, "page" | "x" | "y" | "width" | "height">;

export function parseSignatureFieldPercentage(value: string, minimum: number): number | undefined {
  if (!value.trim()) return undefined;
  const percentage = Number(value);
  if (!Number.isFinite(percentage)) return undefined;
  return Math.max(minimum, Math.min(1, percentage / 100));
}

export function normalizeSignatureFields(request: { fields?: DocumentSignatureField[]; placement: Placement }): DocumentSignatureField[] {
  if (request.fields?.length) return request.fields;
  return [{ ...request.placement, id: "signature", type: "signature", label: "", required: true }];
}

export function clampSignatureField(field: DocumentSignatureField): DocumentSignatureField {
  const width = Math.max(0.02, Math.min(1, field.width));
  const height = Math.max(0.012, Math.min(1, field.height));
  return {
    ...field,
    width,
    height,
    x: Math.max(0, Math.min(1 - width, field.x)),
    y: Math.max(0, Math.min(1 - height, field.y)),
  };
}

export function createSignatureField(type: SignatureFieldType, page: number, label: string, x = 0.35, y = 0.4): DocumentSignatureField {
  const width = type === "checkbox" ? 0.035 : type === "date" ? 0.22 : 0.30;
  const height = type === "signature" ? 0.07 : type === "checkbox" ? 0.026 : 0.035;
  return clampSignatureField({
    id: crypto.randomUUID(), type, label, required: true, page, x, y, width, height,
  });
}
