export type BlueprintVariableParseResult =
  | { ok: true; value: unknown }
  | { ok: false };

export function formatBlueprintVariableValue(value: unknown): string;
export function parseBlueprintVariableValue(
  input: string,
  defaultValue: unknown,
): BlueprintVariableParseResult;
