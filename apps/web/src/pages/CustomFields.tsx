import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import type { CustomField } from "../lib/types";
import { useToastStore } from "../stores/toast";
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
import { IconEdit, IconTrash, IconSettings } from "../components/icons";

/* -- constants ------------------------------------------------ */

const FIELD_TYPES = ["text", "number", "date", "select", "boolean"];
const RESOURCE_TYPES = ["tasks", "documents", "agents", "orders", "contacts"];

const FIELD_TYPE_COLORS: Record<string, { bg: string; color: string }> = {
  text:    { bg: "#e3e9f1", color: "#3f57a0" },
  number:  { bg: "#ece9f5", color: "#6443a0" },
  date:    { bg: "#f3ecd6", color: "#936027" },
  select:  { bg: "#dceae3", color: "#3f7361" },
  boolean: { bg: "#f3e5ed", color: "#be185d" },
};

/* -- component ------------------------------------------------ */

export default function CustomFields() {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const [search, setSearch] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editField, setEditField] = useState<CustomField | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  // Form state
  const [formName, setFormName] = useState("");
  const [formFieldType, setFormFieldType] = useState("text");
  const [formResourceType, setFormResourceType] = useState("tasks");
  const [formRequired, setFormRequired] = useState(false);
  const [formSortOrder, setFormSortOrder] = useState("0");
  const [formOptions, setFormOptions] = useState("");

  const { data: fields = [], isLoading } = useQuery({
    queryKey: ["custom-fields"],
    queryFn: () => api.customFields.list(),
  });

  const createMutation = useMutation({
    mutationFn: (data: Record<string, any>) => api.customFields.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["custom-fields"] });
      closeModal();
      toast.success(t("page.custom_fields.toast_created"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, any> }) => api.customFields.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["custom-fields"] });
      closeModal();
      toast.success(t("page.custom_fields.toast_updated"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.customFields.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["custom-fields"] });
      toast.success(t("page.custom_fields.toast_deleted"));
    },
  });

  const allFields = fields as CustomField[];

  const filtered = useMemo(() => {
    if (!search.trim()) return allFields;
    const q = search.toLowerCase();
    return allFields.filter(
      (f) => f.name.toLowerCase().includes(q) || f.resource_type.toLowerCase().includes(q) || f.field_type.toLowerCase().includes(q),
    );
  }, [allFields, search]);

  // Group by resource_type
  const grouped = useMemo(() => {
    const groups: Record<string, CustomField[]> = {};
    for (const f of filtered) {
      if (!groups[f.resource_type]) groups[f.resource_type] = [];
      groups[f.resource_type].push(f);
    }
    // Sort each group by sort_order
    for (const key of Object.keys(groups)) {
      groups[key].sort((a, b) => a.sort_order - b.sort_order);
    }
    return groups;
  }, [filtered]);

  function openCreate() {
    setEditField(null);
    setFormName("");
    setFormFieldType("text");
    setFormResourceType("tasks");
    setFormRequired(false);
    setFormSortOrder("0");
    setFormOptions("");
    setModalOpen(true);
  }

  function openEdit(field: CustomField) {
    setEditField(field);
    setFormName(field.name);
    setFormFieldType(field.field_type);
    setFormResourceType(field.resource_type);
    setFormRequired(field.required);
    setFormSortOrder(String(field.sort_order));
    setFormOptions(field.options ? JSON.stringify(field.options, null, 2) : "");
    setModalOpen(true);
  }

  function closeModal() {
    setModalOpen(false);
    setEditField(null);
  }

  function handleSubmit() {
    let options: Record<string, any> | undefined;
    try {
      if (formOptions.trim()) options = JSON.parse(formOptions);
    } catch {
      /* ignore */
    }

    const data: Record<string, any> = {
      name: formName,
      field_type: formFieldType,
      resource_type: formResourceType,
      required: formRequired,
      sort_order: parseInt(formSortOrder, 10) || 0,
      options,
    };

    if (editField) {
      updateMutation.mutate({ id: editField.id, data });
    } else {
      createMutation.mutate(data);
    }
  }

  return (
    <div style={{ maxWidth: 1060, margin: "0 auto" }}>
      <PageHeader
        title={t("page.custom_fields.title")}
        subtitle={t("page.custom_fields.subtitle")}
        toolbar={(
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.custom_fields.search_placeholder")}
            className="w-full sm:w-64"
          />
        )}
        actions={
          <PageHeaderAddButton label={t("page.custom_fields.add_field")} onClick={openCreate} />
        }
      />

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}

      {/* Empty */}
      {!isLoading && filtered.length === 0 && (
        <EmptyState
          icon={<IconSettings size={32} className="text-stone-300" />}
          title={t("page.custom_fields.no_fields")}
          description={t("page.custom_fields.no_fields_desc")}
        />
      )}

      {/* Grouped tables */}
      {!isLoading && Object.keys(grouped).length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          {Object.entries(grouped).map(([resourceType, fields]) => (
            <div key={resourceType}>
              <h3 style={{ fontSize: 13, fontWeight: 700, color: "#1c1917", textTransform: "capitalize", marginBottom: 8, paddingLeft: 4 }}>
                {resourceType}
                <span style={{ fontSize: 11, fontWeight: 500, color: "#a8a29e", marginLeft: 8 }}>{fields.length} {fields.length === 1 ? t("page.custom_fields.field_singular") : t("page.custom_fields.field_plural")}</span>
              </h3>
              <div className="glass-card" style={{ overflow: "hidden", padding: 0 }}>
                <div style={{ overflowX: "auto" }}>
                  <table className="glass-table" style={{ width: "100%", fontSize: 13 }}>
                    <thead>
                      <tr>
                        <th>{t("page.custom_fields.name")}</th>
                        <th>{t("page.custom_fields.type")}</th>
                        <th>{t("page.custom_fields.required")}</th>
                        <th>{t("page.custom_fields.sort_order")}</th>
                        <th style={{ textAlign: "right" }}>{t("page.custom_fields.actions")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {fields.map((field) => {
                        const typeColor = FIELD_TYPE_COLORS[field.field_type] || { bg: "#f5f5f4", color: "#57534e" };
                        return (
                          <tr key={field.id}>
                            <td style={{ padding: "12px 16px", fontWeight: 600, color: "#292524" }}>{field.name}</td>
                            <td style={{ padding: "12px 16px" }}>
                              <span style={{
                                padding: "2px 8px",
                                borderRadius: 6,
                                fontSize: 11,
                                fontWeight: 600,
                                background: typeColor.bg,
                                color: typeColor.color,
                              }}>
                                {field.field_type}
                              </span>
                            </td>
                            <td style={{ padding: "12px 16px" }}>
                              {field.required ? (
                                <StatusBadge type="warning">{t("page.custom_fields.required")}</StatusBadge>
                              ) : (
                                <span style={{ color: "#a8a29e", fontSize: 12 }}>{t("page.custom_fields.optional")}</span>
                              )}
                            </td>
                            <td style={{ padding: "12px 16px", color: "#78716c", fontSize: 12 }}>{field.sort_order}</td>
                            <td style={{ padding: "12px 16px", textAlign: "right" }}>
                              <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 4 }}>
                                <button
                                  onClick={() => openEdit(field)}
                                  style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                                  title={t("action.edit")}
                                >
                                  <IconEdit size={16} />
                                </button>
                                <button
                                  onClick={() => setDeleteTarget(field.id)}
                                  style={{ padding: 6, borderRadius: 8, border: "none", background: "transparent", cursor: "pointer", color: "#a8a29e" }}
                                  title={t("action.delete")}
                                >
                                  <IconTrash size={16} />
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
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
        title={t("page.custom_fields.delete_title")}
        message={t("page.custom_fields.delete_message")}
        confirmLabel={t("action.delete")}
        danger
      />

      {/* Create / Edit Modal */}
      <Modal
        open={modalOpen}
        onClose={closeModal}
        title={editField ? t("page.custom_fields.edit_title") : t("page.custom_fields.add_title")}
        footer={
          <>
            <Button variant="outline" onClick={closeModal}>{t("action.cancel")}</Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              disabled={!formName.trim() || createMutation.isPending || updateMutation.isPending}
            >
              {createMutation.isPending || updateMutation.isPending ? t("page.custom_fields.saving") : editField ? t("page.custom_fields.update") : t("action.create")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Input label={t("page.custom_fields.name")} value={formName} onChange={(e) => setFormName(e.target.value)} placeholder={t("page.custom_fields.name_placeholder")} />
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 4 }}>{t("page.custom_fields.field_type")}</label>
            <select value={formFieldType} onChange={(e) => setFormFieldType(e.target.value)} className="manor-input">
              {FIELD_TYPES.map((t) => (
                <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>
              ))}
            </select>
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 4 }}>{t("page.custom_fields.resource_type")}</label>
            <select value={formResourceType} onChange={(e) => setFormResourceType(e.target.value)} className="manor-input">
              {RESOURCE_TYPES.map((r) => (
                <option key={r} value={r}>{r.charAt(0).toUpperCase() + r.slice(1)}</option>
              ))}
            </select>
          </div>
          {formFieldType === "select" && (
            <Textarea
              label={t("page.custom_fields.options_json")}
              value={formOptions}
              onChange={(e) => setFormOptions(e.target.value)}
              rows={3}
              placeholder={t("page.custom_fields.choices_low_medium_high")}
            />
          )}
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={formRequired}
              onChange={(e) => setFormRequired(e.target.checked)}
              id="field-required"
              style={{ accentColor: "#4f7d75" }}
            />
            <label htmlFor="field-required" style={{ fontSize: 13, fontWeight: 500, color: "#57534e", cursor: "pointer" }}>{t("page.custom_fields.required_field")}</label>
          </div>
          <Input
            label={t("page.custom_fields.sort_order")}
            type="number"
            value={formSortOrder}
            onChange={(e) => setFormSortOrder(e.target.value)}
            placeholder="0"
          />
        </div>
      </Modal>
    </div>
  );
}
