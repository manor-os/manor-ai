export const EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX = "__MANOR_SPREADSHEET_EDITOR_V1__\n";

export function serializeEditorLiveSpreadsheetPayload(
  data: unknown[][],
  charts: unknown[],
  styles: Record<string, unknown>,
): string {
  return `${EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX}${JSON.stringify({ data, charts, styles })}`;
}
