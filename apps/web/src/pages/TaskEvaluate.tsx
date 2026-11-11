import { t } from "../lib/i18n";
/**
 * Public task evaluation page — no auth required.
 * Customer access via: /task/evaluate?code={session_code}
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import { AuthBrand } from "../components/auth/AuthShell";
import { IconStar } from "../components/icons";
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
    <div style={{ minHeight: "100vh", background: "var(--surface-panel)", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
      <div style={{ width: "100%", maxWidth: 420 }}>
        <div style={{ marginBottom: 32 }}><AuthBrand /></div>
        {submitted ? (
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 600, color: "var(--text-strong)", margin: "0 0 8px" }}>{t("page.task_evaluate.thank_you")}</h1>
            <p style={{ fontSize: 14, color: "var(--text-muted)", margin: 0 }}>{t("page.task_evaluate.your_feedback_helps_us_improve_our_service")}</p>
          </div>
        ) : (
          <>
            <h1 style={{ fontSize: 22, fontWeight: 600, color: "var(--text-strong)", margin: "0 0 8px" }}>{t("page.task_evaluate.how_was_your_experience")}</h1>
            <p style={{ fontSize: 14, color: "var(--text-muted)", margin: "0 0 28px" }}>{task.title}</p>

            {evalMutation.error && (
              <p role="alert" style={{ margin: "0 0 16px", color: "var(--text-danger, #b91c1c)", fontSize: 13 }}>
                {(evalMutation.error as Error).message}
              </p>
            )}

            <div style={{ marginBottom: 28 }}>
              <div style={{ display: "flex", gap: 4, color: "var(--text-strong)" }}>
                {[1, 2, 3, 4, 5].map((s) => {
                  const active = s <= (hover || score);
                  return (
                    <button
                      key={s}
                      type="button"
                      aria-label={`${s}`}
                      aria-pressed={s === score}
                      onClick={() => setScore(s)}
                      onMouseEnter={() => setHover(s)}
                      onMouseLeave={() => setHover(0)}
                      style={{ width: 44, height: 44, border: "none", background: "transparent", cursor: "pointer", color: "inherit", display: "flex", alignItems: "center", justifyContent: "center", padding: 0 }}
                    >
                      <IconStar size={28} style={{ fill: active ? "currentColor" : "none", opacity: active ? 1 : 0.35 }} />
                    </button>
                  );
                })}
              </div>
              <p style={{ fontSize: 13, color: "var(--text-muted)", margin: "8px 0 0", minHeight: 18 }}>
                {score > 0
                  ? [
                      t("page.task_detail.evaluation.poor"),
                      t("page.task_detail.evaluation.fair"),
                      t("page.task_detail.evaluation.good"),
                      t("page.task_detail.evaluation.very_good"),
                      t("page.task_detail.evaluation.excellent"),
                    ][score - 1]
                  : t("page.task_evaluate.tap_to_rate")}
              </p>
            </div>

            <div style={{ marginBottom: 24 }}>
              <label htmlFor="task-evaluate-review" style={{ fontSize: 13, color: "var(--text-muted)", display: "block", marginBottom: 6 }}>{t("page.task_evaluate.your_feedback_optional")}</label>
              <textarea
                id="task-evaluate-review"
                className="manor-input"
                value={review}
                onChange={(e) => setReview(e.target.value)}
                placeholder={t("page.task_evaluate.tell_us_about_your_experience")}
                rows={3}
                style={{ width: "100%", minHeight: 88, resize: "vertical", boxSizing: "border-box" }}
              />
            </div>

            <button
              type="button"
              className="btn-manor btn-manor-lg"
              style={{ width: "100%" }}
              onClick={() => evalMutation.mutate()}
              disabled={score === 0 || evalMutation.isPending}
            >
              {evalMutation.isPending ? t("page.client_portal.submitting") : t("page.task_evaluate.submit_feedback")}
            </button>
          </>
        )}
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
      <h2 style={{ fontSize: 20, fontWeight: 600, color: "var(--text-strong)", margin: "0 0 8px" }}>{t("page.task_process.link_invalid")}</h2>
      <p style={{ fontSize: 14, color: "var(--text-muted)" }}>{message}</p>
    </div>
  );
}
