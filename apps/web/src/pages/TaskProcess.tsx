/**
 * Public task processing page — no auth required.
 * Staff access via: /task/process?code={session_code}
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import StatusBadge from "../components/ui/StatusBadge";
import { t } from "../lib/i18n";
import { formatDateOnly } from "../lib/format";

const API = "/api/v1/public/task";

async function fetchTask(code: string) {
  const res = await fetch(`${API}?code=${code}`);
  if (!res.ok) throw new Error(t("page.task_process.invalid_or_expired_link"));
  return res.json();
}

async function updateStatus(code: string, status?: string, comment?: string) {
  const res = await fetch(`${API}/update-status`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, status, comment }),
  });
  if (!res.ok) throw new Error(t("page.task_process.update_failed"));
  return res.json();
}

async function completeTask(code: string, notes?: string) {
  const res = await fetch(`${API}/complete`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, notes }),
  });
  if (!res.ok) throw new Error(t("page.task_process.complete_failed"));
  return res.json();
}

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending: { label: t("status.pending"), color: "#cf9b44" },
  scheduled: { label: t("status.scheduled"), color: "#5f84bd" },
  in_progress: { label: t("status.in_progress"), color: "#4869ac" },
  completed: { label: t("status.completed"), color: "#4f9c84" },
  cancelled: { label: t("status.cancelled"), color: "#78716c" },
};

export default function TaskProcess() {
  const [params] = useSearchParams();
  const code = params.get("code") || "";
  const [comment, setComment] = useState("");
  const [notes, setNotes] = useState("");
  const [success, setSuccess] = useState("");

  const { data: task, isLoading, error, refetch } = useQuery({
    queryKey: ["public-task", code],
    queryFn: () => fetchTask(code),
    enabled: !!code,
  });

  const statusMutation = useMutation({
    mutationFn: () => updateStatus(code, "in_progress", comment || undefined),
    onSuccess: () => { setComment(""); setSuccess(t("page.task_process.status_updated")); refetch(); },
  });

  const completeMutation = useMutation({
    mutationFn: () => completeTask(code, notes || undefined),
    onSuccess: () => { setNotes(""); setSuccess(t("page.task_process.task_marked_complete")); refetch(); },
  });

  if (!code) return <ErrorPage message={t("page.task_process.no_session_code")} />;
  if (isLoading) return <CenterBox><LoadingSpinner size={32} /></CenterBox>;
  if (error) return <ErrorPage message={t("page.task_process.link_invalid_or_expired")} />;

  const s = STATUS_LABELS[task?.status] || { label: task?.status, color: "#a8a29e" };
  const isCompleted = task?.status === "completed" || task?.status === "cancelled";

  return (
    <div style={{ minHeight: "100vh", background: "#fafaf9", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
      <div style={{ width: "100%", maxWidth: 520, background: "#fff", borderRadius: 28, boxShadow: "0 20px 60px rgba(0,0,0,0.08)", overflow: "hidden" }}>
        {/* Header */}
        <div style={{ background: "#292524", padding: "32px 28px", color: "#fff" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
            <div style={{ width: 36, height: 36, borderRadius: 10, background: "#436b65", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <svg width="18" height="18" viewBox="0 0 1024 1024" fill="#fff"><path d="M295.152941 0l224.376471 224.376471L743.905882 0H1024v63.247059L519.529412 567.717647 0 49.694118V0h295.152941zM0 256l243.952941 243.952941V1024H0V256z m1024 15.058824v752.941176H780.047059V515.011765L1024 271.058824z" /></svg>
            </div>
            <span style={{ fontSize: 18, fontWeight: 800 }}>{t("page.chat_history.manor_ai")}</span>
          </div>
          <h1 style={{ fontSize: 22, fontWeight: 800, margin: "0 0 8px" }}>{task.title}</h1>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 12px", borderRadius: 8, background: `${s.color}22`, color: s.color, fontSize: 12, fontWeight: 700 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: s.color }} />
              {s.label}
            </span>
            {task.deadline && (
              <span style={{ fontSize: 12, color: "#a8a29e" }}>{t("page.task_process.due")}: {formatDateOnly(task.deadline)}</span>
            )}
          </div>
        </div>

        {/* Body */}
        <div style={{ padding: "28px" }}>
          {success && (
            <div style={{ marginBottom: 16, padding: "10px 16px", borderRadius: 12, background: "rgba(79,156,132,0.08)", border: "1px solid rgba(79,156,132,0.2)", color: "#437f6b", fontSize: 13, fontWeight: 600 }}>
              {success}
            </div>
          )}

          {task.description && (
            <div style={{ marginBottom: 24 }}>
              <label style={{ fontSize: 11, fontWeight: 700, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em" }}>{t("page.task_process.description")}</label>
              <p style={{ fontSize: 14, color: "#44403c", lineHeight: 1.6, margin: "6px 0 0" }}>{task.description}</p>
            </div>
          )}

          {!isCompleted ? (
            <>
              {/* Add comment / update */}
              <div style={{ marginBottom: 20 }}>
                <label style={{ fontSize: 11, fontWeight: 700, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em", display: "block", marginBottom: 6 }}>{t("page.task_process.add_update")}</label>
                <textarea
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder={t("page.task_process.update_placeholder")}
                  rows={3}
                  style={{ width: "100%", padding: "10px 14px", borderRadius: 12, border: "1px solid rgba(28,25,23,0.06)", fontSize: 14, resize: "vertical", outline: "none", fontFamily: "inherit", boxSizing: "border-box" }}
                />
                <button
                  onClick={() => statusMutation.mutate()}
                  disabled={statusMutation.isPending}
                  style={{ marginTop: 8, padding: "10px 20px", borderRadius: 10, background: "#436b65", color: "#fff", fontWeight: 700, fontSize: 13, border: "none", cursor: "pointer" }}
                >
                  {statusMutation.isPending ? t("page.task_process.updating") : task.status === "pending" ? t("page.task_process.start_and_add_update") : t("page.task_process.add_update")}
                </button>
              </div>

              {/* Complete task */}
              <div style={{ borderTop: "1px solid #f5f5f4", paddingTop: 20 }}>
                <label style={{ fontSize: 11, fontWeight: 700, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em", display: "block", marginBottom: 6 }}>{t("page.task_process.mark_complete")}</label>
                <textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder={t("page.task_process.completion_notes_placeholder")}
                  rows={2}
                  style={{ width: "100%", padding: "10px 14px", borderRadius: 12, border: "1px solid rgba(28,25,23,0.06)", fontSize: 14, resize: "vertical", outline: "none", fontFamily: "inherit", boxSizing: "border-box" }}
                />
                <button
                  onClick={() => completeMutation.mutate()}
                  disabled={completeMutation.isPending}
                  style={{ marginTop: 8, padding: "10px 20px", borderRadius: 10, background: "#4f9c84", color: "#fff", fontWeight: 700, fontSize: 13, border: "none", cursor: "pointer" }}
                >
                  {completeMutation.isPending ? t("page.task_process.completing") : t("page.task_process.mark_as_complete")}
                </button>
              </div>
            </>
          ) : (
            <div style={{ textAlign: "center", padding: "20px 0" }}>
              <div style={{ width: 56, height: 56, borderRadius: "50%", background: "#dceae3", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 12px" }}>
                <svg width="28" height="28" fill="none" viewBox="0 0 24 24" stroke="#437f6b" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
              </div>
              <p style={{ fontSize: 16, fontWeight: 700, color: "#292524", margin: "0 0 4px" }}>{t("page.task_process.task_completed")}</p>
              <p style={{ fontSize: 13, color: "#a8a29e" }}>{t("page.task_process.task_resolved")}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CenterBox({ children }: { children: React.ReactNode }) {
  return <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>{children}</div>;
}

function ErrorPage({ message }: { message: string }) {
  return (
    <CenterBox>
      <div style={{ textAlign: "center", maxWidth: 400, padding: 24 }}>
        <div style={{ width: 64, height: 64, borderRadius: "50%", background: "#f8f0ef", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
          <svg width="32" height="32" fill="none" viewBox="0 0 24 24" stroke="#d65f59" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" /></svg>
        </div>
        <h2 style={{ fontSize: 20, fontWeight: 800, color: "#292524", margin: "0 0 8px" }}>{t("page.task_process.link_invalid")}</h2>
        <p style={{ fontSize: 14, color: "#78716c" }}>{message}</p>
      </div>
    </CenterBox>
  );
}
