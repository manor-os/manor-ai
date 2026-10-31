import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ClientRecord, type ClientDeliveryReceipt } from "../../lib/api";
import { t } from "../../lib/i18n";
import { crmQueryOptions } from "../../lib/crmQueries";
import { useAuthStore } from "../../stores/auth";
import Button from "../../components/ui/Button";
import Input from "../../components/ui/Input";
import Checkbox from "../../components/ui/Checkbox";
import Select from "../../components/ui/Select";
import Modal from "../../components/ui/Modal";
import EmptyState from "../../components/ui/EmptyState";
import LoadingSpinner from "../../components/ui/LoadingSpinner";

type Props = {
  open: boolean;
  onClose: () => void;
  client?: ClientRecord;
  workspace?: { id: string; name: string };
  initialWorkspace?: string;
};
const c = (key: string) => t(`page.clients.${key}`);
const KNOWLEDGE_PAGE_SIZE = 50;
const KNOWLEDGE_SELECTION_LIMIT = 64;

// Mount each opening separately so closing or changing company cannot retain an invitation.
export default function ClientDeliveryDialog(props: Props) {
  const identity = useAuthStore(s => `${s.user?.id}:${s.user?.entity_id}`);
  return props.open ? <DeliveryForm key={`${identity}:${props.workspace?.id}:${props.client?.id}`} {...props} identity={identity} /> : null;
}

function DeliveryForm({ onClose, client, workspace, initialWorkspace, identity }: Props & { identity: string }) {
  const cache = useQueryClient();
  const navigate = useNavigate();
  const [selectedClient, setSelectedClient] = useState(client);
  const [workspaceId, setWorkspaceId] = useState(workspace?.id || initialWorkspace || "");
  const [contactId, setContactId] = useState("");
  const [knowledgeDocumentIds, setKnowledgeDocumentIds] = useState<string[]>([]);
  const [knowledgeOffset, setKnowledgeOffset] = useState(0);
  const [phase, setPhase] = useState<"trial" | "delivered">("trial");
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<ClientDeliveryReceipt | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setAppliedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const clients = useInfiniteQuery({
    queryKey: ["client-delivery-clients", identity, appliedSearch],
    queryFn: ({ pageParam, signal }) => api.crm.clients(new URLSearchParams({ status: "active", search: appliedSearch, limit: "50", offset: String(pageParam) }).toString(), signal),
    initialPageParam: 0,
    getNextPageParam: (page, _pages, offset) => page.items.length > 0 && offset + page.items.length < page.total ? offset + page.items.length : undefined,
    staleTime: 60_000,
    enabled: !client && !receipt,
  });
  const rows = clients.data?.pages.flatMap(page => page.items) || [];
  const clientOptions = selectedClient && !rows.some(row => row.id === selectedClient.id) ? [selectedClient, ...rows] : rows;
  const contacts = useQuery({
    ...crmQueryOptions(["client-contacts", identity, selectedClient?.id], signal => api.clients.contacts(selectedClient!.id, signal)),
    enabled: !!selectedClient && !receipt,
  });
  const workspaces = useQuery({
    queryKey: ["client-delivery-workspaces", identity],
    queryFn: () => api.workspaces.list(),
    staleTime: 60_000,
    enabled: !workspace && !receipt,
  });
  const knowledge = useQuery({
    queryKey: ["client-delivery-knowledge", identity, workspaceId, knowledgeOffset],
    queryFn: () => api.workspaces.blueprintExportKnowledgeDocuments(workspaceId, KNOWLEDGE_PAGE_SIZE, knowledgeOffset),
    enabled: !!workspaceId && !receipt,
    staleTime: 30_000,
  });
  const knowledgeDocuments = knowledge.data || [];
  const recipient = contacts.data?.find(row => row.id === contactId) || (!contactId ? contacts.data?.[0] : undefined);
  const queryError = contacts.error || (!client && clients.error) || (!workspace && workspaces.error);
  const validWorkspace = workspace || workspaces.data?.find(row => row.id === workspaceId && row.can_manage);
  const ready = !!selectedClient && selectedClient.status === "active" && !!recipient && !!validWorkspace && !queryError && !contacts.isPending && (!!workspace || !workspaces.isPending);
  const inviteLink = receipt?.invite_path ? new URL(receipt.invite_path, window.location.origin).href : "";

  async function submit() {
    if (!ready || busy || receipt) return;
    setBusy(true);
    setError("");
    try {
      const created = await api.clients.prepareDelivery(selectedClient!.id, { workspace_id: workspaceId, contact_id: recipient!.id, phase, knowledge_document_ids: knowledgeDocumentIds });
      // Persist success in the dialog before refreshing the list; refresh failure must not allow a second creation.
      setReceipt(created);
      await cache.invalidateQueries({ queryKey: ["client-deliveries", identity, selectedClient!.id] });
    } catch (e) {
      setError(e instanceof Error ? e.message : t("status.error"));
    } finally {
      setBusy(false);
    }
  }
  async function resend() {
    if (!receipt || busy) return;
    setBusy(true); setError(""); setCopied(false);
    try {
      const updated = await api.clients.changeInvite(receipt.client_id, receipt.id, "rotate");
      setReceipt(updated);
      await cache.invalidateQueries({ queryKey: ["client-deliveries", identity, receipt.client_id] });
    } catch (e) {
      setError(e instanceof Error ? e.message : t("status.error"));
    } finally { setBusy(false); }
  }
  function manageContacts() {
    onClose();
    navigate(selectedClient ? `/team/clients?client=${encodeURIComponent(selectedClient.id)}&section=contacts` : "/team/clients");
  }
  return <Modal open onClose={() => { if (!busy) onClose(); }} title={c(receipt ? (receipt.email_delivery?.status === "sent" ? "email_sent_title" : "invite_ready") : "deliver")}>
    {receipt ? <div className="space-y-4">
      <p role="status" className="text-sm">{t("page.clients.delivery_ready", { name: receipt.title })}</p>
      <p className="text-sm text-stone-500">{receipt.recipient_email}</p>
      <p role={receipt.email_delivery?.status === "sent" ? "status" : "alert"} className="text-sm">{t(`page.clients.email_${receipt.email_delivery?.status || "not_sent"}`, { email: receipt.recipient_email })}</p>
      {receipt.email_delivery?.status !== "sent" && <Button loading={busy} onClick={() => void resend()}>{c("resend_email")}</Button>}
      <p className="text-sm text-stone-500">{c("link_policy")}</p>
      <Input label={c("invite_link")} value={inviteLink} onChange={() => {}} readOnly />
      <p className="text-sm text-stone-500">{c("delivery_next_step")}</p>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" disabled={busy || !inviteLink} onClick={() => {
          setError("");
          void navigator.clipboard.writeText(inviteLink).then(() => setCopied(true)).catch(e => setError(e instanceof Error ? e.message : t("status.error")));
        }}>{c(copied ? "copied" : "copy")}</Button>
        <Button variant="outline" disabled={busy} onClick={() => { onClose(); navigate(`/team/clients?client=${encodeURIComponent(receipt.client_id)}&section=delivery`); }}>{c("view_delivery")}</Button>
      </div>
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
    </div> : <form className="space-y-4" onSubmit={event => { event.preventDefault(); void submit(); }}>
      {workspace ? <Input label={c("source_workspace")} value={workspace.name} onChange={() => {}} readOnly /> : <div className="space-y-1.5">
        <p className="text-sm font-medium">{c("workspace")}</p>
        <Select ariaLabel={c("workspace")} value={workspaceId} onChange={id => { setWorkspaceId(id); setKnowledgeDocumentIds([]); setKnowledgeOffset(0); setError(""); }} disabled={busy || workspaces.isPending} placeholder={c("workspace")} filterable options={(workspaces.data || []).filter(row => row.can_manage).map(row => ({ value: row.id, label: row.name }))} />
      </div>}
      {client ? <Input label={c("client_company")} value={client.name} onChange={() => {}} readOnly /> : <div className="space-y-2">
        <Input label={c("search_clients")} value={search} onChange={event => setSearch(event.target.value)} disabled={busy} />
        <Select ariaLabel={c("client_company")} value={selectedClient?.id || ""} onChange={id => { setSelectedClient(clientOptions.find(row => row.id === id)); setContactId(""); setError(""); }} disabled={busy || clients.isPending} placeholder={c("choose_client")} options={clientOptions.map(row => ({ value: row.id, label: row.name }))} />
        {clients.isPending && <LoadingSpinner size={16} ariaLabel={t("status.loading")} />}
        {clients.hasNextPage && <Button variant="ghost" size="sm" loading={clients.isFetchingNextPage} disabled={busy} onClick={() => void clients.fetchNextPage()}>{c("load_more_clients")}</Button>}
        {!clients.isPending && !clients.isError && !rows.length && <EmptyState compact title={c("no_matching_clients")} action={<Button variant="outline" size="sm" onClick={manageContacts}>{c("manage_clients")}</Button>} />}
      </div>}
      <div className="space-y-1.5">
        <p className="text-sm font-medium">{c("recipient")}</p>
        <Select ariaLabel={c("recipient")} value={recipient?.id || ""} onChange={setContactId} disabled={busy || !selectedClient || contacts.isPending} placeholder={c("recipient")} options={(contacts.data || []).map(row => ({ value: row.id, label: `${row.name} · ${row.email}` }))} />
        {selectedClient && contacts.isPending && <LoadingSpinner size={16} ariaLabel={t("status.loading")} />}
        {selectedClient && !contacts.isPending && !contacts.isError && !contacts.data?.length && <p className="text-sm text-stone-500">{c("delivery_contact_required")} <Button variant="ghost" size="sm" disabled={busy} onClick={manageContacts}>{c("manage_contacts")}</Button></p>}
      </div>
      {workspaceId && <fieldset className="space-y-2" disabled={busy}>
        <legend className="text-sm font-medium">{c("starter_files")}</legend>
        <p className="text-sm text-stone-500">{c("starter_files_hint")}</p>
        <p className="text-xs text-stone-500" aria-live="polite">{t("page.clients.starter_files_selected", { selected: knowledgeDocumentIds.length, limit: KNOWLEDGE_SELECTION_LIMIT })}</p>
        {knowledge.isPending && <LoadingSpinner size={16} ariaLabel={t("status.loading")} />}
        {knowledge.isError && <p role="alert" className="text-sm text-red-700">{c("starter_files_error")} <Button variant="ghost" size="sm" disabled={busy} onClick={() => void knowledge.refetch()}>{c("retry")}</Button></p>}
        {knowledge.isSuccess && !knowledgeDocuments.length && <p className="text-sm text-stone-500">{c("starter_files_empty")}</p>}
        {!!knowledgeDocuments.length && <div className="max-h-48 overflow-y-auto space-y-2 rounded-[var(--radius-control)] bg-[var(--surface-muted)] p-3">
          {knowledgeDocuments.map(document => <Checkbox
            key={document.id}
            aria-label={document.path}
            checked={knowledgeDocumentIds.includes(document.id)}
            disabled={busy || (!knowledgeDocumentIds.includes(document.id) && knowledgeDocumentIds.length >= KNOWLEDGE_SELECTION_LIMIT)}
            onChange={checked => {
              setKnowledgeDocumentIds(current => checked
                ? [...new Set([...current, document.id])].slice(0, KNOWLEDGE_SELECTION_LIMIT)
                : current.filter(id => id !== document.id));
              setError("");
            }}
            style={{ width: "100%", alignItems: "flex-start" }}
            label={<span className="break-all text-sm">
              {document.path}
              {document.content_source === "initial_template" && <span className="block text-xs text-stone-500">{c("starter_initial_template")}</span>}
              {!!document.required_by_workflows?.length && <span className="block text-xs text-stone-500">{t("page.clients.starter_required_by", { workflows: document.required_by_workflows.join(", ") })}</span>}
            </span>}
          />)}
        </div>}
        {(knowledgeOffset > 0 || knowledgeDocuments.length === KNOWLEDGE_PAGE_SIZE) && <div className="flex flex-wrap gap-2">
          <Button variant="ghost" size="sm" disabled={busy || knowledgeOffset === 0 || knowledge.isFetching} onClick={() => setKnowledgeOffset(offset => Math.max(0, offset - KNOWLEDGE_PAGE_SIZE))}>{c("starter_files_previous")}</Button>
          <Button variant="ghost" size="sm" disabled={busy || knowledgeDocuments.length < KNOWLEDGE_PAGE_SIZE || knowledge.isFetching} onClick={() => setKnowledgeOffset(offset => offset + KNOWLEDGE_PAGE_SIZE)}>{c("starter_files_next")}</Button>
        </div>}
      </fieldset>}
      <p className="text-sm text-stone-500">{c("materials_policy")}</p>
      <div className="space-y-1.5">
        <p className="text-sm font-medium">{t("page.collaboration.delivery_mode")}</p>
        <Select ariaLabel={t("page.collaboration.delivery_mode")} value={phase} onChange={value => setPhase(value as "trial" | "delivered")} disabled={busy} options={[{ value: "trial", label: t("page.collaboration.phase_trial") }, { value: "delivered", label: t("page.collaboration.direct_delivery") }]} />
      </div>
      {queryError && <p role="alert" className="text-sm text-red-700">{queryError.message} <Button variant="ghost" size="sm" onClick={() => { if (contacts.isError) void contacts.refetch(); if (clients.isError) void clients.refetch(); if (workspaces.isError) void workspaces.refetch(); }}>{t("page.collaboration.retry")}</Button></p>}
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
      <Button type="submit" loading={busy} disabled={!ready}>{c("send_delivery")}</Button>
    </form>}
  </Modal>;
}
