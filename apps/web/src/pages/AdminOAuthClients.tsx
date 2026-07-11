import { Navigate } from "react-router-dom";
import { useAuthStore } from "../stores/auth";
import OAuthClientsPanel from "../components/integrations/OAuthClientsPanel";
import { t } from "../lib/i18n";

export default function AdminOAuthClients() {
  const role = useAuthStore((s) => s.user?.role);
  const isAdmin = role === "admin" || role === "owner";
  if (!isAdmin) return <Navigate to="/404" replace />;

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: "0 auto" }}>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ fontSize: 24, fontWeight: 700, color: "#1c1917", margin: "8px 0 4px" }}>
          {t("page.admin_oauth.title")}
        </h1>
        <p style={{ fontSize: 13, color: "#78716c", margin: 0, lineHeight: 1.5 }}>
          {t("page.admin_oauth.subtitle")}
        </p>
      </div>

      <OAuthClientsPanel />
    </div>
  );
}
