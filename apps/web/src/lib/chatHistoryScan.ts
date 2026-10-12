/** Whether an earlier entry matches, without repeatedly copying history prefixes. */
export function priorMatchingEntries<T>(entries: readonly T[], matches: (entry: T) => boolean): boolean[] {
  let found = false;
  return entries.map((entry) => {
    const prior = found;
    found = found || matches(entry);
    return prior;
  });
}

/** Search from the tail without allocating a reversed copy of the transcript. */
export function latestMessageByRole<T extends { role: string }>(messages: readonly T[], role: string): T | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === role) return messages[index];
  }
  return undefined;
}
