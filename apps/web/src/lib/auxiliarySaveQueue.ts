export interface AuxiliarySaveIntent {
  sessionId: string;
  sequence: number;
}

interface AuxiliarySaveContext {
  intent: AuxiliarySaveIntent;
  isLatest: () => boolean;
  signal: AbortSignal;
}

type AuxiliarySaveOperation = (context: AuxiliarySaveContext) => Promise<boolean>;

interface AuxiliarySaveOptions {
  timeoutMs?: number;
  onTimeout?: () => void;
}

const auxiliarySaveQueues = new Map<string, Promise<boolean>>();
const AUXILIARY_SAVE_SESSION_STORAGE_KEY = "manor:editor-save-session";
const AUXILIARY_SAVE_SEQUENCE_STORAGE_KEY = "manor:editor-save-sequence";
const AUXILIARY_SAVE_SEQUENCE_LOCK_KEY = "manor:editor-save-sequence-lock";
const auxiliarySavePageId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
const auxiliarySessionStorage = (() => {
  try {
    return globalThis.sessionStorage || null;
  } catch {
    return null;
  }
})();
const auxiliarySharedSequenceStorage = (() => {
  try {
    const storage = globalThis.localStorage;
    if (!storage || !globalThis.navigator?.locks) return null;
    const probeKey = `${AUXILIARY_SAVE_SEQUENCE_STORAGE_KEY}:probe:${auxiliarySavePageId}`;
    storage.setItem(probeKey, auxiliarySavePageId);
    const available = storage.getItem(probeKey) === auxiliarySavePageId;
    storage.removeItem(probeKey);
    return available ? storage : null;
  } catch {
    return null;
  }
})();
const auxiliarySequenceLockManager = auxiliarySharedSequenceStorage
  ? globalThis.navigator?.locks
  : null;
const auxiliarySaveSessionLineage = (() => {
  try {
    const existing = auxiliarySessionStorage?.getItem(AUXILIARY_SAVE_SESSION_STORAGE_KEY);
    if (existing) return existing;
    const generated = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    auxiliarySessionStorage?.setItem(AUXILIARY_SAVE_SESSION_STORAGE_KEY, generated);
    return generated;
  } catch {
    return `${Date.now()}-${Math.random()}`;
  }
})();
const auxiliaryFallbackSaveSessionId = `${auxiliarySaveSessionLineage}:${auxiliarySavePageId}`;

function readStoredSequence(storage: Storage | null): number {
  try {
    const stored = Number(storage?.getItem(AUXILIARY_SAVE_SEQUENCE_STORAGE_KEY));
    return Number.isSafeInteger(stored) && stored > 0 ? stored : 0;
  } catch {
    return 0;
  }
}

let auxiliarySaveSequence = (() => {
  return Math.max(
    readStoredSequence(auxiliarySessionStorage),
    readStoredSequence(auxiliarySharedSequenceStorage),
  );
})();
let auxiliarySequenceAllocationQueue = Promise.resolve();

function persistAuxiliarySaveSequence(sequence: number): void {
  try {
    auxiliarySessionStorage?.setItem(
      AUXILIARY_SAVE_SEQUENCE_STORAGE_KEY,
      String(sequence),
    );
  } catch {
    // The shared counter or in-memory ordering still protects this page.
  }
  try {
    auxiliarySharedSequenceStorage?.setItem(
      AUXILIARY_SAVE_SEQUENCE_STORAGE_KEY,
      String(sequence),
    );
  } catch {
    // The per-page session ID prevents collisions when shared storage fails.
  }
}

function nextAuxiliarySaveIntent(): Promise<AuxiliarySaveIntent> {
  const allocate = async () => {
    if (auxiliarySequenceLockManager && auxiliarySharedSequenceStorage) {
      return auxiliarySequenceLockManager.request(AUXILIARY_SAVE_SEQUENCE_LOCK_KEY, () => {
        const sharedSessionId = auxiliarySharedSequenceStorage.getItem(
          AUXILIARY_SAVE_SESSION_STORAGE_KEY,
        ) || auxiliarySaveSessionLineage;
        auxiliarySaveSequence = Math.max(
          auxiliarySaveSequence,
          readStoredSequence(auxiliarySharedSequenceStorage),
        ) + 1;
        auxiliarySharedSequenceStorage.setItem(
          AUXILIARY_SAVE_SESSION_STORAGE_KEY,
          sharedSessionId,
        );
        persistAuxiliarySaveSequence(auxiliarySaveSequence);
        return { sessionId: sharedSessionId, sequence: auxiliarySaveSequence };
      });
    }
    auxiliarySaveSequence += 1;
    persistAuxiliarySaveSequence(auxiliarySaveSequence);
    return {
      sessionId: auxiliaryFallbackSaveSessionId,
      sequence: auxiliarySaveSequence,
    };
  };
  const allocation = auxiliarySequenceAllocationQueue.then(allocate);
  auxiliarySequenceAllocationQueue = allocation.then(() => undefined, () => undefined);
  return allocation;
}

export async function allocateEditorSaveIntent(): Promise<AuxiliarySaveIntent> {
  return nextAuxiliarySaveIntent();
}

function runAuxiliarySave(
  save: AuxiliarySaveOperation,
  intent: AuxiliarySaveIntent,
  isLatest: () => boolean,
  options: AuxiliarySaveOptions,
): Promise<boolean> {
  const abortController = new AbortController();
  let operation: Promise<boolean>;
  try {
    operation = save({
      intent,
      isLatest,
      signal: abortController.signal,
    });
  } catch (error) {
    operation = Promise.reject(error);
  }
  const { timeoutMs, onTimeout } = options;
  if (timeoutMs === undefined || timeoutMs <= 0) return operation;

  return new Promise<boolean>((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      abortController.abort();
      if (isLatest()) {
        try {
          onTimeout?.();
        } catch {
          // A notification failure must not keep save callers waiting forever.
        }
      }
      resolve(false);
    }, timeoutMs);

    void operation.then(
      (result) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        resolve(result);
      },
      (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        reject(error);
      },
    );
  });
}

export function enqueueAuxiliarySave(
  authEntity: string,
  path: string,
  save: AuxiliarySaveOperation,
  options: AuxiliarySaveOptions = {},
): Promise<boolean> {
  const queueKey = JSON.stringify([authEntity, path]);
  const previousSave = auxiliarySaveQueues.get(queueKey) || Promise.resolve(true);
  const intent = allocateEditorSaveIntent();
  let saveTask!: Promise<boolean>;
  saveTask = previousSave
    .catch(() => false)
    .then(async () => runAuxiliarySave(
      save,
      await intent,
      () => auxiliarySaveQueues.get(queueKey) === saveTask,
      options,
    ));
  auxiliarySaveQueues.set(queueKey, saveTask);
  const clearCompletedSave = () => {
    if (auxiliarySaveQueues.get(queueKey) === saveTask) auxiliarySaveQueues.delete(queueKey);
  };
  void saveTask.then(clearCompletedSave, clearCompletedSave);
  return saveTask;
}
