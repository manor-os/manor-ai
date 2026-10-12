export type OfficeSaveOrigin = "autosave" | "explicit";

export interface OfficeSaveRevision {
  origin: OfficeSaveOrigin;
  sessionRevision: number;
  revision: number;
}

/** A queued autosave is replaceable only by a newer snapshot from its own session. */
export function shouldSkipQueuedOfficeAutosave(
  request: OfficeSaveRevision,
  currentSessionRevision: number,
  currentRevision: number,
): boolean {
  return request.origin === "autosave"
    && request.sessionRevision === currentSessionRevision
    && request.revision < currentRevision;
}
