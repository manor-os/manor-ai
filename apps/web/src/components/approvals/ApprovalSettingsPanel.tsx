import { useEffect, useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../lib/api";
import { approvalKeys, useApprovalSettings } from "../../lib/approvalQueries";
import { approvalDraft, type ApprovalDraft, type ApprovalMode, type ApprovalPreview, type ApprovalScope, type ApprovalSnapshot } from "../../lib/approvalTypes";
import { t } from "../../lib/i18n";
import { useAuthStore } from "../../stores/auth";
import { openDetail, useDetailStore } from "../../stores/detail";
import Button from "../ui/Button";
import ConfirmDialog from "../ui/ConfirmDialog";
import RadioCard from "../ui/RadioCard";
import ApprovalRuleEditor from "./ApprovalRuleEditor";
import "./approvals.css";
import { PageLoading } from "../ui/Skeleton";

const modes: ApprovalMode[] = ["confirm_changes", "risk_based", "automatic"];
const drafts = new Map<string, { baseline: ApprovalSnapshot; draft: ApprovalDraft }>();
// Drafts are private, in-memory only, and cannot cross an account/actor switch.
useAuthStore.subscribe((state, previous) => {
  if (state.user?.id !== previous.user?.id || state.user?.entity_id !== previous.user?.entity_id) drafts.clear();
});
export const approvalScopeLabel = (scope: ApprovalScope) => t(`approval_settings.${scope.kind}`);
export function approvalInheritanceLabel(snapshot: ApprovalSnapshot) {
  if (snapshot.scope.kind === "account") return t("approval_settings.account");
  return t(snapshot.policy.mode !== null ? "approval_settings.custom_mode" : snapshot.policy.rules.length ? "approval_settings.rules_only" : "approval_settings.following");
}
export function openApprovalSettings(scope: ApprovalScope, label?: string) {
  const key = `approval:${scope.kind}:${scope.id || "personal"}`;
  openDetail({ key, width: 460, title: t("approval_settings.title"), subtitle: label || approvalScopeLabel(scope), body: <ApprovalSettingsPanel scope={scope} presentation="drawer" detailKey={key} /> });
}
export default function ApprovalSettingsPanel(props: { scope: ApprovalScope; presentation?: "page" | "drawer"; detailKey?: string; showOverrides?: boolean }) {
  const user = useAuthStore(state => state.user);
  const identity = `${user?.entity_id}:${user?.id}:${props.scope.kind}:${props.scope.id || "personal"}`;
  return <ApprovalSettingsBody key={identity} {...props} identity={identity} />;
}
function ApprovalSettingsBody({ scope, presentation = "page", detailKey, showOverrides = false, identity }: {
  scope: ApprovalScope; presentation?: "page" | "drawer"; detailKey?: string; showOverrides?: boolean; identity: string;
}) {
  const user = useAuthStore(state => state.user);
  const descriptionId = useId();
  const queryClient = useQueryClient();
  const query = useApprovalSettings(scope);
  const [baseline, setBaseline] = useState<ApprovalSnapshot | null>(() => drafts.get(identity)?.baseline || null);
  const [draft, setDraft] = useState<ApprovalDraft | null>(() => drafts.get(identity)?.draft || null);
  const [busy, setBusy] = useState<"save" | "preview" | null>(null);
  const [error, setError] = useState("");
  // The data version a save/preview was refused against; a newer read decides again.
  const [forbiddenAt, setForbiddenAt] = useState<number | null>(null);
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState<ApprovalPreview | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [pendingClose, setPendingClose] = useState<(() => void) | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const dirty = !!baseline && !!draft && JSON.stringify(approvalDraft(baseline)) !== JSON.stringify(draft);
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const busyRef = useRef(busy); busyRef.current = busy;
  const current = query.data;
  const conflict = !!current && !!baseline && current.version !== baseline.version && dirty;
  const forbidden = forbiddenAt !== null && query.dataUpdatedAt <= forbiddenAt;
  // Only the server's capabilities are a permission fact. A failed background
  // refresh (backend restarting, connection dropped) keeps the last known
  // snapshot and its capabilities; the save itself is still version-checked.
  const allowedToEdit = !!current && (scope.kind === "account" ? current.capabilities.can_edit_root : current.capabilities.can_edit_local);
  const canEdit = allowedToEdit && !forbidden;
  const refreshFailed = !!current && query.isError && !query.isFetching;

  useEffect(() => {
    if (current && (!baseline || !dirty)) { setBaseline(current); setDraft(approvalDraft(current)); }
  }, [current, baseline, dirty]);
  useEffect(() => {
    if (dirty && baseline && draft) drafts.set(identity, { baseline, draft }); else drafts.delete(identity);
  }, [identity, baseline, draft, dirty]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirtyRef.current || busyRef.current) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, []);
  useEffect(() => {
    if (!detailKey) return;
    const beforeClose = (proceed: () => void) => {
      if (busyRef.current) return;
      if (dirtyRef.current) setPendingClose(() => proceed); else proceed();
    };
    useDetailStore.setState(state => state.payload?.key === detailKey ? { payload: { ...state.payload, beforeClose } } : state);
    return () => { useDetailStore.setState(state => state.payload?.beforeClose === beforeClose ? { payload: { ...state.payload, beforeClose: undefined } } : state); };
  }, [detailKey]);

  const edit = (next: ApprovalDraft) => { setDraft(next); setSaved(false); setPreview(null); setError(""); };
  const handleError = (cause: unknown) => {
    if (cause instanceof ApiError && cause.status === 409) {
      const latest = cause.detail?.current as ApprovalSnapshot | undefined;
      if (latest && user) queryClient.setQueryData(approvalKeys.settings(user.entity_id, user.id, scope), latest);
      else void query.refetch();
      setError(t("approval_settings.conflict"));
    } else if (cause instanceof ApiError && [403, 404].includes(cause.status)) {
      setForbiddenAt(query.dataUpdatedAt); setError(t("approval_settings.unavailable")); void query.refetch();
    } else setError(`${t("approval_settings.save_error")} ${cause instanceof Error ? cause.message : ""}`);
  };
  const previewDraft = async (latest = false) => {
    const source = latest ? current : baseline;
    if (!source || !draft || !canEdit || busy) return;
    setAdvancedOpen(true);
    setBusy("preview"); setError("");
    try {
      const result = await api.approvalSettings.preview(scope, { expected_version: source.version, draft });
      if (!mounted.current) return;
      if (latest) setBaseline(source);
      setPreview(result);
    } catch (cause) { if (mounted.current) handleError(cause); }
    finally { if (mounted.current) setBusy(null); }
  };
  const save = async () => {
    if (!baseline || !draft || !user || !canEdit || !dirty || conflict || busy) return;
    setBusy("save"); setError("");
    try {
      const result = await api.approvalSettings.update(scope, { expected_version: baseline.version, draft });
      queryClient.setQueryData(approvalKeys.settings(user.entity_id, user.id, scope), result);
      void queryClient.invalidateQueries({ queryKey: approvalKeys.entity(result.scope.entity_id || user.entity_id) });
      if (!mounted.current) return;
      setBaseline(result); setDraft(approvalDraft(result)); drafts.delete(identity); setPreview(null); setSaved(true);
      if (presentation === "drawer" && detailKey) {
        useDetailStore.setState(state => state.payload?.key === detailKey ? { payload: null } : state);
      }
    } catch (cause) { if (mounted.current) handleError(cause); }
    finally { if (mounted.current) setBusy(null); }
  };

  if (query.isPending || (current && (!baseline || !draft))) return <PageLoading label={t("approval_settings.loading")} minHeight={96} />;
  if (!current || !baseline || !draft || (query.error instanceof ApiError && [403, 404].includes(query.error.status))) return <div role="alert" className="approval-status"><p>{t(query.error instanceof ApiError && [403, 404].includes(query.error.status) ? "approval_settings.unavailable" : "approval_settings.error")}</p><Button variant="outline" size="sm" onClick={() => void query.refetch()}>{t("approval_settings.retry")}</Button></div>;
  const local = scope.kind !== "account";
  const inheritedMode = current.inherited_mode;
  const ruleCount = draft.user_rules.length + current.policy.rules.filter(rule => rule.source_kind !== "user").length;
  const rulesDisabled = !canEdit || !!busy || !current.capabilities.can_save_rule;
  const catalog = current.action_catalog || [];
  return <div className={`approval-settings approval-settings--${presentation}`} aria-busy={!!busy}>
    {presentation === "page" && <p className="approval-scope">{t("approval_settings.scope_prefix", { scope: approvalScopeLabel(current.scope) })}</p>}
    {!allowedToEdit && <p role="status" className="approval-readonly">{t("approval_settings.readonly")}</p>}
    {refreshFailed && <div role="alert" className="approval-status"><p>{t("approval_settings.refresh_failed")}</p><Button variant="outline" size="sm" onClick={() => void query.refetch()}>{t("approval_settings.retry")}</Button></div>}
    {current.approval_review?.required && <p role="status" className="approval-readonly">{t("approval_settings.review_required")}</p>}
    <div role="radiogroup" aria-label={t(local ? "approval_settings.local_mode" : "approval_settings.default_mode")} className="approval-mode-list" onKeyDown={event => {
      if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const options = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]:not(:disabled)'));
      const index = options.indexOf(event.target as HTMLButtonElement);
      if (index < 0) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (index + (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) + options.length) % options.length;
      options[next].focus(); options[next].click();
    }}>
      {(local ? [null, ...modes] : modes).map(mode => <RadioCard key={mode ?? "follow"} variant="tile" selected={draft.mode === mode} disabled={!canEdit || !!busy} tabIndex={draft.mode === mode ? 0 : -1} ariaLabel={t(`approval_settings.${mode ?? "follow"}`)} ariaDescribedBy={`${descriptionId}-${mode ?? "follow"}`} onSelect={() => edit({ ...draft, mode })}>
        <span className="approval-mode-copy"><strong>{t(`approval_settings.${mode ?? "follow"}`)}</strong><span id={`${descriptionId}-${mode ?? "follow"}`} className="approval-mode-description">{mode ? t(`approval_settings.${mode}_description`) : inheritedMode ? t(`approval_settings.${inheritedMode}`) : t("approval_settings.following")}</span></span>
        <svg className="approval-mode-check" aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 4 4L19 6" /></svg>
      </RadioCard>)}
    </div>
    <details className="approval-disclosure" open={advancedOpen} onToggle={event => setAdvancedOpen(event.currentTarget.open)}><summary>{t("approval_settings.advanced")}{ruleCount > 0 && <> <span>{t("approval_settings.rule_count", { count: ruleCount })}</span></>}</summary>
      <div className="approval-advanced">
        {current.root_scope && <Button variant="ghost" size="sm" onClick={() => openApprovalSettings(current.root_scope!)}>{t("approval_settings.open_default")}</Button>}
        <p className="approval-note">{t(local ? "approval_settings.local_description" : "approval_settings.root_description")} {t("approval_settings.limits")}</p>
        <h3 className="manor-section-title">{t("approval_settings.rules")}</h3>
        <ApprovalRuleEditor draft={draft} catalog={catalog} inherited={current.inherited_rules || []} derived={(current.saved_rules || current.policy.rules).filter(rule => rule.source_kind !== "user")} disabled={rulesDisabled} onChange={edit} />
    <div className="approval-preview"><Button variant="ghost" size="sm" disabled={!canEdit || !!busy || conflict} loading={busy === "preview"} onClick={() => void previewDraft()}>{t("approval_settings.preview")}</Button>
      {preview && <div className="approval-preview-results"><p className="approval-note">{t("approval_settings.representative")}</p><table><thead><tr><th>{t("approval_settings.action")}</th><th>{t("approval_settings.before")}</th><th>{t("approval_settings.after")}</th></tr></thead><tbody>{preview.samples.map(sample => <tr key={sample.action_key}><td>{catalog.find(item => item.action_key === sample.action_key)?.label || sample.action_key}</td><td>{t(`approval_settings.${sample.before.outcome}`)}</td><td>{t(`approval_settings.${sample.after.outcome}`)}</td></tr>)}</tbody></table></div>}
    </div>
        {local && <Button variant="ghost" size="sm" disabled={!canEdit || !!busy || (draft.mode === null && draft.user_rules.length === 0)} onClick={() => setResetOpen(true)}>{t("approval_settings.reset")}</Button>}
      </div>
    </details>
    {(conflict || error) && <div role="alert" className="approval-status"><p>{conflict ? t("approval_settings.conflict") : error}</p>{conflict && <Button variant="outline" size="sm" disabled={!canEdit || !!busy} onClick={() => void previewDraft(true)}>{t("approval_settings.refresh_preview")}</Button>}</div>}
    <div className="approval-save-row">
      <span role="status">{dirty ? t("approval_settings.unsaved") : saved ? t("approval_settings.saved") : ""}</span>
      <div>{dirty && <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => { setBaseline(current); edit(approvalDraft(current)); }}>{t("approval_settings.cancel")}</Button>}<Button size="sm" disabled={!canEdit || !dirty || conflict || !!busy} loading={busy === "save"} onClick={() => void save()}>{t("approval_settings.save")}</Button></div>
    </div>
    {showOverrides && scope.id && <ApprovalExceptions entityId={scope.id} />}
    <ConfirmDialog open={resetOpen} onClose={() => setResetOpen(false)} onConfirm={() => edit({ mode: null, user_rules: [] })} title={t("approval_settings.reset")} message={t("approval_settings.reset_description")} confirmLabel={t("approval_settings.reset")} cancelLabel={t("approval_settings.cancel")} />
    <ConfirmDialog open={!!pendingClose} onClose={() => setPendingClose(null)} onConfirm={() => { drafts.delete(identity); dirtyRef.current = false; pendingClose?.(); }} title={t("approval_settings.discard_title")} message={t("approval_settings.discard_description")} confirmLabel={t("approval_settings.discard")} cancelLabel={t("approval_settings.keep_editing")} />
  </div>;
}
function ApprovalExceptions({ entityId }: { entityId: string }) {
  const user = useAuthStore(state => state.user);
  const [offset, setOffset] = useState(0);
  const query = useQuery({ queryKey: approvalKeys.overrides(entityId, user?.id || "", offset), queryFn: () => api.approvalSettings.overrides(entityId, offset), enabled: !!user, refetchOnWindowFocus: "always" });
  return <section className="approval-exceptions"><h3 className="manor-section-title">{t("approval_settings.exceptions")}</h3><p className="manor-section-subtitle">{t("approval_settings.exceptions_description")}</p>
    {query.data && <p className="approval-note">{t("approval_settings.scope_counts", { following: query.data.following_count, custom: query.data.custom_count })}</p>}
    {query.isPending ? <PageLoading label={t("approval_settings.loading")} minHeight={96} /> : query.isError ? <div role="alert"><p className="approval-note">{t("approval_settings.error")}</p><Button size="sm" variant="outline" onClick={() => void query.refetch()}>{t("approval_settings.retry")}</Button></div> : <>
      {query.data.items.length === 0 && <p className="approval-note">{t("approval_settings.no_exceptions")}</p>}
      {query.data.items.map(item => <div className="approval-exception" key={`${item.scope.kind}:${item.scope.id}`}><div><strong>{item.label}</strong><span>{approvalInheritanceLabel(item.snapshot)} · {t(`approval_settings.${item.snapshot.effective_policy.mode}`)}</span></div><Button size="sm" variant="ghost" onClick={() => openApprovalSettings(item.scope, item.label)}>{t("approval_settings.edit")}</Button></div>)}
      {query.data.total > query.data.limit && <div className="approval-pagination"><Button variant="ghost" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - query.data.limit))}>{t("approval_settings.previous")}</Button><Button variant="ghost" size="sm" disabled={offset + query.data.items.length >= query.data.total} onClick={() => setOffset(offset + query.data.limit)}>{t("approval_settings.next")}</Button></div>}
    </>}
  </section>;
}
