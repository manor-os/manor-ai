import { t } from "../lib/i18n";
/**
 * Public task evaluation page — no auth required.
 * Customer access via: /task/evaluate?code={session_code}
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import LoadingSpinner from "../components/ui/LoadingSpinner";

const API = "/api/v1/public/task";

async function fetchTask(code: string) {
  const res = await fetch(`${API}?code=${code}`);
  if (!res.ok) throw new Error(t("page.task_process.invalid_or_expired_link"));
  return res.json();
}

async function submitEvaluation(code: string, score: number, review: string) {
  const res = await fetch(`${API}/evaluate`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, score, review }),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(d.detail || t("page.task_evaluate.evaluation_failed"));
  }
  return res.json();
}

const STAR_COLORS = ["#d65f59", "#d3873f", "#c3a63f", "#54a176", "#4f9c84"];

export default function TaskEvaluate() {
  const [params] = useSearchParams();
  const code = params.get("code") || "";
  const preScore = parseInt(params.get("score") || "0");
  const [score, setScore] = useState(preScore || 0);
  const [hover, setHover] = useState(0);
  const [review, setReview] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const { data: task, isLoading, error } = useQuery({
    queryKey: ["public-task", code],
    queryFn: () => fetchTask(code),
    enabled: !!code,
  });

  const evalMutation = useMutation({
    mutationFn: () => submitEvaluation(code, score, review),
    onSuccess: () => setSubmitted(true),
  });

  if (!code) return <CenterBox><ErrorMsg message={t("page.task_evaluate.no_session_code_provided")} /></CenterBox>;
  if (isLoading) return <CenterBox><LoadingSpinner size={32} /></CenterBox>;
  if (error) return <CenterBox><ErrorMsg message={t("page.task_process.link_invalid_or_expired")} /></CenterBox>;

  return (
    <div style={{ minHeight: "100vh", background: "#fafaf9", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
      <div style={{ width: "100%", maxWidth: 480, background: "#fff", borderRadius: 28, boxShadow: "0 20px 60px rgba(0,0,0,0.08)", overflow: "hidden" }}>
        {/* Header */}
        <div style={{ background: "linear-gradient(135deg, #436b65, #5f928a)", padding: "32px 28px", color: "#fff", textAlign: "center" }}>
          <div style={{ width: 48, height: 48, borderRadius: 14, background: "rgba(255,255,255,0.2)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
            <svg width="24" height="24" viewBox="0 0 1024 1024" fill="#fff"><path d="M295.152941 0l224.376471 224.376471L743.905882 0H1024v63.247059L519.529412 567.717647 0 49.694118V0h295.152941zM0 256l243.952941 243.952941V1024H0V256z m1024 15.058824v752.941176H780.047059V515.011765L1024 271.058824z" /></svg>
          </div>
          <h1 style={{ fontSize: 20, fontWeight: 800, margin: "0 0 8px" }}>{t("page.task_evaluate.how_was_your_experience")}</h1>
          <p style={{ fontSize: 14, opacity: 0.8, margin: 0 }}>{t("page.task_evaluate.task")} {task.title}</p>
        </div>

        {/* Body */}
        <div style={{ padding: 28 }}>
          {submitted ? (
            <div style={{ textAlign: "center", padding: "24px 0" }}>
              <div style={{ fontSize: 48, marginBottom: 12 }}>{"⭐".repeat(score)}</div>
              <h2 style={{ fontSize: 20, fontWeight: 800, color: "#292524", margin: "0 0 8px" }}>{t("page.task_evaluate.thank_you")}</h2>
              <p style={{ fontSize: 14, color: "#78716c" }}>{t("page.task_evaluate.your_feedback_helps_us_improve_our_service")}</p>
            </div>
          ) : (
            <>
              {evalMutation.error && (
                <div style={{ marginBottom: 16, padding: "10px 16px", borderRadius: 12, background: "#f8f0ef", border: "1px solid rgba(214,95,89,0.2)", color: "#c14a44", fontSize: 13 }}>
                  {(evalMutation.error as Error).message}
                </div>
              )}

              {/* Star rating */}
              <div style={{ textAlign: "center", marginBottom: 28 }}>
                <p style={{ fontSize: 13, fontWeight: 600, color: "#78716c", marginBottom: 12 }}>{t("page.task_evaluate.tap_to_rate")}</p>
                <div style={{ display: "flex", justifyContent: "center", gap: 8 }}>
                  {[1, 2, 3, 4, 5].map((s) => {
                    const active = s <= (hover || score);
                    return (
                      <button
                        key={s}
                        onClick={() => setScore(s)}
                        onMouseEnter={() => setHover(s)}
                        onMouseLeave={() => setHover(0)}
                        style={{
                          width: 48, height: 48, borderRadius: 12, border: "none", cursor: "pointer",
                          background: active ? `${STAR_COLORS[Math.max(0, (hover || score) - 1)]}18` : "#fafaf9",
                          fontSize: 24, transition: "all 0.15s",
                          transform: active ? "scale(1.15)" : "scale(1)",
                        }}
                      >
                        {active ? "⭐" : "☆"}
                      </button>
                    );
                  })}
                </div>
                {score > 0 && (
                  <p style={{ fontSize: 13, fontWeight: 700, color: STAR_COLORS[score - 1], marginTop: 8 }}>
                    {[
                      t("page.task_detail.evaluation.poor"),
                      t("page.task_detail.evaluation.fair"),
                      t("page.task_detail.evaluation.good"),
                      t("page.task_detail.evaluation.very_good"),
                      t("page.task_detail.evaluation.excellent"),
                    ][score - 1]}
                  </p>
                )}
              </div>

              {/* Review text */}
              <div style={{ marginBottom: 24 }}>
                <label style={{ fontSize: 11, fontWeight: 700, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em", display: "block", marginBottom: 6 }}>{t("page.task_evaluate.your_feedback_optional")}</label>
                <textarea
                  value={review}
                  onChange={(e) => setReview(e.target.value)}
                  placeholder={t("page.task_evaluate.tell_us_about_your_experience")}
                  rows={3}
                  style={{ width: "100%", padding: "10px 14px", borderRadius: 12, border: "1px solid rgba(28,25,23,0.06)", fontSize: 14, resize: "vertical", outline: "none", fontFamily: "inherit", boxSizing: "border-box" }}
                />
              </div>

              {/* Submit */}
              <button
                onClick={() => evalMutation.mutate()}
                disabled={score === 0 || evalMutation.isPending}
                style={{
                  width: "100%", padding: 14, borderRadius: 14, border: "none", cursor: score === 0 ? "not-allowed" : "pointer",
                  background: score === 0 ? "#e7e5e4" : "#1c1917", color: score === 0 ? "#a8a29e" : "#fff",
                  fontWeight: 700, fontSize: 15, transition: "all 0.2s",
                }}
              >
                {evalMutation.isPending ? t("page.client_portal.submitting") : t("page.task_evaluate.submit_feedback")}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function CenterBox({ children }: { children: React.ReactNode }) {
  return <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>{children}</div>;
}

function ErrorMsg({ message }: { message: string }) {
  return (
    <div style={{ textAlign: "center", maxWidth: 400, padding: 24 }}>
      <div style={{ width: 64, height: 64, borderRadius: "50%", background: "#f8f0ef", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" }}>
        <svg width="32" height="32" fill="none" viewBox="0 0 24 24" stroke="#d65f59" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" /></svg>
      </div>
      <h2 style={{ fontSize: 20, fontWeight: 800, color: "#292524", margin: "0 0 8px" }}>{t("page.task_process.link_invalid")}</h2>
      <p style={{ fontSize: 14, color: "#78716c" }}>{message}</p>
    </div>
  );
}
