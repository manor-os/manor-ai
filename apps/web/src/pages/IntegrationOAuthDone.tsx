import { useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { OAUTH_POPUP_MESSAGE } from "../lib/integrationConnect";
import { t } from "../lib/i18n";

/**
 * Where a provider sends the connect popup back. It tells the page that
 * opened it (the chat) and closes; opened on its own, it shows the
 * Integrations page the old way.
 */
export default function IntegrationOAuthDone() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  useEffect(() => {
    const connected = params.get("connected");
    const cancelled = params.get("oauth_cancelled");
    const provider = connected || cancelled || "";
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage(
        { type: OAUTH_POPUP_MESSAGE, provider, status: connected ? "connected" : "cancelled" },
        window.location.origin,
      );
      window.close();
      return;
    }
    navigate(provider ? `/integrations?provider=${encodeURIComponent(provider)}` : "/integrations", { replace: true });
  }, [params, navigate]);
  return (
    <div className="flex h-screen items-center justify-center p-6 text-sm text-stone-500">
      {t("status.loading")}
    </div>
  );
}
