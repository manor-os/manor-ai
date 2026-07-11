import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { WebhookEndpoint, WebhookDelivery } from "../lib/types";
import { useToastStore } from "../stores/toast";
import { formatDateLong as formatDate, statusBadgeType } from "../lib/format";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import SmartToolbar from "../components/ui/SmartToolbar";
import StatusBadge from "../components/ui/StatusBadge";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import { IconChevronRight, IconEdit, IconPlay, IconTrash, IconFlow } from "../components/icons";
import { t } from "../lib/i18n";

/* -- constants ------------------------------------------------ */

const EVENTS = [
  "task.created",
  "task.updated",
  "task.completed",
  "task.deleted",
  "document.uploaded",
  "document.updated",
  "agent.executed",
  "conversation.created",
  "user.invited",
];

/* -- component ------------------------------------------------ */

export default function WebhookManager({ embedded = false }: { embedded?: boolean } = {}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const [search, setSearch] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editHook, setEditHook] = useState<WebhookEndpoint | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Form state
  const [formUrl, setFormUrl] = useState("");
  const [formEvents, setFormEvents] = useState<string[]>([]);
  const [formSecret, setFormSecret] = useState("");

  const { data: webhooks = [], isLoading } = useQuery({
    queryKey: ["webhooks"],
    queryFn: () => api.webhooks.list(),
  });

  const { data: deliveries = [] } = useQuery({
    queryKey: ["webhook-deliveries", expandedId],
    queryFn: () => (expandedId ? api.webhooks.deliveries(expandedId) : Promise.resolve([])),
    enabled: !!expandedId,
  });

  const createMutation = useMutation({
    mutationFn: (data: Record<string, any>) => api.webhooks.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["webhooks"] });
      closeModal();
      toast.success(t("page.webhooks.webhook_created"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, any> }) => api.webhooks.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["webhooks"] });
      closeModal();
      toast.success(t("page.webhooks.webhook_updated"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.webhooks.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["webhooks"] });
      toast.success(t("page.webhooks.webhook_deleted"));
    },
  });

  const testMutation = useMutation({
    mutationFn: (id: string) => api.webhooks.test(id),
    onSuccess: () => toast.success(t("page.webhooks.test_event_sent")),
    onError: () => toast.error(t("page.webhooks.test_failed"), t("page.webhooks.could_not_deliver_test_event")),
  });

  const filtered = useMemo(() => {
    const list = webhooks as WebhookEndpoint[];
    if (!search.trim()) return list;
    const q = search.toLowerCase();
    return list.filter(
      (w) => w.url.toLowerCase().includes(q) || w.events.some((e) => e.toLowerCase().includes(q)),
    );
  }, [webhooks, search]);

  function openCreate() {
    setEditHook(null);
    setFormUrl("");
    setFormEvents([]);
    setFormSecret("");
    setModalOpen(true);
  }

  function openEdit(hook: WebhookEndpoint) {
    setEditHook(hook);
    setFormUrl(hook.url);
    setFormEvents([...hook.events]);
    setFormSecret(hook.secret || "");
    setModalOpen(true);
  }

  function closeModal() {
    setModalOpen(false);
    setEditHook(null);
  }

  function toggleEvent(event: string) {
    setFormEvents((prev) =>
      prev.includes(event) ? prev.filter((e) => e !== event) : [...prev, event],
    );
  }

  function handleSubmit() {
    const data: Record<string, any> = {
      url: formUrl,
      events: formEvents,
      secret: formSecret || undefined,
    };

    if (editHook) {
      updateMutation.mutate({ id: editHook.id, data });
    } else {
      createMutation.mutate(data);
    }
  }

  const deliveryStatusBadge = (code?: number) => {
    if (!code) return "inactive";
    if (code >= 200 && code < 300) return "active";
    if (code >= 400) return "danger";
    return "warning";
  };

  return (
    <div style={embedded ? undefined : { maxWidth: 1060, margin: "0 auto" }}>
      {!embedded && (
      <PageHeader
        title={t("page.webhooks.title")}
        subtitle={t("page.webhooks.subtitle")}
        toolbar={(
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.webhooks.search_placeholder")}
            className="w-full sm:w-64"
          />
        )}
        actions={
          <PageHeaderAddButton label={t("page.webhooks.add_webhook")} onClick={openCreate} />
        }
      />
      )}
      {embedded && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, gap: 12 }}>
          <div>
            <h3 className="manor-section-title" style={{ margin: "0 0 4px" }}>{t("page.webhooks.title")}</h3>
            <p style={{ margin: 0, fontSize: 12, color: "var(--ink-3, #78716c)" }}>{t("page.webhooks.subtitle")}</p>
          </div>
          <PageHeaderAddButton label={t("page.webhooks.add_webhook")} onClick={openCreate} />
        </div>
      )}

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}

      {/* Empty */}
      {!isLoading && filtered.length === 0 && (
        <EmptyState
          icon={<IconFlow size={32} className="text-stone-300" />}
          title={t("page.webhooks.no_webhooks")}
          description={t("page.webhooks.no_webhooks_desc")}
        />
      )}

      {/* Table */}
      {!isLoading && filtered.length > 0 && (
        <div className="glass-card" style={{ overflow: "hidden", padding: 0 }}>
          <div style={{ overflowX: "auto" }}>
            <table className="glass-table" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  <th>{t("page.webhooks.url")}</th>
                  <th>{t("page.webhooks.events")}</th>
                  <th>{t("page.webhooks.status")}</th>
                  <th>{t("page.webhooks.created")}</th>
                  <th style={{ textAlign: "right" }}>{t("page.webhooks.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((hook) => (
                  <>
                    <tr key={hook.id} style={{ cursor: "pointer" }} onClick={() => setExpandedId(expandedId === hook.id ? null : hook.id)}>
                      <td style={{ padding: "12px 16px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span style={{ transition: "transform 0.2s", transform: expandedId === hook.id ? "rotate(90deg)" : "none", flexShrink: 0, display: "inline-flex", color: "#a8a29e" }}>
                            <IconChevronRight size={14} />
                          </span>
                          <span style={{ fontWeight: 600, color: "#292524", fontFamily: "monospace", fontSize: 12 }}>{hook.url}</span>
                        </div>
                      </td>
                      <td style={{ padding: "12px 16px" }}>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                          {hook.events.map((ev) => (
                            <span key={ev} style={{
                              padding: "2px 6px",
                              borderRadius: 4,
                              fontSize: 10,
                              fontWeight: 600,
                              background: "#f5f5f4",
                              color: "#57534e",
                            }}>
                              {ev}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td style={{ padding: "12px 16px" }}>
                        <StatusBadge type={statusBadgeType(hook.status)} dot>{hook.status}</StatusBadge>
                      </td>
                      <td style={{ padding: "12px 16px", color: "#78716c", fontSize: 12 }}>{formatDate(hook.created_at)}</td>
                      <td style={{ padding: "12px 16px", textAlign: "right" }}>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 4 }} onClick={(e) => e.stopPropagation()}>
                          <button
                            onClick={() => openEdit(hook)}
                            style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                            title={t("action.edit")}
                          >
                            <IconEdit size={16} />
                          </button>
                          <button
                            onClick={() => testMutation.mutate(hook.id)}
                            style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                            title={t("page.webhooks.test")}
                          >
                            <IconPlay size={16} />
                          </button>
                          <button
                            onClick={() => setDeleteTarget(hook.id)}
                            style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                            title={t("action.delete")}
                          >
                            <IconTrash size={16} />
                          </button>
                        </div>
                      </td>
                    </tr>
                    {/* Expanded deliveries */}
                    {expandedId === hook.id && (
                      <tr key={`${hook.id}-deliveries`}>
                        <td colSpan={5} style={{ padding: "12px 16px", background: "rgba(250,250,249,0.5)" }}>
                          <div style={{ paddingLeft: 24 }}>
                            <h4 style={{ fontSize: 11, fontWeight: 700, color: "#78716c", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>{t("page.webhooks.recent_deliveries")}</h4>
                            {(deliveries as WebhookDelivery[]).length === 0 ? (
                              <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>{t("page.webhooks.no_deliveries_yet")}</p>
                            ) : (
                              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                                {(deliveries as WebhookDelivery[]).slice(0, 10).map((d) => (
                                  <div key={d.id} style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 12, padding: "6px 0" }}>
                                    <StatusBadge type={deliveryStatusBadge(d.response_code)}>{d.response_code || t("page.webhooks.na")}</StatusBadge>
                                    <span style={{ color: "#57534e", fontWeight: 500 }}>{d.event}</span>
                                    <span style={{ color: "#a8a29e" }}>{d.status}</span>
                                    <span style={{ color: "#a8a29e", marginLeft: "auto" }}>{formatDate(d.created_at)}</span>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Delete confirm */}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}
        title={t("page.webhooks.delete_webhook")}
        message={t("page.webhooks.delete_webhook_confirm")}
        confirmLabel={t("action.delete")}
        danger
      />

      {/* Create / Edit Modal */}
      <Modal
        open={modalOpen}
        onClose={closeModal}
        title={editHook ? t("page.webhooks.edit_webhook") : t("page.webhooks.add_webhook")}
        footer={
          <>
            <Button variant="outline" onClick={closeModal}>{t("action.cancel")}</Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              disabled={!formUrl.trim() || formEvents.length === 0 || createMutation.isPending || updateMutation.isPending}
            >
              {createMutation.isPending || updateMutation.isPending ? t("page.webhooks.saving") : editHook ? t("action.update") : t("page.webhooks.add_webhook")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Input label={t("page.webhooks.url")} value={formUrl} onChange={(e) => setFormUrl(e.target.value)} placeholder={t("page.webhooks.https_example_com_webhook")} />
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 8 }}>{t("page.webhooks.events")}</label>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {EVENTS.map((ev) => {
                const selected = formEvents.includes(ev);
                return (
                  <button
                    key={ev}
                    type="button"
                    onClick={() => toggleEvent(ev)}
                    style={{
                      padding: "4px 10px",
                      borderRadius: 8,
                      fontSize: 11,
                      fontWeight: 600,
                      border: "1px solid",
                      borderColor: selected ? "#4f7d75" : "#e7e5e4",
                      background: selected ? "#f2f6f5" : "#fafaf9",
                      color: selected ? "#4f7d75" : "#78716c",
                      cursor: "pointer",
                      transition: "all 0.2s",
                    }}
                  >
                    {ev}
                  </button>
                );
              })}
            </div>
          </div>
          <Input
            label={t("page.webhooks.secret_optional")}
            type="password"
            value={formSecret}
            onChange={(e) => setFormSecret(e.target.value)}
            placeholder={t("page.webhooks.secret_placeholder")}
          />
        </div>
      </Modal>
    </div>
  );
}
