/**
 * Frontend mirror of `packages/core/permissions.py::has_permission`.
 *
 * Used to gate UI affordances (hide buttons, dim rows, show tooltips)
 * so the interface matches what the backend will accept. Backend still
 * enforces on every mutating endpoint — this is purely for UX.
 *
 * Keep in sync with the ROLE_PERMISSIONS map in permissions.py.
 */
import { useAuthStore } from "../stores/auth";
import type { Document, DocumentFolderInfo, User, WorkspaceStaff } from "./types";
import { capabilitiesToShareRole } from "./shareRoles";

const _HIERARCHY = ["viewer", "member", "admin", "owner"] as const;

const _ROLE_PERMISSIONS: Record<string, string[]> = {
  viewer: [
    "entity.read", "tasks.read", "docs.read", "agents.read",
    "chat.use", "workspaces.read", "integrations.read",
  ],
  member: [
    "tasks.create", "tasks.update", "tasks.assign",
    "docs.upload", "agents.create",
    "integrations.connect", "integrations.share", "mcp.use_personal",
  ],
  admin: [
    "entity.update", "users.read", "users.invite",
    "tasks.delete", "docs.delete", "docs.transfer_departed", "agents.update", "agents.delete",
    "workspaces.create", "workspaces.update", "workspaces.delete",
    "admin.settings", "admin.audit", "chat.view_all",
    "integrations.manage", "mcp.quickbooks.use", "mcp.stripe.use",
  ],
  owner: [
    "users.manage", "admin.api_keys", "admin.webhooks", "admin.billing",
    "docs.read.all", "docs.manage.all", "workspaces.read.all",
  ],
};

function collect(role: string): Set<string> {
  const out = new Set<string>();
  if (!_HIERARCHY.includes(role as (typeof _HIERARCHY)[number])) return out;
  for (const r of _HIERARCHY) {
    for (const p of _ROLE_PERMISSIONS[r] || []) out.add(p);
    if (r === role) break;
  }
  return out;
}

/** Pure check — does a role grant a permission? */
export function hasPermission(role: string | undefined, permission: string): boolean {
  if (!role) return false;
  return collect(role).has(permission);
}

function _isEntityAdminRole(role: string | undefined): boolean {
  return role === "owner" || role === "admin";
}

function _canManageAllKnowledge(user: Pick<User, "role" | "permissions">): boolean {
  if (user.permissions) return user.permissions.includes("docs.manage.all");
  return user.role === "owner";
}

function _canManageAllWorkspaces(user: Pick<User, "role" | "permissions">): boolean {
  if (user.permissions) {
    return _isEntityAdminRole(user.role)
      && user.permissions.includes("workspaces.read.all");
  }
  return user.role === "owner";
}

function _sameIdentity(left: string | null | undefined, right: string | null | undefined): boolean {
  return Boolean(left && right && String(left) === String(right));
}

function _isActiveWorkspaceStaff(row: WorkspaceStaff): boolean {
  if (row.status && row.status !== "active") return false;
  if (!row.expires_at) return true;
  return new Date(row.expires_at).getTime() > Date.now();
}

export function isEntityAdmin(user: Pick<User, "role"> | null | undefined): boolean {
  return _isEntityAdminRole(user?.role);
}

export function canManageDocument(
  user: Pick<User, "id" | "email" | "display_name" | "role" | "permissions"> | null | undefined,
  doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined,
): boolean {
  if (!user || !doc) return false;
  if (_canManageAllKnowledge(user)) return true;
  if (_sameIdentity(doc.owner_id, user.id)) return true;
  if (capabilitiesToShareRole(doc.current_user_capabilities || []) === "co_owner") return true;
  // Legacy rows may store the immutable user id in created_by. Mutable
  // display names and email addresses are labels, never authorization keys.
  return _sameIdentity(doc.created_by, user.id);
}

export function hasDocumentCapability(
  doc: Pick<Document, "current_user_capabilities"> | null | undefined,
  capability: string,
): boolean {
  return Boolean(doc?.current_user_capabilities?.includes(capability));
}

export function canEditDocument(
  user: Pick<User, "id" | "email" | "display_name" | "role" | "permissions"> | null | undefined,
  doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageDocument(user, doc) || hasDocumentCapability(doc, "edit");
}

export function canCommentDocument(
  user: Pick<User, "id" | "email" | "display_name" | "role" | "permissions"> | null | undefined,
  doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageDocument(user, doc) || hasDocumentCapability(doc, "comment");
}

export function canShareDocument(
  user: Pick<User, "id" | "email" | "display_name" | "role" | "permissions"> | null | undefined,
  doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageDocument(user, doc)
    || hasDocumentCapability(doc, "grant_access")
    || hasDocumentCapability(doc, "share_internal")
    || hasDocumentCapability(doc, "share_external");
}

export function canShareDocumentExternally(
  user: Pick<User, "id" | "role" | "permissions"> | null | undefined,
  doc: Pick<Document, "owner_id" | "current_user_capabilities"> | null | undefined,
): boolean {
  if (!user || !doc) return false;
  return _canManageAllKnowledge(user)
    || _sameIdentity(doc.owner_id, user.id)
    || hasDocumentCapability(doc, "share_external");
}

export function canManageDocumentMetadata(
  user: Pick<User, "id" | "email" | "display_name" | "role" | "permissions"> | null | undefined,
  doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageDocument(user, doc) || hasDocumentCapability(doc, "manage_metadata");
}

export function canDeleteDocument(
  user: Pick<User, "id" | "email" | "display_name" | "role" | "permissions"> | null | undefined,
  doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageDocument(user, doc) || hasDocumentCapability(doc, "delete");
}

export function canManageFolder(
  user: Pick<User, "id" | "role" | "permissions"> | null | undefined,
  folder: Pick<DocumentFolderInfo, "owner_id" | "current_user_capabilities"> | null | undefined,
): boolean {
  if (!user || !folder) return false;
  if (_canManageAllKnowledge(user)) return true;
  return _sameIdentity(folder.owner_id, user.id)
    || capabilitiesToShareRole(folder.current_user_capabilities || [], "document_folder") === "co_owner";
}

export function hasFolderCapability(
  folder: Pick<DocumentFolderInfo, "current_user_capabilities"> | null | undefined,
  capability: string,
): boolean {
  return Boolean(folder?.current_user_capabilities?.includes(capability));
}

export function canShareFolder(
  user: Pick<User, "id" | "role" | "permissions"> | null | undefined,
  folder: Pick<DocumentFolderInfo, "owner_id" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageFolder(user, folder)
    || hasFolderCapability(folder, "grant_access")
    || hasFolderCapability(folder, "share_internal")
    || hasFolderCapability(folder, "share_external");
}

export function canManageFolderMetadata(
  user: Pick<User, "id" | "role" | "permissions"> | null | undefined,
  folder: Pick<DocumentFolderInfo, "owner_id" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageFolder(user, folder) || hasFolderCapability(folder, "manage_metadata");
}

export function canDeleteFolder(
  user: Pick<User, "id" | "role" | "permissions"> | null | undefined,
  folder: Pick<DocumentFolderInfo, "owner_id" | "current_user_capabilities"> | null | undefined,
): boolean {
  return canManageFolder(user, folder) || hasFolderCapability(folder, "delete");
}

export function canManageWorkspace(
  user: Pick<User, "id" | "role" | "permissions"> | null | undefined,
  staffRows: WorkspaceStaff[] | null | undefined,
): boolean {
  if (!user) return false;
  if (_canManageAllWorkspaces(user)) return true;
  return (staffRows || []).some((row) => (
    row.role === "owner"
    && _sameIdentity(row.user_id, user.id)
    && _isActiveWorkspaceStaff(row)
  ));
}

/** Workspace content write access: owner/editor/contributor, excluding viewer. */
export function canWriteWorkspace(
  user: Pick<User, "id" | "role" | "permissions"> | null | undefined,
  staffRows: WorkspaceStaff[] | null | undefined,
): boolean {
  if (!user) return false;
  if (_canManageAllWorkspaces(user)) return true;
  return (staffRows || []).some((row) => (
    ["owner", "editor", "contributor"].includes(String(row.role || "").toLowerCase())
    && _sameIdentity(row.user_id, user.id)
    && _isActiveWorkspaceStaff(row)
  ));
}

/**
 * React hook — reads the current user's role from the auth store and
 * returns a memoized `can(permission)` function.
 *
 * Usage:
 *   const can = usePermission();
 *   {can("users.invite") && <Button>Invite</Button>}
 */
export function usePermission() {
  const user = useAuthStore((s) => s.user);
  // StaffRole permissions are authoritative when the API supplies them.
  // Falling back to the legacy role matrix is only for older identities that
  // do not yet have a Staff assignment.
  const perms = user?.permissions
    ? new Set(user.permissions)
    : collect(user?.role || "");
  return (permission: string) => perms.has(permission);
}
