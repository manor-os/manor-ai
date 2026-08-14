const LEGACY_OR_PROPRIETARY_OFFICE_EXTENSIONS = new Set(["doc", "xls", "ppt", "wps", "et", "dps"]);

export function isLegacyOfficeFile(name?: string | null): boolean {
  const extension = (name || "").split(".").pop()?.toLowerCase() || "";
  return LEGACY_OR_PROPRIETARY_OFFICE_EXTENSIONS.has(extension);
}
