import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Memory, Agent } from "../lib/types";
import { useToastStore } from "../stores/toast";
import { formatDate, statusBadgeType } from "../lib/format";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import SmartToolbar from "../components/ui/SmartToolbar";
import StatusBadge from "../components/ui/StatusBadge";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import { IconEdit, IconTrash } from "../components/icons";
import { t } from "../lib/i18n";

/* -- component ------------------------------------------------ */

export default function Memories() {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const [search, setSearch] = useState("");
  const [agentFilter, setAgentFilter] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [extractModalOpen, setExtractModalOpen] = useState(false);
  const [editMemory, setEditMemory] = useState<Memory | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  // Form state
  const [formContent, setFormContent] = useState("");
  const [formAgentId, setFormAgentId] = useState("");
  const [formContext, setFormContext] = useState("");

  // Extract form
  const [extractText, setExtractText] = useState("");

  const { data: memories = [], isLoading } = useQuery({
    queryKey: ["memories"],
    queryFn: () => api.memories.list(),
  });

  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
  });

  const createMutation = useMutation({
    mutationFn: (data: { content: string; agent_id?: string; context?: string }) => api.memories.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      closeModal();
      toast.success(t("page.memories.memory_created"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, any> }) => api.memories.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      closeModal();
      toast.success(t("page.memories.memory_updated"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.memories.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      toast.success(t("page.memories.memory_deleted"));
    },
  });

  const archiveMutation = useMutation({
    mutationFn: (id: string) => api.memories.archive(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      toast.success(t("page.memories.memory_archived"));
    },
  });

  const extractMutation = useMutation({
    mutationFn: (text: string) => api.memories.extract(text),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      setExtractModalOpen(false);
      setExtractText("");
      toast.success(t("page.memories.memories_extracted"));
    },
  });

  const allMemories = memories as Memory[];
  const agentsList = agents as Agent[];

  const filtered = useMemo(() => {
    let list = allMemories;
    if (agentFilter) {
      list = list.filter((m) => m.agent_id === agentFilter);
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(
        (m) => m.content.toLowerCase().includes(q) || (m.context || "").toLowerCase().includes(q),
      );
    }
    return list;
  }, [allMemories, search, agentFilter]);

  const agentName = (agentId?: string) => {
    if (!agentId) return t("page.memories.global");
    const agent = agentsList.find((a) => a.id === agentId);
    return agent?.name || t("page.memories.unknown_agent");
  };

  function openCreate() {
    setEditMemory(null);
    setFormContent("");
    setFormAgentId("");
    setFormContext("");
    setModalOpen(true);
  }

  function openEdit(memory: Memory) {
    setEditMemory(memory);
    setFormContent(memory.content);
    setFormAgentId(memory.agent_id || "");
    setFormContext(memory.context || "");
    setModalOpen(true);
  }

  function closeModal() {
    setModalOpen(false);
    setEditMemory(null);
  }

  function handleSubmit() {
    const data = {
      content: formContent,
      agent_id: formAgentId || undefined,
      context: formContext || undefined,
    };

    if (editMemory) {
      updateMutation.mutate({ id: editMemory.id, data });
    } else {
      createMutation.mutate({ content: formContent, agent_id: formAgentId || undefined, context: formContext || undefined });
    }
  }

  return (
    <div style={{ maxWidth: 1060, margin: "0 auto" }}>
      <PageHeader
        title={t("page.memories.agent_memories")}
        subtitle={t("page.memories.subtitle")}
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <Button
              variant="outline"
              onClick={() => setExtractModalOpen(true)}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
              </svg>
              {t("page.memories.extract_memories")}
            </Button>
            <PageHeaderAddButton label={t("page.memories.add_memory")} onClick={openCreate} />
          </div>
        }
      >
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select
            value={agentFilter}
            onChange={(e) => setAgentFilter(e.target.value)}
            className="manor-input"
            style={{ width: 180, height: 36, fontSize: 13 }}
          >
            <option value="">{t("page.memories.all_agents")}</option>
            {agentsList.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.memories.search_placeholder")}
            className="w-full sm:w-64"
          />
        </div>
      </PageHeader>

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}

      {/* Empty */}
      {!isLoading && filtered.length === 0 && (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" />
            </svg>
          }
          title={t("page.memories.no_memories")}
          description={t("page.memories.no_memories_desc")}
        />
      )}

      {/* Memory cards */}
      {!isLoading && filtered.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 320px), 1fr))", gap: 16 }}>
          {filtered.map((memory) => (
            <div
              key={memory.id}
              className="glass-card"
              style={{
                padding: 20,
                display: "flex",
                flexDirection: "column",
                gap: 12,
                transition: "all 0.2s",
              }}
            >
              {/* Header */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{
                    padding: "2px 8px",
                    borderRadius: 6,
                    fontSize: 10,
                    fontWeight: 600,
                    background: "#f5f5f4",
                    color: "#57534e",
                  }}>
                    {agentName(memory.agent_id)}
                  </span>
                  <StatusBadge type={statusBadgeType(memory.status)}>{memory.status}</StatusBadge>
                </div>
                <span style={{ fontSize: 11, color: "#a8a29e" }}>{formatDate(memory.created_at)}</span>
              </div>

              {/* Content preview */}
              <p style={{
                fontSize: 13,
                color: "#44403c",
                lineHeight: 1.6,
                margin: 0,
                display: "-webkit-box",
                WebkitLineClamp: 3,
                WebkitBoxOrient: "vertical" as const,
                overflow: "hidden",
              }}>
                {memory.content}
              </p>

              {/* Context */}
              {memory.context && (
                <p style={{ fontSize: 11, color: "#a8a29e", margin: 0, fontStyle: "italic" }}>
                  {t("page.memories.context")}: {memory.context}
                </p>
              )}

              {/* Actions */}
              <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: "auto" }}>
                <button
                  onClick={() => openEdit(memory)}
                  style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                  title={t("action.edit")}
                >
                  <IconEdit size={16} />
                </button>
                {memory.status === "active" && (
                  <button
                    onClick={() => archiveMutation.mutate(memory.id)}
                    style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                    title={t("action.archive")}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" />
                    </svg>
                  </button>
                )}
                <button
                  onClick={() => setDeleteTarget(memory.id)}
                  style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                  title={t("action.delete")}
                >
                  <IconTrash size={16} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Delete confirm */}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}
        title={t("page.memories.delete_memory")}
        message={t("page.memories.delete_memory_confirm")}
        confirmLabel={t("action.delete")}
        danger
      />

      {/* Create / Edit Modal */}
      <Modal
        open={modalOpen}
        onClose={closeModal}
        title={editMemory ? t("page.memories.edit_memory") : t("page.memories.add_memory")}
        footer={
          <>
            <Button variant="outline" onClick={closeModal}>{t("action.cancel")}</Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              disabled={!formContent.trim() || createMutation.isPending || updateMutation.isPending}
            >
              {createMutation.isPending || updateMutation.isPending ? t("page.memories.saving") : editMemory ? t("action.update") : t("action.create")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Textarea
            label={t("page.memories.content")}
            value={formContent}
            onChange={(e) => setFormContent(e.target.value)}
            rows={4}
            placeholder={t("page.memories.content_placeholder")}
          />
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 4 }}>{t("page.memories.agent_optional")}</label>
            <select value={formAgentId} onChange={(e) => setFormAgentId(e.target.value)} className="manor-input">
              <option value="">{t("page.memories.global_all_agents")}</option>
              {agentsList.map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
          </div>
          <Input
            label={t("page.memories.context_optional")}
            value={formContext}
            onChange={(e) => setFormContext(e.target.value)}
            placeholder={t("page.memories.context_placeholder")}
          />
        </div>
      </Modal>

      {/* Extract Modal */}
      <Modal
        open={extractModalOpen}
        onClose={() => setExtractModalOpen(false)}
        title={t("page.memories.extract_memories")}
        footer={
          <>
            <Button variant="outline" onClick={() => setExtractModalOpen(false)}>{t("action.cancel")}</Button>
            <Button
              variant="primary"
              onClick={() => extractMutation.mutate(extractText)}
              disabled={!extractText.trim() || extractMutation.isPending}
            >
              {extractMutation.isPending ? t("page.memories.extracting") : t("action.extract")}
            </Button>
          </>
        }
      >
        <Textarea
          label={t("page.memories.paste_to_extract")}
          value={extractText}
          onChange={(e) => setExtractText(e.target.value)}
          rows={8}
          placeholder={t("page.memories.extract_placeholder")}
        />
      </Modal>
    </div>
  );
}
