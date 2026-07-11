import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { TaskCategory, Task } from "../lib/types";
import { formatDateOnly as formatDate } from "../lib/format";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import { IconFolder, IconTrash } from "../components/icons";
import { t } from "../lib/i18n";

/* -- Create / Edit Modal Content ----------------------------- */

function CollectionModalContent({
  initial,
  onSave,
}: {
  initial?: TaskCategory | null;
  onSave: (data: { name: string; description: string; color: string }) => void;
  saving: boolean;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [color, setColor] = useState(initial?.color ?? "#6d6fb2");

  return (
    <form
      id="collection-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) onSave({ name, description, color });
      }}
      style={{ display: "flex", flexDirection: "column", gap: 16 }}
    >
      <Input label={t("page.task_collections.name")} value={name} onChange={(e) => setName(e.target.value)} placeholder={t("page.task_collections.collection_name")} />

      <Textarea label={t("page.task_collections.description")} value={description} onChange={(e) => setDescription(e.target.value)} rows={3} placeholder={t("page.task_collections.optional_description")} />

      <div>
        <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#44403c", marginBottom: 4 }}>{t("page.task_collections.color")}</label>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value)}
            style={{ height: 36, width: 36, borderRadius: 10, border: "1px solid rgba(28,25,23,0.06)", cursor: "pointer" }}
          />
          <span style={{ fontSize: 12, color: "#a8a29e" }}>{color}</span>
        </div>
      </div>
    </form>
  );
}

/* -- Main Page ---------------------------------------------- */

const COLLECTION_TASK_PAGE_SIZE = 200;

async function loadTasksForCollections(): Promise<Task[]> {
  const tasks: Task[] = [];
  let offset = 0;

  for (let guard = 0; guard < 25; guard += 1) {
    const page = await api.tasks.list({ limit: COLLECTION_TASK_PAGE_SIZE, offset });
    tasks.push(...page.items);
    if (tasks.length >= page.total || page.items.length < COLLECTION_TASK_PAGE_SIZE) break;
    offset += COLLECTION_TASK_PAGE_SIZE;
  }

  return tasks;
}

export default function TaskCollections() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<TaskCategory | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const { data: categories = [], isLoading } = useQuery({
    queryKey: ["taskCategories"],
    queryFn: () => api.tasks.categories.list(),
  });

  const { data: tasks = [] } = useQuery({
    queryKey: ["tasksForCollections"],
    queryFn: loadTasksForCollections,
  });

  const createMut = useMutation({
    mutationFn: (data: Partial<TaskCategory>) => api.tasks.categories.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["taskCategories"] });
      setModalOpen(false);
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<TaskCategory> }) =>
      api.tasks.categories.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["taskCategories"] });
      setEditing(null);
      setModalOpen(false);
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.tasks.categories.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["taskCategories"] });
      setConfirmDelete(null);
    },
  });

  function tasksForCategory(catId: string): Task[] {
    return tasks.filter((t) => t.task_type === catId || (t.details as any)?.category_id === catId);
  }

  function countByStatus(catTasks: Task[], status: string) {
    return catTasks.filter((t) => t.status === status).length;
  }

  function handleSave(data: { name: string; description: string; color: string }) {
    if (editing) {
      updateMut.mutate({ id: editing.id, data });
    } else {
      createMut.mutate(data);
    }
  }

  function openEdit(cat: TaskCategory) {
    setEditing(cat);
    setModalOpen(true);
  }

  function openCreate() {
    setEditing(null);
    setModalOpen(true);
  }

  return (
    <div>
      <PageHeader
        title={t("nav.taskCollections")}
        subtitle={t("page.task_collections.subtitle")}
        actions={
          <PageHeaderAddButton label={t("page.task_collections.add_collection")} onClick={openCreate} />
        }
      />

      {isLoading ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "48px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      ) : categories.length === 0 ? (
        <EmptyState
          icon={
            <IconFolder size={32} className="text-stone-300" />
          }
          title={t("page.task_collections.no_collections")}
          action={
            <button onClick={openCreate} style={{ fontSize: 13, fontWeight: 600, color: "#436b65", background: "transparent", border: "none", cursor: "pointer" }}>
              {t("page.task_collections.create_first")}
            </button>
          }
        />
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 16 }}>
          {categories.map((cat) => {
            const catTasks = tasksForCategory(cat.id);
            const pending = countByStatus(catTasks, "pending");
            const inProgress = countByStatus(catTasks, "in_progress");
            const completed = countByStatus(catTasks, "completed");

            return (
              <div
                key={cat.id}
                onClick={() => navigate(`/tasks?collection=${cat.id}`)}
                onMouseEnter={() => setHoveredId(cat.id)}
                onMouseLeave={() => setHoveredId(null)}
                style={{
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 32,
                  padding: 0,
                  cursor: "pointer",
                  transition: "all 0.25s ease",
                  borderColor: hoveredId === cat.id ? "var(--card-hover-border)" : "rgba(28,25,23,0.06)",
                  background: hoveredId === cat.id ? "var(--card-hover-bg)" : "rgba(255,255,255,0.65)",
                  transform: hoveredId === cat.id ? "var(--card-hover-transform)" : "none",
                  boxShadow: hoveredId === cat.id ? "var(--card-hover-shadow)" : "none",
                  overflow: "hidden",
                }}
              >
                {/* Card body */}
                <div style={{ padding: 20 }}>
                  <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                    <div
                      style={{
                        width: 40,
                        height: 40,
                        borderRadius: 12,
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        flexShrink: 0,
                        backgroundColor: (cat.color || "#6d6fb2") + "20",
                      }}
                    >
                      <span style={{ color: cat.color || "#6d6fb2" }}>
                        <IconFolder size={20} />
                      </span>
                    </div>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{cat.name}</h3>
                      {cat.description && (
                        <p style={{ fontSize: 12, color: "#78716c", marginTop: 2, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{cat.description}</p>
                      )}
                      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8, fontSize: 12, color: "#a8a29e" }}>
                        <span>{catTasks.length} {catTasks.length !== 1 ? t("page.task_collections.tasks_plural") : t("page.task_collections.task_singular")}</span>
                        <span>{formatDate(cat.created_at)}</span>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Card footer */}
                <div style={{ padding: "12px 20px", borderTop: "1px solid rgba(28,25,23,0.06)", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    {pending > 0 && (
                      <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "#b27c34" }}>
                        <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#cf9b44" }} />
                        {pending}
                      </span>
                    )}
                    {inProgress > 0 && (
                      <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "#4869ac" }}>
                        <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#5f84bd" }} />
                        {inProgress}
                      </span>
                    )}
                    {completed > 0 && (
                      <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "#44895f" }}>
                        <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#54a176" }} />
                        {completed}
                      </span>
                    )}
                    {catTasks.length === 0 && (
                      <span style={{ fontSize: 12, color: "#a8a29e" }}>{t("page.task_collections.no_tasks")}</span>
                    )}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 4 }} onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() => openEdit(cat)}
                      style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e", transition: "all 0.2s" }}
                      title={t("action.edit")}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125" />
                      </svg>
                    </button>
                    <button
                      onClick={() => setConfirmDelete(cat.id)}
                      style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e", transition: "all 0.2s" }}
                      title={t("action.delete")}
                    >
                      <IconTrash size={14} />
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Create/Edit modal */}
      <Modal
        open={modalOpen}
        onClose={() => { setModalOpen(false); setEditing(null); }}
        title={editing ? t("page.task_collections.edit_collection") : t("page.task_collections.create_collection")}
        footer={
          <>
            <Button variant="outline" onClick={() => { setModalOpen(false); setEditing(null); }}>{t("action.cancel")}</Button>
            <button
              type="submit"
              form="collection-form"
              disabled={createMut.isPending || updateMut.isPending}
              className="btn-manor"
              style={{ opacity: createMut.isPending || updateMut.isPending ? 0.5 : 1 }}
            >
              {createMut.isPending || updateMut.isPending ? t("page.task_collections.saving") : editing ? t("page.task_collections.update") : t("action.create")}
            </button>
          </>
        }
      >
        <CollectionModalContent initial={editing} onSave={handleSave} saving={createMut.isPending || updateMut.isPending} />
      </Modal>

      {/* Delete confirmation */}
      <ConfirmDialog
        open={!!confirmDelete}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => {
          if (confirmDelete) deleteMut.mutate(confirmDelete);
        }}
        title={t("page.task_collections.delete_collection")}
        message={t("page.task_collections.delete_message")}
        confirmLabel={deleteMut.isPending ? t("page.task_collections.deleting") : t("action.delete")}
        danger
      />
    </div>
  );
}
