type TabEvent = {
  key: string;
  shiftKey: boolean;
  nativeEvent: { isComposing?: boolean };
  preventDefault(): void;
};

/** Tab on an empty field takes the suggested text; with anything typed, Tab moves on as usual. */
export function fillSuggestionOnTab(
  event: TabEvent,
  value: string,
  suggestion: string | undefined,
  fill: (text: string) => void,
) {
  if (event.key !== "Tab" || event.shiftKey || event.nativeEvent.isComposing) return;
  if (value || !suggestion?.trim()) return;
  event.preventDefault();
  fill(suggestion);
}
