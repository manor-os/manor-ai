import { FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { DocumentSignatureRequest } from "../../lib/types";
import { normalizeSignatureFields, type DocumentSignatureField } from "../../lib/signatureFields";
import { useToastStore } from "../../stores/toast";
import { IconCopy, IconRefresh, IconTrash } from "../icons";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Modal from "../ui/Modal";
import DefaultEmailSender from "../integrations/DefaultEmailSender";
import type { EmailAccount } from "../../lib/emailAccounts";
import Textarea from "../ui/Textarea";
import SignatureFieldEditor from "./SignatureFieldEditor";
import "./signature.css";
import { getPreferredTimeZone } from "../../lib/format";

interface SignatureRequestDialogProps {
  open: boolean;
  onClose: () => void;
  documentId: string;
  documentName: string;
}

function formatDate(value?: string) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: getPreferredTimeZone(),
  }).format(new Date(value));
}

function requestStatusLabel(request: DocumentSignatureRequest) {
  if (request.status === "completed" && request.group_id && !request.group_finalized) {
    return t("signature.group.signed_waiting");
  }
  return t(`signature.status.${request.status}`);
}

function recipientEmails(value: string): string[] {
  return [...new Set(value.split(",").map((email) => email.trim().toLowerCase()).filter(Boolean))];
}

export default function SignatureRequestDialog({
  open,
  onClose,
  documentId,
  documentName,
}: SignatureRequestDialogProps) {
  const toast = useToastStore();
  const navigate = useNavigate();
  const [requests, setRequests] = useState<DocumentSignatureRequest[]>([]);
  const [senders, setSenders] = useState<EmailAccount[]>([]);
  const [senderLoading, setSenderLoading] = useState(true);
  const [senderError, setSenderError] = useState<string>();
  const [selectedSenderId, setSelectedSenderId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [recipientName, setRecipientName] = useState("");
  const [recipientEmail, setRecipientEmail] = useState("");
  const [message, setMessage] = useState("");
  const [fields, setFields] = useState<DocumentSignatureField[]>([]);
  const [additionalRecipients, setAdditionalRecipients] = useState<Array<{
    name: string; email: string; fields: DocumentSignatureField[];
  }>>([]);
  const [selectedSigner, setSelectedSigner] = useState(0);
  const [step, setStep] = useState<"fields" | "review">("fields");
  const [previewReady, setPreviewReady] = useState(false);
  const [lastUrl, setLastUrl] = useState<string>();

  const activeRequest = useMemo(
    () => requests.find((request) => request.status === "pending"),
    [requests],
  );
  const completedRequest = useMemo(
    () => requests.find((request) => request.status === "completed"),
    [requests],
  );
  const unfinishedGroup = requests.find((request) => request.group_id && !request.group_finalized && request.status !== "revoked");
  const cancelledPartialGroup = requests.find((request) => request.group_id && request.status === "revoked" && (request.group_signed_count || 0) > 0);
  const preparing = !loading && !activeRequest && !completedRequest && !unfinishedGroup && !cancelledPartialGroup;
  const signerFields = selectedSigner === 0 ? fields : additionalRecipients[selectedSigner - 1]?.fields || [];
  const allSignerFields = [fields, ...additionalRecipients.map((recipient) => recipient.fields)];
  const validFields = allSignerFields.every((signer) => signer.some((field) => field.type === "signature" && field.required)
    && signer.every((field) => field.label.trim()));
  const validRecipients = Boolean(recipientName.trim() && recipientEmail.trim())
    && additionalRecipients.every((recipient) => recipient.name.trim() && recipient.email.trim());

  function updateSignerFields(next: DocumentSignatureField[]) {
    if (selectedSigner === 0) setFields(next);
    else setAdditionalRecipients((current) => current.map((recipient, index) =>
      index === selectedSigner - 1 ? { ...recipient, fields: next } : recipient));
  }

  useEffect(() => {
    setFields([]);
    setAdditionalRecipients([]);
    setSelectedSigner(0);
    setStep("fields");
    setRecipientName("");
    setRecipientEmail("");
    setMessage("");
    setPreviewReady(false);
    setSelectedSenderId("");
  }, [documentId]);

  async function loadSenders() {
    setSenderLoading(true);
    setSenderError(undefined);
    try {
      const available = await api.docPermissions.listSignatureSenders(documentId);
      setSenders(available);
      setSelectedSenderId(available.find((account) => account.is_default)?.id || "");
    } catch (caught: any) {
      setSenders([]);
      setSelectedSenderId("");
      setSenderError(caught?.message || t("signature.sender.load_error"));
    } finally {
      setSenderLoading(false);
    }
  }

  async function loadRequests() {
    setLoading(true);
    setError(undefined);
    try {
      setRequests(await api.docPermissions.listSignatureRequests(documentId));
    } catch (caught: any) {
      setError(caught?.message || t("signature.error.load"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open) return;
    setLastUrl(undefined);
    void loadRequests();
    void loadSenders();
  }, [open, documentId]);

  async function createRequest(event: FormEvent) {
    event.preventDefault();
    if (busy || !previewReady || !validFields || (step === "review" && !selectedSenderId)) return;
    if (step === "fields") {
      setStep("review");
      setError(undefined);
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      const emails = recipientEmails(recipientEmail);
      if (emails.length + additionalRecipients.reduce((total, recipient) => total + recipientEmails(recipient.email).length, 0) > 10) {
        throw new Error(t("signature.recipient_email_limit"));
      }
      const created = await api.docPermissions.createSignatureRequest(documentId, {
        recipient_name: recipientName.trim(),
        recipient_email: emails[0],
        recipient_emails: emails,
        message: message.trim() || undefined,
        expires_in_days: 7,
        sender_integration_id: selectedSenderId,
        fields: fields.map((field) => ({ ...field, label: field.label.trim() })),
        additional_recipients: additionalRecipients.map((recipient) => ({
          recipient_name: recipient.name.trim(),
          recipient_email: recipientEmails(recipient.email)[0],
          recipient_emails: recipientEmails(recipient.email),
          fields: recipient.fields.map((field) => ({ ...field, label: field.label.trim() })),
        })),
      });
      setLastUrl(additionalRecipients.length ? undefined : created.url || undefined);
      await loadRequests();
      if (created.delivery_status === "failed" || (created.group_delivery_failed_count || 0) > 0) {
        toast.warning(t("signature.created_delivery_failed"), t("signature.copy_link_hint"));
      } else {
        toast.success(t("signature.sent"), additionalRecipients.length
          ? t("signature.group.sent", { count: additionalRecipients.length + 1 })
          : recipientEmail.trim());
      }
    } catch (caught: any) {
      setError(caught?.message || t("signature.error.create"));
    } finally {
      setBusy(false);
    }
  }

  async function resend(request: DocumentSignatureRequest) {
    setBusy(true);
    setError(undefined);
    try {
      const resent = await api.docPermissions.resendSignatureRequest(documentId, request.id);
      setLastUrl(resent.url || undefined);
      await loadRequests();
      if (resent.delivery_status === "failed") {
        toast.warning(t("signature.created_delivery_failed"), t("signature.copy_link_hint"));
      } else {
        toast.success(t("signature.resent"), (resent.recipient_emails || [resent.recipient_email]).join(", "));
      }
    } catch (caught: any) {
      setError(caught?.message || t("signature.error.resend"));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(request: DocumentSignatureRequest) {
    setBusy(true);
    setError(undefined);
    try {
      await api.docPermissions.revokeSignatureRequest(documentId, request.id);
      setLastUrl(undefined);
      await loadRequests();
      toast.success(t("signature.revoked"));
    } catch (caught: any) {
      setError(caught?.message || t("signature.error.revoke"));
    } finally {
      setBusy(false);
    }
  }

  async function copyLink() {
    if (!lastUrl) return;
    try {
      await navigator.clipboard.writeText(lastUrl);
      toast.success(t("signature.link_copied"));
    } catch {
      toast.error(t("signature.error.copy"));
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("signature.dialog_title")}
      className="signature-flow signature-dialog"
      maxWidth={preparing && senders.length > 0 ? "1400px" : "680px"}
      height={preparing && senders.length > 0 ? "calc(100dvh - 48px)" : undefined}
      bodyClassName={preparing && senders.length > 0 ? "flex min-h-0 flex-1 flex-col gap-3 !overflow-hidden" : "space-y-5"}
    >
      <div className="min-w-0 shrink-0 pb-1">
        <p className="truncate text-sm font-medium text-[var(--text-strong)]" title={documentName}>{documentName}</p>
        <p className="mt-1 text-xs leading-relaxed text-stone-500">{preparing ? t(step === "fields" ? "signature.fields.prepare_hint" : "signature.fields.review_hint") : t("signature.security_hint")}</p>
      </div>

      {error && (
        <div role="alert" className="shrink-0 py-1 text-sm text-[var(--chip-danger-fg)]">
          {error}
        </div>
      )}
      {senderError && <p role="alert" className="text-sm text-[var(--chip-danger-fg)]">{senderError}</p>}

      {cancelledPartialGroup && (
        <p role="status" className="text-sm text-stone-600">{t("signature.group.cancelled_copy")}</p>
      )}

      {lastUrl && (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 py-1">
          <p className="min-w-0 flex-1 text-xs leading-relaxed text-stone-700">{t("signature.copy_link_hint")}</p>
          <Button variant="ghost" size="sm" onClick={() => void copyLink()}>
            <span className="inline-flex items-center gap-1.5"><IconCopy size={14} />{t("signature.copy_link")}</span>
          </Button>
        </div>
      )}

      {preparing && senderLoading && <p className="py-8 text-center text-sm text-stone-500">{t("signature.sender.loading")}</p>}
      {preparing && !senderLoading && senderError && (
        <Button variant="outline" onClick={() => void loadSenders()}>{t("signature.sender.retry")}</Button>
      )}
      {preparing && !senderLoading && senders.length === 0 && !senderError && (
        <div className="space-y-4 py-5">
          <p className="text-sm text-stone-600">{t("signature.sender.connect_hint")}</p>
          <Button onClick={() => { onClose(); navigate("/integrations?provider=email"); }}>{t("signature.sender.connect")}</Button>
        </div>
      )}
      {preparing && !senderLoading && senders.length > 0 && (
        <form className="flex min-h-0 flex-1 flex-col gap-3" onSubmit={createRequest}>
          <div className="flex shrink-0 flex-wrap items-center gap-2" role="group" aria-label={t("signature.group.signers")}>
            {[0, ...additionalRecipients.map((_, index) => index + 1)].map((index) => (
              <Button key={index} type="button" variant={selectedSigner === index ? "outline" : "ghost"} size="sm"
                ariaLabel={t("signature.group.signer", { number: index + 1 })}
                onClick={() => { setSelectedSigner(index); setPreviewReady(false); }}>
                {index === 0 ? (recipientName || t("signature.group.signer", { number: 1 }))
                  : (additionalRecipients[index - 1].name || t("signature.group.signer", { number: index + 1 }))}
              </Button>
            ))}
            {step === "fields" && additionalRecipients.length < 9 && (
              <Button type="button" variant="ghost" size="sm" onClick={() => {
                setAdditionalRecipients((current) => [...current, { name: "", email: "", fields: [] }]);
                setSelectedSigner(additionalRecipients.length + 1);
                setPreviewReady(false);
              }}>{t("signature.group.add_signer")}</Button>
            )}
            {step === "fields" && selectedSigner > 0 && (
              <Button type="button" variant="ghost" size="sm" onClick={() => {
                setAdditionalRecipients((current) => current.filter((_, index) => index !== selectedSigner - 1));
                setSelectedSigner(0);
                setPreviewReady(false);
              }}>{t("signature.group.remove_signer")}</Button>
            )}
          </div>
          <SignatureFieldEditor
            key={selectedSigner}
            documentId={documentId}
            fields={signerFields}
            onChange={updateSignerFields}
            onReadyChange={setPreviewReady}
            readOnly={step === "review"}
            sidebar={step === "review" ? <div className="space-y-4">
              <DefaultEmailSender account={senders.find((sender) => sender.id === selectedSenderId)} />
              <h3 className="signature-section-title">{t("signature.fields.recipient")}</h3>
              {additionalRecipients.length > 0 && <p className="text-xs text-stone-500">{t("signature.group.signer", { number: 1 })}</p>}
              <Input
                label={t("signature.recipient_name")}
                value={recipientName}
                onChange={(event) => setRecipientName(event.target.value)}
                autoComplete="name"
                maxLength={120}
                required
                disabled={busy}
              />
              <Input
                label={t("signature.recipient_emails")}
                hint={t("signature.recipient_emails_hint")}
                value={recipientEmail}
                onChange={(event) => setRecipientEmail(event.target.value)}
                type="email"
                multiple
                autoComplete="email"
                maxLength={3210}
                required
                disabled={busy}
              />
              {additionalRecipients.map((recipient, index) => (
                <div key={index} className="space-y-3 rounded-[var(--radius-control)] bg-[var(--surface-muted)] p-3">
                  <p className="text-xs font-medium text-[var(--text-strong)]">{t("signature.group.signer", { number: index + 2 })}</p>
                  <Input label={t("signature.recipient_name")} value={recipient.name}
                    onChange={(event) => setAdditionalRecipients((current) => current.map((item, itemIndex) =>
                      itemIndex === index ? { ...item, name: event.target.value } : item))}
                    maxLength={120} required disabled={busy} />
                  <Input label={t("signature.recipient_emails")} hint={t("signature.recipient_emails_hint")}
                    value={recipient.email} type="email" multiple
                    onChange={(event) => setAdditionalRecipients((current) => current.map((item, itemIndex) =>
                      itemIndex === index ? { ...item, email: event.target.value } : item))}
                    maxLength={3210} required disabled={busy} />
                  <p className="text-xs text-stone-500">{t("signature.fields.count", { count: recipient.fields.length })}</p>
                </div>
              ))}
              <Textarea
                label={t("signature.message")}
                ariaLabel={t("signature.message")}
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                rows={3}
                placeholder={t("signature.message_placeholder")}
                disabled={busy}
              />
              <div className="space-y-2 text-xs leading-relaxed text-stone-500">
                <p>{t("signature.security_hint")}</p>
                <p>{t("signature.fields.expiry_hint")}</p>
              </div>
              <div className="pt-2">
                <h3 className="signature-section-title">{t("signature.fields.count", { count: signerFields.length })}</h3>
                <div className="mt-3 space-y-2">{signerFields.map((field) => <p key={field.id} className="flex justify-between gap-3 text-xs text-stone-600"><span className="truncate">{field.label}{field.required ? " *" : ""}</span><span className="shrink-0 font-mono">{t("signature.fields.page_short", { page: field.page })}</span></p>)}</div>
              </div>
            </div> : undefined}
          />
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 pt-1">
            <p className="text-xs text-stone-500">{!validFields ? t("signature.fields.signature_required") : t("signature.fields.count", { count: signerFields.length })}</p>
            <div className="flex flex-wrap items-center gap-2">
              {step === "review" && <Button variant="ghost" disabled={busy} onClick={() => setStep("fields")}>{t("signature.fields.back")}</Button>}
              <Button type="submit" loading={busy} disabled={!validFields || !previewReady || (step === "review" && (!validRecipients || !selectedSenderId))}>
                {t(step === "fields" ? "signature.fields.preview_send" : "signature.send")}
              </Button>
            </div>
          </div>
        </form>
      )}

      {loading && <p className="py-8 text-center text-sm text-stone-500">{t("status.loading")}</p>}

      {!loading && requests.length > 0 && (
        <details open={preparing ? undefined : true} className={`space-y-3 ${preparing ? "max-h-40 shrink-0 overflow-auto" : ""}`}>
          <summary className="signature-section-title cursor-pointer">{t("signature.history")}</summary>
          {requests.find((item) => item.group_id) && (
            <p className="mt-2 text-xs text-stone-600" role="status">
              {t("signature.group.progress", {
                signed: requests.find((item) => item.group_id)?.group_signed_count || 0,
                total: requests.find((item) => item.group_id)?.group_size || 1,
              })}
            </p>
          )}
          {requests.map((request) => (
            <div key={request.id} className="py-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-[var(--text-strong)]">{request.recipient_name}</p>
                  <p className="mt-1 break-all text-xs text-stone-500">{(request.recipient_emails || [request.recipient_email]).join(", ")}</p>
                </div>
                <span className="inline-flex items-center gap-1.5 text-xs text-stone-700">
                  <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" />
                  {requestStatusLabel(request)}
                </span>
              </div>
              <div className="mt-3 grid gap-2 text-xs text-stone-500 sm:grid-cols-2">
                <p>{t("signature.delivery")}: {t(`signature.delivery.${request.delivery_status}`)}</p>
                <p>{request.status === "completed" ? t("signature.signed_at") : t("signature.expires_at")}: {formatDate(request.signed_at || request.expires_at)}</p>
                {request.signed_by_email && <p className="break-all">{t("signature.signed_by_email", { email: request.signed_by_email })}</p>}
                {request.viewed_at && <p>{t("signature.viewed_at")}: {formatDate(request.viewed_at)}</p>}
                <p>{t("signature.fields.count", { count: normalizeSignatureFields(request).length })}</p>
              </div>
              {request.delivery_error && <p className="mt-2 text-xs text-[var(--chip-danger-fg)]">{request.delivery_error}</p>}
              {(request.status === "pending" || (request.status === "expired" && Boolean(request.group_id))) && (
                <div className="mt-3 flex flex-wrap justify-end gap-2">
                  {request.status === "pending" && <Button variant="ghost" size="sm" disabled={busy} onClick={() => void resend(request)}>
                    <span className="inline-flex items-center gap-1.5"><IconRefresh size={14} />{t("signature.resend")}</span>
                  </Button>}
                  <Button variant="ghost" size="sm" className="signature-revoke" disabled={busy} onClick={() => void revoke(request)}>
                    <span className="inline-flex items-center gap-1.5"><IconTrash size={14} />{t("signature.revoke")}</span>
                  </Button>
                </div>
              )}
            </div>
          ))}
        </details>
      )}
    </Modal>
  );
}
