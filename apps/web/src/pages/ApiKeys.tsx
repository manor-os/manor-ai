import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { ApiKey } from "../lib/types";
import { useToastStore } from "../stores/toast";
import { relativeTime, statusBadgeType } from "../lib/format";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import SmartToolbar from "../components/ui/SmartToolbar";
import StatusBadge from "../components/ui/StatusBadge";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import { IconKey, IconEdit, IconRefresh, IconPlay, IconTrash } from "../components/icons";
import { t } from "../lib/i18n";

/* -- constants ------------------------------------------------ */

const PROVIDERS = ["openrouter", "openai", "anthropic", "custom"];

/* -- component ------------------------------------------------ */

export default function ApiKeys({ embedded = false }: { embedded?: boolean } = {}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const [search, setSearch] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editKey, setEditKey] = useState<ApiKey | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);

  // Form state
  const [formName, setFormName] = useState("");
  const [formProvider, setFormProvider] = useState("openrouter");
  const [formApiKey, setFormApiKey] = useState("");
  const [formBaseUrl, setFormBaseUrl] = useState("");
  const [formDefaultModel, setFormDefaultModel] = useState("");

  const { data: keys = [], isLoading } = useQuery({
    queryKey: ["api-keys"],
    queryFn: () => api.apiKeys.list(),
  });

  const createMutation = useMutation({
    mutationFn: (data: { name: string; provider: string; key: string; base_url?: string; default_model?: string }) => api.apiKeys.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
      closeModal();
      toast.success(t("page.api_keys.key_created"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, any> }) => api.apiKeys.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
      closeModal();
      toast.success(t("page.api_keys.key_updated"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.apiKeys.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
      toast.success(t("page.api_keys.key_deleted"));
    },
  });

  const rotateMutation = useMutation({
    mutationFn: ({ id, newKey }: { id: string; newKey: string }) => api.apiKeys.rotate(id, newKey),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
      toast.success(t("page.api_keys.key_rotated"));
    },
  });

  const testMutation = useMutation({
    mutationFn: (id: string) => api.apiKeys.test(id),
    onSuccess: () => {
      toast.success(t("page.api_keys.test_passed"));
      setTestingId(null);
    },
    onError: () => {
      toast.error(t("page.api_keys.test_failed"), t("page.api_keys.test_failed_detail"));
      setTestingId(null);
    },
  });

  const setDefaultMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, any> }) => api.apiKeys.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
    },
  });

  const filtered = useMemo(() => {
    const list = keys as ApiKey[];
    if (!search.trim()) return list;
    const q = search.toLowerCase();
    return list.filter(
      (k) => k.name.toLowerCase().includes(q) || k.provider.toLowerCase().includes(q),
    );
  }, [keys, search]);

  function openCreate() {
    setEditKey(null);
    setFormName("");
    setFormProvider("openrouter");
    setFormApiKey("");
    setFormBaseUrl("");
    setFormDefaultModel("");
    setModalOpen(true);
  }

  function openEdit(key: ApiKey) {
    setEditKey(key);
    setFormName(key.name);
    setFormProvider(key.provider);
    setFormApiKey("");
    setFormBaseUrl(key.base_url || "");
    setFormDefaultModel(key.default_model || "");
    setModalOpen(true);
  }

  function closeModal() {
    setModalOpen(false);
    setEditKey(null);
  }

  function handleSubmit() {
    const data: Record<string, any> = {
      name: formName,
      provider: formProvider,
      base_url: formBaseUrl || undefined,
      default_model: formDefaultModel || undefined,
    };
    if (formApiKey) data.api_key = formApiKey;

    if (editKey) {
      updateMutation.mutate({ id: editKey.id, data });
    } else {
      data.api_key = formApiKey;
      createMutation.mutate({ name: formName, provider: formProvider, key: formApiKey, base_url: formBaseUrl || undefined });
    }
  }

  return (
    <div style={embedded ? undefined : { maxWidth: 1060, margin: "0 auto" }}>
      {!embedded && (
      <PageHeader
        title={t("page.api_keys.title")}
        subtitle={t("page.api_keys.subtitle")}
        toolbar={(
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.api_keys.search_placeholder")}
            className="w-full sm:w-64"
          />
        )}
        actions={
          <PageHeaderAddButton label={t("page.api_keys.add_key")} onClick={openCreate} />
        }
      />
      )}
      {embedded && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, gap: 12 }}>
          <div>
            <h3 className="manor-section-title" style={{ margin: "0 0 4px" }}>{t("page.api_keys.title")}</h3>
            <p style={{ margin: 0, fontSize: 12, color: "var(--ink-3, #78716c)" }}>{t("page.api_keys.subtitle")}</p>
          </div>
          <PageHeaderAddButton label={t("page.api_keys.add_key")} onClick={openCreate} />
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
          icon={<IconKey size={32} className="text-stone-300" />}
          title={t("page.api_keys.no_keys")}
          description={t("page.api_keys.no_keys_desc")}
        />
      )}

      {/* Table */}
      {!isLoading && filtered.length > 0 && (
        <div className="glass-card" style={{ overflow: "hidden", padding: 0 }}>
          <div style={{ overflowX: "auto" }}>
            <table className="glass-table" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  <th>{t("page.api_keys.name")}</th>
                  <th>{t("page.api_keys.provider")}</th>
                  <th>{t("page.api_keys.key")}</th>
                  <th>{t("page.api_keys.model")}</th>
                  <th>{t("page.api_keys.default")}</th>
                  <th>{t("page.api_keys.status")}</th>
                  <th>{t("page.api_keys.usage")}</th>
                  <th>{t("page.api_keys.last_used")}</th>
                  <th style={{ textAlign: "right" }}>{t("page.api_keys.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((key) => (
                  <tr key={key.id}>
                    <td style={{ padding: "12px 16px", fontWeight: 600, color: "#292524" }}>{key.name}</td>
                    <td style={{ padding: "12px 16px" }}>
                      <span style={{
                        padding: "2px 8px",
                        borderRadius: 6,
                        fontSize: 11,
                        fontWeight: 600,
                        background: "#f5f5f4",
                        color: "#57534e",
                        textTransform: "capitalize",
                      }}>
                        {key.provider}
                      </span>
                    </td>
                    <td style={{ padding: "12px 16px", fontFamily: "monospace", fontSize: 12, color: "#78716c" }}>{key.key_prefix}...</td>
                    <td style={{ padding: "12px 16px", color: "#78716c", fontSize: 12 }}>{key.default_model || "--"}</td>
                    <td style={{ padding: "12px 16px" }}>
                      {key.is_default ? (
                        <StatusBadge type="teal" dot>{t("page.api_keys.default")}</StatusBadge>
                      ) : (
                        <button
                          onClick={() => setDefaultMutation.mutate({ id: key.id, data: { is_default: true } })}
                          style={{
                            padding: "4px 10px",
                            borderRadius: 8,
                            fontSize: 11,
                            fontWeight: 600,
                            background: "#fafaf9",
                            border: "1px solid rgba(28,25,23,0.06)",
                            color: "#a8a29e",
                            cursor: "pointer",
                            transition: "all 0.2s",
                          }}
                        >
                          {t("page.api_keys.set_default")}
                        </button>
                      )}
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      <StatusBadge type={statusBadgeType(key.status)} dot>{key.status}</StatusBadge>
                    </td>
                    <td style={{ padding: "12px 16px", color: "#78716c", fontSize: 12 }}>{key.usage_count}</td>
                    <td style={{ padding: "12px 16px", color: "#78716c", fontSize: 12 }}>{relativeTime(key.last_used_at)}</td>
                    <td style={{ padding: "12px 16px", textAlign: "right" }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 4 }}>
                        <button
                          onClick={() => openEdit(key)}
                          style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e", transition: "all 0.2s" }}
                          title={t("action.edit")}
                        >
                          <IconEdit size={16} />
                        </button>
                        <button
                          onClick={() => { const newKey = prompt(t("page.api_keys.enter_new_api_key")); if (newKey) rotateMutation.mutate({ id: key.id, newKey }); }}
                          style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e", transition: "all 0.2s" }}
                          title={t("page.api_keys.rotate_key")}
                        >
                          <IconRefresh size={16} />
                        </button>
                        <button
                          onClick={() => { setTestingId(key.id); testMutation.mutate(key.id); }}
                          disabled={testingId === key.id}
                          style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: testingId === key.id ? "#4f7d75" : "#a8a29e", transition: "all 0.2s" }}
                          title={t("page.api_keys.test_key")}
                        >
                          <IconPlay size={16} />
                        </button>
                        <button
                          onClick={() => setDeleteTarget(key.id)}
                          style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e", transition: "all 0.2s" }}
                          title={t("action.delete")}
                        >
                          <IconTrash size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
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
        title={t("page.api_keys.delete_api_key")}
        message={t("page.api_keys.delete_api_key_confirm")}
        confirmLabel={t("action.delete")}
        danger
      />

      {/* Create / Edit Modal */}
      <Modal
        open={modalOpen}
        onClose={closeModal}
        title={editKey ? t("page.api_keys.edit_api_key") : t("page.api_keys.add_api_key")}
        footer={
          <>
            <Button variant="outline" onClick={closeModal}>{t("action.cancel")}</Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              disabled={!formName.trim() || (!editKey && !formApiKey.trim()) || createMutation.isPending || updateMutation.isPending}
            >
              {createMutation.isPending || updateMutation.isPending ? t("page.api_keys.saving") : editKey ? t("action.update") : t("page.api_keys.add_key")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Input label={t("page.api_keys.name")} value={formName} onChange={(e) => setFormName(e.target.value)} placeholder={t("page.api_keys.name_placeholder")} />
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 4 }}>{t("page.api_keys.provider")}</label>
            <select value={formProvider} onChange={(e) => setFormProvider(e.target.value)} className="manor-input">
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>{p.charAt(0).toUpperCase() + p.slice(1)}</option>
              ))}
            </select>
          </div>
          <Input
            label={editKey ? t("page.api_keys.api_key_leave_blank") : t("page.api_keys.api_key")}
            type="password"
            value={formApiKey}
            onChange={(e) => setFormApiKey(e.target.value)}
            placeholder={editKey ? t("page.api_keys.api_key_replace_placeholder") : "sk-..."}
          />
          <Input label={t("page.api_keys.base_url_optional")} value={formBaseUrl} onChange={(e) => setFormBaseUrl(e.target.value)} placeholder={t("page.api_keys.https_api_openai_com_v1")} />
          <Input label={t("page.api_keys.default_model_optional")} value={formDefaultModel} onChange={(e) => setFormDefaultModel(e.target.value)} placeholder={t("page.api_keys.gpt_4o")} />
        </div>
      </Modal>
    </div>
  );
}
