export function resolveBlueprintChannelSelections(
  requirements: ReadonlyArray<{
    kind: string;
    requirement_key?: string | null;
    resource_id?: string | null;
    resource_options?: ReadonlyArray<{ id: string }>;
  }>,
  selected?: Record<string, string>,
): Record<string, string>;
