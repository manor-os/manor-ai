import { api, DocumentUploadAuthChangedError } from "./api";
import { authPrincipalKey, getAuthToken, isSameAuthIdentity } from "./authToken";

interface InputAttachment {
  name: string;
  id: string;
  type: "knowledge";
  origin: "input_upload";
}

interface PendingUpload {
  idempotencyKey: string;
  attachment?: InputAttachment;
  pending?: Promise<InputAttachment>;
}

// The input card owns the File lifetime. Retain receipts through a failed batch
// or failed resolve request without keeping file bytes after the card is gone.
const uploads = new WeakMap<File, Map<string, PendingUpload>>();

export async function uploadPendingInputFiles(
  scope: { workspaceId: string; messageId: string },
  files: File[],
): Promise<InputAttachment[]> {
  const token = getAuthToken();
  const scopeKey = JSON.stringify([authPrincipalKey(token), scope.workspaceId, scope.messageId]);
  const assertCurrentIdentity = () => {
    if (!isSameAuthIdentity(token, getAuthToken())) throw new DocumentUploadAuthChangedError();
  };
  const attachments = await Promise.all(files.map(async (file) => {
    assertCurrentIdentity();
    let fileUploads = uploads.get(file);
    if (!fileUploads) {
      fileUploads = new Map();
      uploads.set(file, fileUploads);
    }
    let entry = fileUploads.get(scopeKey);
    if (!entry) {
      entry = {
        idempotencyKey: typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `input-upload-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      };
      fileUploads.set(scopeKey, entry);
    }
    if (entry.attachment) return entry.attachment;
    if (!entry.pending) {
      const upload = entry;
      entry.pending = api.documents.upload(file, undefined, { visibility: "private" }, {
        idempotencyKey: upload.idempotencyKey,
      }).then((document) => {
        assertCurrentIdentity();
        if (!document?.id) throw new Error(`Failed to upload ${file.name}`);
        upload.attachment = { name: file.name, id: document.id, type: "knowledge", origin: "input_upload" };
        return upload.attachment;
      }).finally(() => {
        upload.pending = undefined;
      });
    }
    return entry.pending;
  }));
  // Even an already uploaded receipt must not cross an account/permission
  // change while its sibling uploads finish. resolveAction rechecks access.
  assertCurrentIdentity();
  return attachments;
}
