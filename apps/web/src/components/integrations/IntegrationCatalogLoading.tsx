import { t } from "../../lib/i18n";

/** Preserve the catalog's card grid while the first database snapshot loads. */
export default function IntegrationCatalogLoading() {
  return (
    <div role="status" aria-label={t("page.integrations.loading_integrations")}>
      <span className="sr-only">{t("page.integrations.loading_integrations")}</span>
      <div aria-hidden="true" style={{ height: 66, borderRadius: 14, background: "var(--bg-subtle, #f5f5f4)", marginBottom: 16 }} />
      <div aria-hidden="true" style={{ width: 240, height: 36, borderRadius: 10, background: "var(--bg-subtle, #f5f5f4)", marginBottom: 24 }} />
      <div aria-hidden="true" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 300px), 1fr))", gap: 12 }}>
        {Array.from({ length: 9 }, (_, index) => (
          <div key={index} className="compact-card" style={{ pointerEvents: "none", minHeight: 70 }}>
            <div style={{ width: 34, height: 34, borderRadius: 10, background: "var(--bg-subtle, #f5f5f4)", flexShrink: 0 }} />
            <div style={{ flex: 1, display: "grid", gap: 8 }}>
              <div style={{ width: "48%", height: 10, borderRadius: 4, background: "var(--bg-subtle, #f5f5f4)" }} />
              <div style={{ width: "76%", height: 8, borderRadius: 4, background: "var(--bg-subtle, #f5f5f4)" }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
