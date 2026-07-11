import { Link } from "react-router-dom";
import { t } from "../lib/i18n";

export default function NotFound() {
  return (
    <div className="flex items-center justify-center h-screen relative">
      {/* Aurora background */}
      <div className="aurora-bg">
        <div className="aurora-blob aurora-blob-1" />
        <div className="aurora-blob aurora-blob-2" />
      </div>

      <div className="glass-panel p-12 text-center max-w-md relative z-10 animate-fade-in">
        <div className="w-20 h-20 mx-auto mb-6 rounded-2xl bg-gradient-to-br from-manor-500 to-manor-700 flex items-center justify-center">
          <span className="text-3xl font-extrabold text-white">404</span>
        </div>
        <h2 className="text-xl font-bold text-stone-900 mb-2">{t("page.not_found.title")}</h2>
        <p className="text-sm text-stone-500 mb-8">
          {t("page.not_found.description")}
        </p>
        <Link to="/" className="btn-manor inline-flex items-center gap-2">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" />
          </svg>
          {t("page.not_found.back_home")}
        </Link>
      </div>
    </div>
  );
}
