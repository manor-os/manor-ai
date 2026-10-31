import { useAuthStore } from "../stores/auth";
import { decodeAuthTokenClaims } from "./authToken";
import { COLLABORATOR_ROLE } from "./persistentSession";

export { COLLABORATOR_ROLE };

/**
 * Whether the active company session is an external collaborator's.
 *
 * The server confines such a session to the granted Workspace and refuses
 * company routes (notifications, staff, billing...), so callers use
 * this to avoid issuing requests that can only fail. The token's role is read
 * until /auth/me has loaded the user for the same company.
 */
export function isCollaboratorSession(token: string | null, user: { role?: string; entity_id?: string } | null): boolean {
  const claims = decodeAuthTokenClaims(token);
  if (user?.role && (!claims?.entity_id || user.entity_id === claims.entity_id)) return user.role === COLLABORATOR_ROLE;
  return claims?.role === COLLABORATOR_ROLE;
}

export function useIsCollaborator(): boolean {
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  return isCollaboratorSession(token, user);
}
