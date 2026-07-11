export const ERROR_BOUNDARY_AUTO_RELOAD_KEY = "manor:eb-auto-reload";

export function getErrorText(err: unknown): string {
  if (!err) return "";
  if (typeof err === "string") return err;
  if (err instanceof Error) return `${err.name || ""} ${err.message || ""}`;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

export function isStaleChunkError(err: unknown): boolean {
  const msg = getErrorText(err).toLowerCase();
  return (
    msg.includes("dynamically imported module") ||
    msg.includes("loading chunk") ||
    msg.includes("chunkloaderror") ||
    msg.includes("module script failed") ||
    msg.includes("importing a module")
  );
}

export function isExternalDomMutationError(err: unknown): boolean {
  const msg = getErrorText(err).toLowerCase();
  const nodeApiMismatch =
    msg.includes("failed to execute") &&
    msg.includes("node") &&
    (
      msg.includes("removechild") ||
      msg.includes("remoyechild") ||
      msg.includes("insertbefore")
    );
  const childMismatch =
    msg.includes("not a child of this node") ||
    msg.includes("node to be removed is not a child") ||
    msg.includes("node to be remoyed is not a child") ||
    msg.includes("child of this node");
  const notFoundDomException =
    typeof DOMException !== "undefined" &&
    err instanceof DOMException &&
    err.name === "NotFoundError";

  return (nodeApiMismatch && childMismatch) || (notFoundDomException && childMismatch);
}

export function isRecoverableUiRuntimeError(err: unknown): boolean {
  return isStaleChunkError(err) || isExternalDomMutationError(err);
}

export function shouldAutoReloadForRecoverableError(): boolean {
  try {
    return sessionStorage.getItem(ERROR_BOUNDARY_AUTO_RELOAD_KEY) !== __APP_VERSION__;
  } catch {
    return false;
  }
}

export function markRecoverableErrorAutoReloadAttempt(): void {
  try {
    sessionStorage.setItem(ERROR_BOUNDARY_AUTO_RELOAD_KEY, __APP_VERSION__);
  } catch {
    // ignore storage issues in private / restricted environments
  }
}

export function clearRecoverableErrorAutoReloadAttempt(): void {
  try {
    sessionStorage.removeItem(ERROR_BOUNDARY_AUTO_RELOAD_KEY);
  } catch {
    // ignore storage issues in private / restricted environments
  }
}
