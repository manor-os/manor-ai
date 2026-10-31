import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type OwnedApprovalRequest } from "../lib/api";
import { t } from "../lib/i18n";
import Button from "../components/ui/Button";

// Why the owner's settings ask, by the policy's reason code. A code with no
// sentence here is not shown: it is a code, not an explanation.
const REASONS: Record<string, string> = {
  "approval.mode": "page.approval_request.reason_mode",
  "approval.rule": "page.approval_request.reason_rule",
  "approval.unknown_impact": "page.approval_request.reason_unknown",
};

/** One approval a user's own work outside any Workspace is waiting on
 *  (e.g. an agent acting on one of their own devices). Linked from the
 *  notification; only that user can answer it. */
export default function ApprovalRequest() {
  const { requestId = "" } = useParams();
  const [request, setRequest] = useState<OwnedApprovalRequest | null>(null);
  const [failed, setFailed] = useState(false);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let alive = true;
    api.approvalRequests.get(requestId)
      .then((loaded) => { if (alive) setRequest(loaded); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [requestId]);

  const decide = async (decision: "approve" | "reject") => {
    setPending(true);
    try {
      setRequest(await api.approvalRequests.decide(requestId, decision));
    } catch {
      setFailed(true);
    } finally {
      setPending(false);
    }
  };

  const images = (request?.payload?.review?.review_artifacts ?? []).filter((item) => item.kind === "image" && item.url);
  const open = request?.status === "pending";

  return (
    <div className="mx-auto max-w-2xl px-4 py-8">
      <div className="glass-panel p-6">
        <h1 className="text-lg font-semibold text-stone-900">{t("page.approval_request.title")}</h1>
        {failed && <p className="mt-3 text-sm text-stone-500">{t("page.approval_request.unavailable")}</p>}
        {request && (
          <>
            {request.reason && REASONS[request.reason] && (
              <p className="mt-2 text-sm text-stone-600">{t(REASONS[request.reason])}</p>
            )}
            <p className="mt-4 text-xs text-stone-500">
              {t("page.approval_request.action")}: <span className="mono">{request.action_key}</span>
            </p>
            {images.map((image, index) => (
              <figure key={`${image.url}-${index}`} className="mt-4">
                <img src={image.url} alt={image.title || t("page.approval_request.screen")} className="w-full rounded-xl" />
                <figcaption className="mt-1 text-xs text-stone-500">{image.title || t("page.approval_request.screen")}</figcaption>
              </figure>
            ))}
            {open ? (
              <div className="mt-6 flex gap-3">
                <Button variant="primary" loading={pending} disabled={pending} onClick={() => void decide("approve")}>
                  {t("page.approval_request.approve")}
                </Button>
                <Button variant="ghost" disabled={pending} onClick={() => void decide("reject")}>
                  {t("page.approval_request.reject")}
                </Button>
              </div>
            ) : (
              <p className="mt-6 text-sm text-stone-600">
                {request.status === "granted" || request.status === "consumed"
                  ? t("page.approval_request.approved")
                  : request.status === "denied"
                    ? t("page.approval_request.rejected")
                    : t("page.approval_request.already", { status: request.status })}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
