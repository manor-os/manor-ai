import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Report } from "../lib/types";
import { useToastStore } from "../stores/toast";
import PageHeader from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import Modal from "../components/ui/Modal";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import { IconDownload, IconRefresh } from "../components/icons";

import { t } from "../lib/i18n";
/* -- types ---------------------------------------------------- */

type ReportTab = "tasks" | "usage" | "activity";

/* -- component ------------------------------------------------ */

export default function Reports() {
  const toast = useToastStore();

  const [tab, setTab] = useState<ReportTab>("tasks");
  const [dateFrom, setDateFrom] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - 30);
    return d.toISOString().split("T")[0];
  });
  const [dateTo, setDateTo] = useState(() => new Date().toISOString().split("T")[0]);
  const [emailModalOpen, setEmailModalOpen] = useState(false);
  const [emailRecipients, setEmailRecipients] = useState("");

  const queryParams = { from: dateFrom, to: dateTo };

  const { data: tasksReport, isLoading: tasksLoading, refetch: refetchTasks } = useQuery({
    queryKey: ["reports", "tasks", queryParams],
    queryFn: () => api.reports.tasks(queryParams),
    enabled: tab === "tasks",
  });

  const { data: usageReport, isLoading: usageLoading, refetch: refetchUsage } = useQuery({
    queryKey: ["reports", "usage", queryParams],
    queryFn: () => api.reports.usage(queryParams),
    enabled: tab === "usage",
  });

  const { data: activityReport, isLoading: activityLoading, refetch: refetchActivity } = useQuery({
    queryKey: ["reports", "activity", queryParams],
    queryFn: () => api.reports.activity(queryParams),
    enabled: tab === "activity",
  });

  const emailMutation = useMutation({
    mutationFn: (data: { report_type: string; recipients: string[] }) => api.reports.email(data.report_type, data.recipients),
    onSuccess: () => {
      setEmailModalOpen(false);
      setEmailRecipients("");
      toast.success(t("page.reports.report_emailed_successfully"));
    },
  });

  const currentReport = tab === "tasks" ? tasksReport : tab === "usage" ? usageReport : activityReport;
  const isLoading = tab === "tasks" ? tasksLoading : tab === "usage" ? usageLoading : activityLoading;

  function handleGenerate() {
    if (tab === "tasks") refetchTasks();
    else if (tab === "usage") refetchUsage();
    else refetchActivity();
  }

  async function handleExportHtml() {
    try {
      let htmlFn;
      if (tab === "tasks") htmlFn = api.reports.tasksHtml;
      else if (tab === "usage") htmlFn = api.reports.usageHtml;
      else {
        toast.error(t("page.reports.export_not_available"), t("page.reports.activity_html_export_not_supported"));
        return;
      }

      const result = await htmlFn(queryParams);
      const html = typeof result === "string" ? result : (result as any).html || JSON.stringify(result);
      const blob = new Blob([html], { type: "text/html" });
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
    } catch {
      toast.error(t("page.reports.export_failed"), t("page.reports.could_not_generate_html_report"));
    }
  }

  function handleEmail() {
    const recipients = emailRecipients.split(",").map((r) => r.trim()).filter(Boolean);
    if (recipients.length === 0) return;
    emailMutation.mutate({
      report_type: tab,
      recipients,
    });
  }

  const tabs = [
    { key: "tasks", label: t("nav.tasks") },
    { key: "usage", label: t("page.api_keys.usage") },
    { key: "activity", label: t("nav.activity") },
  ];

  return (
    <div style={{ maxWidth: 1060, margin: "0 auto" }}>
      <PageHeader
        title={t("page.reports.reports")}
        subtitle={t("page.reports.generate_and_export_operational_reports")}
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <button
              onClick={() => setEmailModalOpen(true)}
              className="btn-manor-outline"
              style={{ fontSize: 13, padding: "8px 16px", borderRadius: 12, display: "flex", alignItems: "center", gap: 8 }}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
              </svg>
              {t("page.reports.email_report")}
            </button>
            <button
              onClick={handleExportHtml}
              className="btn-manor-outline"
              style={{ fontSize: 13, padding: "8px 16px", borderRadius: 12, display: "flex", alignItems: "center", gap: 8 }}
            >
              <IconDownload size={16} />
              {t("page.reports.export_html")}
            </button>
            <button
              onClick={handleGenerate}
              className="btn-manor"
              style={{ fontSize: 13, padding: "8px 16px", borderRadius: 12, display: "flex", alignItems: "center", gap: 8 }}
            >
              <IconRefresh size={16} />
              {t("page.qr.generate")}
            </button>
          </div>
        }
      >
        <TabSwitcher tabs={tabs} value={tab} onChange={(k) => setTab(k as ReportTab)} />
      </PageHeader>

      {/* Date range */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
        <div>
          <label style={{ display: "block", fontSize: 11, fontWeight: 600, color: "#78716c", marginBottom: 4 }}>{t("page.reports.from")}</label>
          <input
            type="date"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
            className="manor-input"
            style={{ fontSize: 13 }}
          />
        </div>
        <div>
          <label style={{ display: "block", fontSize: 11, fontWeight: 600, color: "#78716c", marginBottom: 4 }}>{t("page.reports.to")}</label>
          <input
            type="date"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
            className="manor-input"
            style={{ fontSize: 13 }}
          />
        </div>
      </div>

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}

      {/* Empty */}
      {!isLoading && !currentReport && (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
            </svg>
          }
          title={t("page.reports.no_report_data")}
          description={t("page.reports.click_generate_to_create_a_report_for_the_select")}
        />
      )}

      {/* Report content */}
      {!isLoading && currentReport && (
        <div className="glass-card" style={{ padding: 24 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
            <h3 style={{ fontSize: 16, fontWeight: 700, color: "#1c1917", margin: 0 }}>
              {(currentReport as Report).title || `${tab.charAt(0).toUpperCase() + tab.slice(1)} Report`}
            </h3>
            <span style={{ fontSize: 11, color: "#a8a29e" }}>
              {t("page.reports.generated")} {(currentReport as Report).generated_at ? new Date((currentReport as Report).generated_at).toLocaleString() : "N/A"}
            </span>
          </div>

          {/* Render report data as a formatted table */}
          {renderReportData((currentReport as Report).data)}
        </div>
      )}

      {/* Email modal */}
      <Modal
        open={emailModalOpen}
        onClose={() => setEmailModalOpen(false)}
        title={t("page.reports.email_report")}
        footer={
          <>
            <Button variant="outline" onClick={() => setEmailModalOpen(false)}>{t("action.cancel")}</Button>
            <Button
              variant="primary"
              onClick={handleEmail}
              disabled={!emailRecipients.trim() || emailMutation.isPending}
            >
              {emailMutation.isPending ? t("page.messages.sending") : t("page.reports.send_report")}
            </Button>
          </>
        }
      >
        <div>
          <Input
            label={t("page.reports.recipients_comma_separated")}
            value={emailRecipients}
            onChange={(e) => setEmailRecipients(e.target.value)}
            placeholder={t("page.reports.user_example_com_admin_example_com")}
          />
          <p style={{ fontSize: 11, color: "#a8a29e", marginTop: 8 }}>
            {t("page.reports.the")} {tab} {t("page.reports.report_for")} {dateFrom} {t("page.reports.to_2")} {dateTo} {t("page.reports.will_be_sent_to_these_email_addresses")}
          </p>
        </div>
      </Modal>
    </div>
  );
}

/* -- helper to render report data ----------------------------- */

function renderReportData(data: Record<string, any> | undefined) {
  if (!data || Object.keys(data).length === 0) {
    return <p style={{ fontSize: 13, color: "#a8a29e" }}>{t("page.reports.no_data_available")}</p>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {Object.entries(data).map(([key, value]) => {
        // If value is an array, render as a table
        if (Array.isArray(value) && value.length > 0 && typeof value[0] === "object") {
          const cols = Object.keys(value[0]);
          return (
            <div key={key}>
              <h4 style={{ fontSize: 13, fontWeight: 700, color: "#44403c", textTransform: "capitalize", marginBottom: 8 }}>
                {key.replace(/_/g, " ")}
              </h4>
              <div style={{ overflowX: "auto" }}>
                <table className="glass-table" style={{ width: "100%", fontSize: 12 }}>
                  <thead>
                    <tr>
                      {cols.map((col) => (
                        <th key={col} style={{ textTransform: "capitalize" }}>{col.replace(/_/g, " ")}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {value.map((row: any, i: number) => (
                      <tr key={i}>
                        {cols.map((col) => (
                          <td key={col} style={{ padding: "8px 12px", color: "#57534e" }}>
                            {formatReportValue(row[col])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        }

        // If value is a simple object, render as key-value pairs
        if (typeof value === "object" && value !== null && !Array.isArray(value)) {
          return (
            <div key={key}>
              <h4 style={{ fontSize: 13, fontWeight: 700, color: "#44403c", textTransform: "capitalize", marginBottom: 8 }}>
                {key.replace(/_/g, " ")}
              </h4>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 200px), 1fr))", gap: 12 }}>
                {Object.entries(value).map(([k, v]) => (
                  <div key={k} style={{ background: "#fafaf9", borderRadius: 12, padding: "12px 16px" }}>
                    <div style={{ fontSize: 11, fontWeight: 600, color: "#a8a29e", textTransform: "capitalize", marginBottom: 4 }}>
                      {k.replace(/_/g, " ")}
                    </div>
                    <div style={{ fontSize: 18, fontWeight: 700, color: "#1c1917" }}>
                      {renderReportValue(v)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        }

        // Simple value
        return (
          <div key={key} style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 0", borderBottom: "1px solid #f5f5f4" }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: "#57534e", textTransform: "capitalize", minWidth: 140 }}>
              {key.replace(/_/g, " ")}
            </span>
            <span style={{ fontSize: 13, color: "#292524" }}>
              {formatReportValue(value)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function formatReportLabel(key: string) {
  return key.replace(/_/g, " ");
}

function formatReportValue(value: any): string {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "number") return value.toLocaleString();
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    if (value.length === 0) return "None";
    if (value.every((item) => item === null || typeof item !== "object")) {
      return value.map(formatReportValue).join(", ");
    }
    return `${value.length.toLocaleString()} ${value.length === 1 ? "item" : "items"}`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) return "--";
    const visible = entries.slice(0, 4).map(([k, v]) => `${formatReportLabel(k)}: ${formatReportValue(v)}`);
    if (entries.length > visible.length) visible.push(`+${entries.length - visible.length} more`);
    return visible.join(" · ");
  }
  return String(value);
}

function renderReportValue(value: any) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return formatReportValue(value);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, fontSize: 12, lineHeight: 1.45 }}>
      {Object.entries(value).slice(0, 5).map(([k, v]) => (
        <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
          <span style={{ color: "#78716c", fontWeight: 600, textTransform: "capitalize" }}>{formatReportLabel(k)}</span>
          <span style={{ color: "#1c1917", fontWeight: 700, textAlign: "right" }}>{formatReportValue(v)}</span>
        </div>
      ))}
      {Object.keys(value).length > 5 && (
        <span style={{ color: "#a8a29e", fontWeight: 600 }}>+{Object.keys(value).length - 5} more</span>
      )}
    </div>
  );
}
