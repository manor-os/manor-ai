/** Match Office metrics with bundled fonts when proprietary faces are absent. */
export function officeCompatibleFontFamily(typeface: string | undefined): string | undefined {
  if (!typeface) return undefined;
  const normalized = typeface.trim().toLowerCase();
  if (normalized === "calibri" || normalized === "calibri light") return "Carlito";
  return typeface;
}
