export function taskBoardQueryEnabled(view: string, boardMode: string): boolean {
  return view === "board" && boardMode === "kanban";
}

export function taskListQueryEnabled(view: string, boardMode: string): boolean {
  return view === "calendar" || (view === "board" && boardMode === "list");
}

export function taskRowsForActiveView<T>(view: string, boardMode: string, taskList: T[] | undefined, boardRows: T[]): T[] {
  return taskListQueryEnabled(view, boardMode) ? taskList ?? [] : boardRows;
}

export function preserveSelectedOption<T extends { key: string }>(options: T[], selectedKey: string, cachedOption?: T): T[] {
  if (selectedKey === "all" || !cachedOption || options.some((option) => option.key === selectedKey)) return options;
  return [options[0], cachedOption, ...options.slice(1)];
}
