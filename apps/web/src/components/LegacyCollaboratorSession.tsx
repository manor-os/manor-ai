import { useEffect, useState } from "react";
import { decodeAuthTokenClaims, getAuthToken } from "../lib/authToken";
import { COLLABORATOR_ROLE, restorePersistentSession } from "../lib/persistentSession";
import { t } from "../lib/i18n";
import { useAuthStore } from "../stores/auth";
import Button from "./ui/Button";
import EmptyState from "./ui/EmptyState";
import { PageLoading } from "./ui/Skeleton";

/**
 * A session still parked in a customer's company from before customer trials
 * opened from the provider's own Workspace. The session refresh returns it to
 * the person's own company; without a remembered session they sign in again.
 */
export default function LegacyCollaboratorSession() {
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    void restorePersistentSession().catch(() => false).then((moved) => {
      if (!active) return;
      if (!moved || decodeAuthTokenClaims(getAuthToken())?.role === COLLABORATOR_ROLE) setFailed(true);
    });
    return () => { active = false; };
  }, []);
  if (!failed) return <PageLoading label={t("status.loading")} />;
  return (
    <div className="flex h-screen items-center justify-center p-6">
      <EmptyState
        title={t("page.client_trials.legacy_session")}
        action={<Button onClick={() => void useAuthStore.getState().logout().then(() => window.location.assign("/login"))}>{t("action.logout")}</Button>}
      />
    </div>
  );
}
