export type ShareRole = "viewer" | "commenter" | "editor" | "co_owner";

// Matches the complete document capability bundle accepted by the ACL API.
export const DOCUMENT_CO_OWNER_CAPABILITIES = [
  "view", "view_redacted", "comment", "edit", "download", "print",
  "manage_metadata", "share_internal", "share_external", "reclassify", "delete", "grant_access",
] as const;

export function shareRoleCapabilities(role: ShareRole, resourceType: "document" | "document_folder" = "document"): string[] {
  const folder = resourceType === "document_folder";
  switch (role) {
    case "viewer": return ["view"];
    case "commenter": return ["view", "comment"];
    case "editor": return ["view", "comment", "edit", ...(folder ? ["upload_to"] : [])];
    case "co_owner": return [...DOCUMENT_CO_OWNER_CAPABILITIES, ...(folder ? ["upload_to"] : [])];
  }
}

export function capabilitiesToShareRole(caps: readonly string[], resourceType: "document" | "document_folder" = "document"): ShareRole {
  const set = new Set(caps);
  if (shareRoleCapabilities("co_owner", resourceType).every((capability) => set.has(capability))) return "co_owner";
  // Existing Access manager grants display as Editor; role projection never
  // changes the stored capability bundle or promotes it to Co-owner.
  if (set.has("edit")) return "editor";
  if (set.has("comment")) return "commenter";
  return "viewer";
}
