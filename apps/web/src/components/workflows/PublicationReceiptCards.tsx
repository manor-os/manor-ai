import { formatDateLong } from "../../lib/format";
import { t } from "../../lib/i18n";
import { openDetail } from "../../stores/detail";
import { IconChecklist, IconExternalLink } from "../icons";
import CompactCard from "../ui/CompactCard";
import IconTile from "../ui/IconTile";
import StatusPill from "../ui/StatusPill";
import type { WorkflowPublicationReceipt } from "./workflowRunDisplay";

function publishedHref(value?: string | null): string | null {
  const href = String(value || "").trim();
  return /^https?:\/\//i.test(href) ? href : null;
}

function receiptIdentity(receipt: WorkflowPublicationReceipt): string {
  return receipt.external_id || receipt.payload_hash;
}

function openReceiptDetail(receipt: WorkflowPublicationReceipt): void {
  const verified = receipt.verification_status === "verified";
  const href = publishedHref(receipt.published_url);
  openDetail({
    key: `publication-receipt:${receipt.platform}:${receiptIdentity(receipt)}`,
    icon: (
      <IconTile
        size={48}
        status={{
          color: verified ? "var(--success)" : "var(--warning)",
          label: verified
            ? t("component.publication_receipt.verified")
            : t("component.publication_receipt.not_verified"),
        }}
      >
        <IconChecklist size={22} />
      </IconTile>
    ),
    title: receipt.title || receipt.platform,
    subtitle: t("component.publication_receipt.detail_subtitle", {
      platform: receipt.platform,
    }),
    badges: (
      <StatusPill
        status={verified ? "completed" : "blocked"}
        label={verified
          ? t("component.publication_receipt.verified")
          : t("component.publication_receipt.not_verified")}
        size="sm"
      />
    ),
    body: (
      <div className="publication-receipt-detail">
        <dl className="publication-receipt-detail__grid">
          <div>
            <dt>{t("component.publication_receipt.platform")}</dt>
            <dd>{receipt.platform}</dd>
          </div>
          <div>
            <dt>{t("component.publication_receipt.published_at")}</dt>
            <dd className="mono">{formatDateLong(receipt.published_at)}</dd>
          </div>
          <div>
            <dt>{t("component.publication_receipt.external_id")}</dt>
            <dd className="mono">{receipt.external_id || "—"}</dd>
          </div>
          <div>
            <dt>{t("component.publication_receipt.fallback")}</dt>
            <dd>{receipt.fallback_used || "none"}</dd>
          </div>
          <div className="publication-receipt-detail__wide">
            <dt>{t("component.publication_receipt.payload_hash")}</dt>
            <dd className="mono">{receipt.payload_hash}</dd>
          </div>
        </dl>
        <details className="publication-receipt-detail__evidence">
          <summary>
            {t("component.publication_receipt.evidence_count", {
              count: receipt.evidence?.length || 0,
            })}
          </summary>
          <pre>{JSON.stringify(receipt.evidence || [], null, 2)}</pre>
        </details>
        <details className="publication-receipt-detail__evidence">
          <summary>
            {t("component.publication_receipt.attempt_count", {
              count: receipt.attempts?.length || 0,
            })}
          </summary>
          <pre>{JSON.stringify(receipt.attempts || [], null, 2)}</pre>
        </details>
      </div>
    ),
    primaryAction: href ? {
      label: t("component.publication_receipt.open_publication"),
      icon: <IconExternalLink size={15} />,
      onClick: () => window.open(href, "_blank", "noopener,noreferrer"),
    } : undefined,
  });
}

export default function PublicationReceiptCards({
  receipts,
}: {
  receipts?: WorkflowPublicationReceipt[];
}) {
  if (!receipts?.length) return null;
  return (
    <section
      className="publication-receipts"
      aria-labelledby="publication-receipts-title"
    >
      <header className="publication-receipts__heading">
        <div>
          <span>{t("component.publication_receipt.outcome")}</span>
          <h3 id="publication-receipts-title">
            {t("component.publication_receipt.title")}
          </h3>
        </div>
        <span className="mono">
          {t("component.publication_receipt.verified_count", {
            count: receipts.filter((item) => item.verification_status === "verified").length,
            total: receipts.length,
          })}
        </span>
      </header>
      <div className="publication-receipts__list">
        {receipts.map((receipt) => {
          const verified = receipt.verification_status === "verified";
          return (
            <CompactCard
              key={`${receipt.platform}:${receiptIdentity(receipt)}`}
              icon={(
                <IconTile
                  size={34}
                  status={{
                    color: verified ? "var(--success)" : "var(--warning)",
                    label: verified
                      ? t("component.publication_receipt.verified")
                      : t("component.publication_receipt.not_verified"),
                  }}
                >
                  <IconChecklist size={17} />
                </IconTile>
              )}
              title={receipt.title || receipt.platform}
              subtitle={receipt.published_url || receipt.external_id || receipt.payload_hash}
              meta={verified
                ? t("component.publication_receipt.verified")
                : t("component.publication_receipt.not_verified")}
              metaTone={verified ? "connected" : "muted"}
              onClick={() => openReceiptDetail(receipt)}
            />
          );
        })}
      </div>
    </section>
  );
}
