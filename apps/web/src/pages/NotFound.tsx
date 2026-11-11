import { Link } from "react-router-dom";
import { t } from "../lib/i18n";

export default function NotFound() {
  return (
    <div className="flex h-screen flex-col items-center justify-center gap-2 px-6 text-center animate-fade-in">
      <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13 }}>404</p>
      <h1 style={{ margin: 0, color: "var(--text-strong)", fontSize: 20, fontWeight: 600 }}>{t("page.not_found.title")}</h1>
      <p style={{ margin: "0 0 16px", color: "var(--text-muted)", fontSize: 14 }}>{t("page.not_found.description")}</p>
      <Link to="/" className="btn-manor-outline inline-flex items-center gap-2">
        {t("page.not_found.back_home")}
      </Link>
    </div>
  );
}
