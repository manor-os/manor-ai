export type ChatFeedbackValue = "up" | "down";
export type ChatFeedbackMutationStatusValue = "accepted";

export const ChatFeedbackMutationStatus: Readonly<{
  ACCEPTED: "accepted";
}>;

export interface ChatFeedbackPersistedResult {
  rating: ChatFeedbackValue;
  mutation_sequence: number;
  mutation_status: ChatFeedbackMutationStatusValue;
}

export interface ChatFeedbackSnapshot {
  message_id: string;
  rating: ChatFeedbackValue;
  mutation_sequence: number;
  target_kind: string;
  target_id: string;
  task_id?: string | null;
  plan_id?: string | null;
}

export function chatFeedbackSubjectKey(
  targetKind: string | null | undefined,
  targetId: string | null | undefined,
): string | null;

export interface ChatFeedbackCoordinator {
  dispose(): void;
  hydrate(scope: string, records: ChatFeedbackSnapshot[]): void;
  snapshot(scope: string): Record<string, ChatFeedbackValue>;
  subscribe(
    scope: string,
    listener: (values: Record<string, ChatFeedbackValue>) => void,
  ): () => void;
  submit(
    scope: string,
    key: string,
    value: ChatFeedbackValue,
    persist: (
      value: ChatFeedbackValue,
    ) => Promise<ChatFeedbackPersistedResult | unknown>,
  ): Promise<void>;
}

export function createChatFeedbackCoordinator(options?: {
  channel?: {
    addEventListener?(type: "message", listener: (event: MessageEvent) => void): void;
    removeEventListener?(type: "message", listener: (event: MessageEvent) => void): void;
    postMessage?(message: unknown): void;
    close?(): void;
  } | null;
}): ChatFeedbackCoordinator;

export const chatFeedbackCoordinator: ChatFeedbackCoordinator;
