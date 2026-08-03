import { Navigate } from "react-router-dom";
import { useAuthStore } from "../stores/auth";
import OAuthClientsPanel from "../components/integrations/OAuthClientsPanel";
import PageHeader from "../components/ui/PageHeader";
import { t } from "../lib/i18n";

export default function AdminOAuthClients() {
  const role = useAuthStore((s) => s.user?.role);
  const isAdmin = role === "admin" || role === "owner";
  if (!isAdmin) return <Navigate to="/404" replace />;

  return (
    <div style={{ padding: 0, maxWidth: 1200 }}>
      <PageHeader
        title={t("page.admin_oauth.title")}
        subtitle={t("page.admin_oauth.subtitle")}
      />

      <OAuthClientsPanel />
    </div>
  );
}
