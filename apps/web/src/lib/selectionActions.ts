export const SELECTED_TEXT_TASK_DRAFT_KEY = "manor:selected-text-task-draft:v1";
export const OPEN_FLOATING_CHAT_EVENT = "manor:open-floating-chat";
export const INSERT_CHAT_COMPOSER_EVENT = "manor:insert-chat-composer";
export const ADD_SELECTION_TO_TASK_EVENT = "manor:add-selection-to-task";

export type SelectedTextTaskDraft = {
  title: string;
  description: string;
  sourcePath?: string;
};

export type OpenFloatingChatDetail = {
  prompt: string;
  source?: string;
};

export type InsertChatComposerDetail = OpenFloatingChatDetail;
