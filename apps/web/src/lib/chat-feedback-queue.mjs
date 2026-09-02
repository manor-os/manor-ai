export const ChatFeedbackMutationStatus = Object.freeze({
  ACCEPTED: "accepted",
});

const FEEDBACK_BROADCAST_CHANNEL = "manor:chat-feedback";

function isFeedbackValue(value) {
  return value === "up" || value === "down";
}

function isServerRevision(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function defaultBroadcastChannel() {
  try {
    const Channel = globalThis.document?.defaultView?.BroadcastChannel;
    return Channel ? new Channel(FEEDBACK_BROADCAST_CHANNEL) : null;
  } catch {
    return null;
  }
}

function normalizedPersistedResult(result, fallbackValue) {
  if (!isServerRevision(result?.mutation_sequence)) {
    throw new Error("Feedback response is missing its server revision");
  }
  return {
    rating: isFeedbackValue(result?.rating) ? result.rating : fallbackValue,
    revision: result.mutation_sequence,
  };
}

function scopedKey(scope, key) {
  return `${scope}\u0000${key}`;
}

export function chatFeedbackSubjectKey(targetKind, targetId) {
  if (typeof targetKind !== "string" || typeof targetId !== "string") return null;
  const rawKind = targetKind.trim();
  const kind = rawKind === "task_completion" || rawKind === "plan_completion"
    ? "completion"
    : rawKind;
  const id = targetId.trim();
  return kind && id ? `subject:${kind}:${id}` : null;
}

export function createChatFeedbackCoordinator(options = {}) {
  const channel = options.channel === undefined ? null : options.channel;
  const entries = new Map();
  const valuesByScope = new Map();
  const confirmedValues = new Map();
  const confirmedRevisions = new Map();
  const listenersByScope = new Map();

  const valuesFor = (scope) => {
    let values = valuesByScope.get(scope);
    if (!values) {
      values = new Map();
      valuesByScope.set(scope, values);
    }
    return values;
  };
  const snapshot = (scope) => Object.fromEntries(valuesFor(scope));
  const notify = (scope) => {
    const next = snapshot(scope);
    listenersByScope.get(scope)?.forEach((listener) => listener(next));
  };
  const writeValue = (scope, key, value) => {
    const values = valuesFor(scope);
    if (value) values.set(key, value);
    else values.delete(key);
    notify(scope);
  };
  const hasPendingMutation = (scope, key) => entries.has(scopedKey(scope, key));
  const applyAuthoritative = (scope, key, value, revision, publish = false) => {
    const id = scopedKey(scope, key);
    const currentRevision = confirmedRevisions.get(id) ?? -1;
    if (revision < currentRevision) return false;

    confirmedRevisions.set(id, revision);
    confirmedValues.set(id, value);
    if (!hasPendingMutation(scope, key)) writeValue(scope, key, value);
    if (publish) {
      channel?.postMessage?.({
        type: "chat_feedback_persisted",
        scope,
        key,
        rating: value,
        mutation_sequence: revision,
      });
    }
    return true;
  };
  const receive = (event) => {
    const payload = event?.data;
    const revision = Number(payload?.mutation_sequence);
    if (
      payload?.type !== "chat_feedback_persisted" ||
      typeof payload.scope !== "string" ||
      typeof payload.key !== "string" ||
      !isFeedbackValue(payload.rating) ||
      !isServerRevision(revision)
    ) {
      return;
    }
    applyAuthoritative(payload.scope, payload.key, payload.rating, revision);
  };
  channel?.addEventListener?.("message", receive);

  const subscribe = (scope, listener) => {
    let listeners = listenersByScope.get(scope);
    if (!listeners) {
      listeners = new Set();
      listenersByScope.set(scope, listeners);
    }
    listeners.add(listener);
    listener(snapshot(scope));
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) listenersByScope.delete(scope);
    };
  };

  const hydrate = (scope, records) => {
    for (const record of records || []) {
      const revision = Number(record?.mutation_sequence);
      if (
        typeof record?.message_id !== "string" ||
        !isFeedbackValue(record.rating) ||
        !isServerRevision(revision)
      ) {
        continue;
      }
      applyAuthoritative(
        scope,
        record.message_id,
        record.rating,
        revision,
      );
      const subjectKey = chatFeedbackSubjectKey(
        record.target_kind,
        record.target_id,
      );
      if (subjectKey) {
        applyAuthoritative(
          scope,
          subjectKey,
          record.rating,
          revision,
        );
      }
    }
  };

  const submit = (scope, key, value, persist) => {
    const id = scopedKey(scope, key);
    let entry = entries.get(id);
    if (!entry) {
      entry = { latestGeneration: 0, tail: Promise.resolve() };
      entries.set(id, entry);
    }

    entry.latestGeneration += 1;
    const generation = entry.latestGeneration;
    writeValue(scope, key, value);

    const result = entry.tail.then(async () => {
      try {
        const persisted = normalizedPersistedResult(await persist(value), value);
        applyAuthoritative(
          scope,
          key,
          persisted.rating,
          persisted.revision,
          true,
        );
        if (entry.latestGeneration === generation) {
          writeValue(scope, key, confirmedValues.get(id) || null);
        }
      } catch (error) {
        if (entry.latestGeneration === generation) {
          writeValue(scope, key, confirmedValues.get(id) || null);
        }
        throw error;
      } finally {
        if (entry.latestGeneration === generation) entries.delete(id);
      }
    });
    entry.tail = result.catch(() => undefined);
    return result;
  };

  const dispose = () => {
    channel?.removeEventListener?.("message", receive);
    channel?.close?.();
    listenersByScope.clear();
  };

  return { dispose, hydrate, snapshot, subscribe, submit };
}

export const chatFeedbackCoordinator = createChatFeedbackCoordinator({
  channel: defaultBroadcastChannel(),
});
