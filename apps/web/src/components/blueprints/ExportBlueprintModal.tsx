/**
 * Export-as-blueprint modal — opened from a workspace detail page.
 * Captures title/slug/summary/tags + section toggles, then POSTs to
 * /api/v1/workspaces/{id}/export-blueprint and navigates to the new
 * blueprint's detail page on success.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Textarea from "../ui/Textarea";
import Checkbox from "../ui/Checkbox";
import { api, type BlueprintDetail } from "../../lib/api";
import {
  formatBlueprintVariableValue,
  parseBlueprintVariableValue,
} from "../../lib/blueprintVariables.mjs";
import { useToastStore } from "../../stores/toast";
import { t } from "../../lib/i18n";

const KNOWLEDGE_PAGE_SIZE = 50;
const KNOWLEDGE_SELECTION_LIMIT = 64;

interface Props {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  workspaceName: string;
}

interface SectionToggle {
  key:
    | "include_subscriptions"
    | "include_goals"
    | "include_stats"
    | "include_scheduled_jobs"
    | "include_workflows"
    | "include_custom_fields"
    | "include_governance"
    | "include_channel_requirements"
    | "include_session_requirements"
    | "include_embedded_agents"
    | "include_embedded_skills"
    | "include_knowledge_packs"
    | "include_starter_memory"
    | "include_memory_files";
  label: string;
  defaultOn: boolean;
  hint: string;
}

interface PersonalizationField {
  id: string;
  key: string;
  label: string;
  purpose: string;
  required: boolean;
  defaultValue: string;
  defaultReference: unknown;
  materialize: boolean;
}

let personalizationFieldSequence = 0;

function newPersonalizationField(): PersonalizationField {
  personalizationFieldSequence += 1;
  return {
    id: `personalization-${personalizationFieldSequence}`,
    key: "",
    label: "",
    purpose: "",
    required: false,
    defaultValue: "",
    defaultReference: undefined,
    materialize: true,
  };
}

function personalizationFieldsFromBlueprint(
  blueprint: BlueprintDetail,
): PersonalizationField[] {
  const contract = blueprint.payload.contract;
  if (!contract || typeof contract !== "object" || Array.isArray(contract)) return [];
  const variables = (contract as Record<string, unknown>).variables;
  if (!Array.isArray(variables)) return [];
  return variables.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const declaration = value as Record<string, unknown>;
    if (typeof declaration.key !== "string" || typeof declaration.label !== "string") {
      return [];
    }
    return [{
      ...newPersonalizationField(),
      key: declaration.key,
      label: declaration.label,
      purpose: typeof declaration.purpose === "string" ? declaration.purpose : "",
      required: declaration.required === true,
      defaultValue: formatBlueprintVariableValue(declaration.default),
      defaultReference: declaration.default,
      materialize: declaration.materialize !== false,
    }];
  });
}

function serializePersonalizationField(field: PersonalizationField) {
  const parsedDefault = field.defaultValue.length > 0
    ? parseBlueprintVariableValue(field.defaultValue, field.defaultReference)
    : { ok: true as const, value: undefined };
  if (!parsedDefault.ok) {
    throw new Error("Blueprint personalization contains an invalid typed default.");
  }
  return {
    key: field.key.trim(),
    label: field.label.trim(),
    purpose: field.purpose.trim() || undefined,
    required: field.required,
    default: parsedDefault.value,
    materialize: field.materialize,
  };
}

const SECTIONS: SectionToggle[] = [
  { key: "include_subscriptions",         label: t("component.export_blueprint_modal.agent_subscriptions"),  defaultOn: true,  hint: "Service-key → agent bindings (resolved by slug on install)." },
  { key: "include_goals",                 label: t("nav.goals"),                defaultOn: true,  hint: "Targets + measurement schedule. Runtime values are stripped." },
  { key: "include_stats",                 label: t("page.workspace_stats.title"), defaultOn: true,  hint: "Collection definitions only. Observation history is never exported." },
  { key: "include_scheduled_jobs",        label: t("page.blueprint_detail.scheduled_jobs"),       defaultOn: true,  hint: "Cron triggers (last_run_at dropped)." },
  { key: "include_workflows",             label: "Workflows",                  defaultOn: true,  hint: "Workspace workflow graphs, variables, and active bindings." },
  { key: "include_custom_fields",         label: t("component.export_blueprint_modal.custom_field_defs"),    defaultOn: true,  hint: "Per-workspace field schemas for tasks/clients." },
  { key: "include_governance",            label: t("page.blueprint_detail.governance_policy"),    defaultOn: true,  hint: "Current policy snapshot — operator picks a preset overlay on install." },
  { key: "include_channel_requirements",  label: t("component.export_blueprint_modal.channel_requirements"), defaultOn: true,  hint: "Channel types and service routing; account identities and credentials are never copied." },
  { key: "include_session_requirements",  label: t("page.blueprint_detail.browser_sessions"),     defaultOn: true,  hint: "Only referenced provider+label requirements; credentials are never copied." },
  { key: "include_embedded_agents",       label: "Private Agents",             defaultOn: true,  hint: "Embed subscribed private Agents; public Agents remain catalog requirements." },
  { key: "include_embedded_skills",       label: "Private Skills",             defaultOn: true,  hint: "Embed private Skills bound to exported Agents." },
  { key: "include_starter_memory",        label: "Agent starter memory",       defaultOn: false, hint: "Include eligible agent-level memory; private and confidential entries stay excluded." },
  { key: "include_knowledge_packs",       label: "Knowledge structure",        defaultOn: true,  hint: "Export Workspace Knowledge groups and safe document paths." },
  { key: "include_memory_files",          label: t("component.export_blueprint_modal.memory_md_files"),      defaultOn: false, hint: "Include bodies only for public, clean, non-PII Markdown stored as Knowledge text." },
];

function defaultSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function formatPortableSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${Math.ceil(bytes / 1024)} KB`;
}

export default function ExportBlueprintModal({ open, onClose, workspaceId, workspaceName }: Props) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const [title, setTitle] = useState("");
  const [slug, setSlug] = useState("");
  const [summary, setSummary] = useState("");
  const [description, setDescription] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  const [authorHandle, setAuthorHandle] = useState("");
  const [personalizationFields, setPersonalizationFields] = useState<PersonalizationField[]>([]);
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [includes, setIncludes] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(SECTIONS.map((s) => [s.key, s.defaultOn])),
  );
  const [selectedKnowledgeDocumentIds, setSelectedKnowledgeDocumentIds] = useState<string[]>([]);
  const [knowledgeOffset, setKnowledgeOffset] = useState(0);
  const [knowledgeSelectionInitialized, setKnowledgeSelectionInitialized] = useState(false);
  const [existingBlueprintInitialized, setExistingBlueprintInitialized] = useState(false);

  const existingBlueprintQuery = useQuery({
    queryKey: ["blueprint-export-existing", workspaceId],
    queryFn: async () => {
      const blueprints = await api.blueprints.list();
      const matches = blueprints.filter((blueprint) => (
        blueprint.is_owner === true
        && blueprint.source_workspace_id === workspaceId
      ));
      if (matches.length > 1) {
        throw new Error("Multiple Marketplace Blueprints are linked to this Workspace.");
      }
      return matches[0] ? api.blueprints.get(matches[0].id) : null;
    },
    enabled: open,
    staleTime: 0,
    retry: false,
  });

  const knowledgeDocumentsQuery = useQuery({
    queryKey: ["blueprint-export-knowledge-documents", workspaceId, knowledgeOffset],
    queryFn: () => api.workspaces.blueprintExportKnowledgeDocuments(workspaceId, KNOWLEDGE_PAGE_SIZE, knowledgeOffset),
    enabled: open && !!includes.include_knowledge_packs,
    staleTime: 30_000,
  });

  // Re-prime defaults when re-opened.
  useEffect(() => {
    if (open) {
      setTitle(workspaceName);
      setSlug(defaultSlug(workspaceName));
      setSummary("");
      setDescription("");
      setTagsInput("");
      setAuthorHandle("");
      setPersonalizationFields([]);
      setReplaceExisting(false);
      setIncludes(Object.fromEntries(SECTIONS.map((s) => [s.key, s.defaultOn])));
      setSelectedKnowledgeDocumentIds([]);
      setKnowledgeOffset(0);
      setKnowledgeSelectionInitialized(false);
      setExistingBlueprintInitialized(false);
    }
  }, [open, workspaceName, workspaceId]);

  useEffect(() => {
    if (!open || existingBlueprintInitialized || !existingBlueprintQuery.isSuccess) return;
    const existing = existingBlueprintQuery.data;
    if (existing) {
      setTitle(existing.title);
      setSlug(existing.slug);
      setSummary(existing.summary ?? "");
      setDescription(existing.description ?? "");
      setTagsInput(existing.tags.join(", "));
      setAuthorHandle(existing.author_handle ?? "");
      setPersonalizationFields(personalizationFieldsFromBlueprint(existing));
      setReplaceExisting(true);
    }
    setExistingBlueprintInitialized(true);
  }, [existingBlueprintInitialized, existingBlueprintQuery.data, existingBlueprintQuery.isSuccess, open]);

  useEffect(() => {
    const candidates = knowledgeDocumentsQuery.data;
    if (!open || !includes.include_memory_files || !candidates || knowledgeOffset !== 0) return;
    if (!knowledgeSelectionInitialized) {
      setSelectedKnowledgeDocumentIds(candidates.map((document) => document.id).slice(0, KNOWLEDGE_SELECTION_LIMIT));
      setKnowledgeSelectionInitialized(true);
    }
  }, [
    includes.include_memory_files,
    knowledgeDocumentsQuery.data,
    knowledgeOffset,
    knowledgeSelectionInitialized,
    open,
  ]);

  const exportMutation = useMutation({
    mutationFn: () => {
      const includeKnowledgeBodies = (
        !!includes.include_memory_files
        && selectedKnowledgeDocumentIds.length > 0
      );
      return api.workspaces.exportBlueprint(workspaceId, {
        ...includes,
        slug,
        title,
        summary: summary || undefined,
        description: description || undefined,
        tags: tagsInput.split(",").map((t) => t.trim()).filter(Boolean),
        author_handle: authorHandle || undefined,
        marketplace_blueprint_id: existingBlueprintQuery.data?.id,
        install_variables: personalizationFields.map(serializePersonalizationField),
        include_memory_files: includeKnowledgeBodies,
        knowledge_pack_mode: includeKnowledgeBodies ? "inline_text" : "skeleton",
        knowledge_document_ids: includeKnowledgeBodies
          ? selectedKnowledgeDocumentIds
          : [],
        replace_existing: replaceExisting,
      });
    },
    onSuccess: (bp) => {
      queryClient.invalidateQueries({ queryKey: ["blueprints"] });
      toast.success(t("component.export_blueprint_modal.blueprint_draft_created"));
      onClose();
      navigate(`/blueprints/${bp.id}`);
    },
    onError: (e: Error) => {
      toast.error(`Export failed: ${e.message}`);
    },
  });

  const slugValid = /^[a-z0-9][a-z0-9_-]{1,118}[a-z0-9]$/.test(slug);
  const personalizationKeys = personalizationFields.map((field) => field.key.trim());
  const personalizationValid = personalizationFields.every((field) => (
    /^[A-Za-z_][A-Za-z0-9_.-]{0,99}$/.test(field.key.trim())
    && field.label.trim().length > 0
    && personalizationKeys.filter((key) => key === field.key.trim()).length === 1
    && (
      field.defaultValue.length === 0
      || parseBlueprintVariableValue(field.defaultValue, field.defaultReference).ok
    )
  ));
  const knowledgeSelectionUnavailable = (
    !!includes.include_memory_files
    && (knowledgeDocumentsQuery.isPending || knowledgeDocumentsQuery.isError)
  );
  const canSubmit = (
    title.trim().length > 0
    && slugValid
    && personalizationValid
    && existingBlueprintInitialized
    && !existingBlueprintQuery.isError
    && !knowledgeSelectionUnavailable
    && !exportMutation.isPending
  );
  const knowledgeDocuments = knowledgeDocumentsQuery.data || [];
  const selectedKnowledgeIds = new Set(selectedKnowledgeDocumentIds);
  const allKnowledgeDocumentsSelected = (
    knowledgeDocuments.length > 0
    && knowledgeDocuments.every((document) => selectedKnowledgeIds.has(document.id))
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("component.export_blueprint_modal.export_workspace_as_blueprint")}
      maxWidth="640px"
      footer={
        <>
          <Button variant="outline" onClick={onClose}>{t("action.cancel")}</Button>
          <Button
            variant="primary"
            disabled={!canSubmit}
            loading={exportMutation.isPending}
            onClick={() => exportMutation.mutate()}
          >
            {t("component.export_blueprint_modal.create_draft")}</Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ background: "var(--surface-muted)", padding: 12, borderRadius: "var(--radius-control)", fontSize: 12, color: "var(--text-default)" }}>
          {t("component.export_blueprint_modal.a_blueprint_is_a_portable_json_document_secrets_runtim")}</div>

        {existingBlueprintQuery.isError && (
          <div role="alert" style={{ padding: 10, borderRadius: "var(--radius-control)", background: "var(--surface-muted)", color: "var(--text-default)", fontSize: 12 }}>
            {t("component.export_blueprint_modal.failed_to_load_existing_blueprint")}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => existingBlueprintQuery.refetch()}
              style={{ marginLeft: 6 }}
            >
              {t("component.export_blueprint_modal.retry")}
            </Button>
          </div>
        )}

        <Input
          label={t("page.client_portal.field_title")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t("component.export_blueprint_modal.x_growth_calvin_s_recipe")}
        />

        <div>
          <Input
            label={t("component.export_blueprint_modal.slug_used_in_urls_when_sharing")}
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="x-growth-v1"
          />
          {!slugValid && slug.length > 0 && (
            <div style={{ fontSize: 11, color: "rgb(193, 74, 68)", marginTop: 4 }}>
              {t("component.export_blueprint_modal.lowercase_a_z_0_9_hyphens_underscores_3_120_chars_no_l")}</div>
          )}
        </div>

        <Textarea label={t("component.export_blueprint_modal.summary_one_line")} value={summary} onChange={(e) => setSummary(e.target.value)} rows={2} />
        <Textarea label={t("page.task_collections.description")} value={description} onChange={(e) => setDescription(e.target.value)} rows={4} />
        <Input
          label={t("page.blueprint_detail.tags_csv")}
          value={tagsInput}
          onChange={(e) => setTagsInput(e.target.value)}
          placeholder="social_media, growth"
        />
        <Input
          label={t("component.export_blueprint_modal.author_handle_shown_to_installers")}
          value={authorHandle}
          onChange={(e) => setAuthorHandle(e.target.value)}
          placeholder="calvin"
        />

        <div>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 8 }}>
            <div>
              <h4 style={{ fontSize: 13, fontWeight: 600, margin: 0 }}>
                {t("component.export_blueprint_modal.installer_personalization")}
              </h4>
              <div style={{ color: "var(--text-muted)", fontSize: 11, marginTop: 2 }}>
                {t("component.export_blueprint_modal.personalization_creator_help")}
              </div>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setPersonalizationFields((current) => [
                ...current,
                newPersonalizationField(),
              ])}
            >
              {t("component.export_blueprint_modal.add_personalization_field")}
            </Button>
          </div>
          {personalizationFields.length === 0 ? (
            <div style={{ padding: 10, borderRadius: "var(--radius-control)", background: "var(--surface-muted)", color: "var(--text-muted)", fontSize: 12 }}>
              {t("component.export_blueprint_modal.no_personalization_questions")}
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {personalizationFields.map((field, index) => {
                const duplicateKey = (
                  field.key.trim().length > 0
                  && personalizationKeys.filter((key) => key === field.key.trim()).length > 1
                );
                const updateField = (patch: Partial<PersonalizationField>) => {
                  setPersonalizationFields((current) => current.map((item) => (
                    item.id === field.id ? { ...item, ...patch } : item
                  )));
                };
                return (
                  <div
                    key={field.id}
                    style={{ background: "var(--surface-muted)", borderRadius: "var(--radius-control)", padding: 10 }}
                  >
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 10 }}>
                      <Input
                        label={t("component.export_blueprint_modal.personalization_field_key", { index: index + 1 })}
                        value={field.key}
                        onChange={(event) => updateField({ key: event.target.value })}
                        placeholder="company_name"
                        required
                        error={
                          duplicateKey
                            ? t("component.export_blueprint_modal.personalization_key_unique")
                            : field.key.length > 0 && !/^[A-Za-z_][A-Za-z0-9_.-]{0,99}$/.test(field.key.trim())
                              ? t("component.export_blueprint_modal.personalization_key_format")
                              : undefined
                        }
                      />
                      <Input
                        label={t("component.export_blueprint_modal.personalization_question_label")}
                        value={field.label}
                        onChange={(event) => updateField({ label: event.target.value })}
                        placeholder="Company name"
                        required
                      />
                      <Input
                        label={t("component.export_blueprint_modal.personalization_default_value")}
                        value={field.defaultValue}
                        onChange={(event) => updateField({ defaultValue: event.target.value })}
                        placeholder={t("page.workspace_draft_chat.value.optional")}
                        error={
                          field.defaultValue.length > 0
                          && !parseBlueprintVariableValue(field.defaultValue, field.defaultReference).ok
                            ? t("component.install_blueprint_modal.invalid_typed_value")
                            : undefined
                        }
                      />
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 10, alignItems: "end", marginTop: 10 }}>
                      <Input
                        label={t("component.export_blueprint_modal.personalization_purpose")}
                        value={field.purpose}
                        onChange={(event) => updateField({ purpose: event.target.value })}
                        placeholder={t("component.export_blueprint_modal.personalization_purpose_placeholder")}
                      />
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setPersonalizationFields((current) => (
                          current.filter((item) => item.id !== field.id)
                        ))}
                      >
                        {t("component.export_blueprint_modal.remove_personalization_field")}
                      </Button>
                    </div>
                    <Checkbox
                      checked={field.required}
                      onChange={(required) => updateField({ required })}
                      label={t("component.export_blueprint_modal.personalization_required_before_creation")}
                      style={{ marginTop: 8 }}
                    />
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div style={{ padding: "2px 0" }}>
          <Checkbox
            checked={replaceExisting}
            onChange={setReplaceExisting}
            disabled={Boolean(existingBlueprintQuery.data)}
            label={(
              <span>
                <span style={{ display: "block", fontWeight: 600, color: "var(--text-strong)" }}>
                  Replace an existing draft with this slug
                </span>
                <span style={{ display: "block", marginTop: 2, fontSize: 11, color: "var(--text-muted)" }}>
                  Only an editable Blueprint previously exported from this Workspace can be replaced.
                </span>
              </span>
            )}
          />
        </div>

        <div>
          <h4 style={{ fontSize: 13, fontWeight: 600, margin: "8px 0 8px" }}>{t("component.export_blueprint_modal.what_to_include")}</h4>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {SECTIONS.map((s) => {
              const showKnowledgeSelector = (
                s.key === "include_memory_files"
                && !!includes.include_memory_files
                && !!includes.include_knowledge_packs
              );
              return (
                <div
                  key={s.key}
                  style={{
                    padding: 8,
                    borderRadius: "var(--radius-control)",
                    background: includes[s.key] ? "var(--surface-muted)" : "transparent",
                    fontSize: 12,
                  }}
                >
                  <Checkbox
                    checked={!!includes[s.key]}
                    disabled={
                      (s.key === "include_starter_memory" && !includes.include_embedded_agents)
                      || (s.key === "include_memory_files" && !includes.include_knowledge_packs)
                    }
                    onChange={(checked) => {
                      setIncludes((previous) => ({
                        ...previous,
                        [s.key]: checked,
                        ...(s.key === "include_knowledge_packs" && !checked
                          ? { include_memory_files: false }
                          : {}),
                        ...(s.key === "include_embedded_agents" && !checked
                          ? { include_starter_memory: false }
                          : {}),
                      }));
                    }}
                    style={{ width: "100%", alignItems: "flex-start" }}
                    label={(
                      <span style={{ minWidth: 0 }}>
                        <span style={{ display: "block", fontWeight: 500, color: "var(--text-strong)" }}>{s.label}</span>
                        <span style={{ display: "block", color: "var(--text-muted)", fontSize: 11, marginTop: 2 }}>{s.hint}</span>
                      </span>
                    )}
                  />

                  {showKnowledgeSelector && (
                    <div
                      role="group"
                      aria-label={t("component.export_blueprint_modal.select_knowledge_files")}
                      style={{
                        margin: "10px 0 2px 23px",
                        padding: 10,
                        borderRadius: "var(--radius-control)",
                        background: "var(--surface-sunken)",
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                        <span style={{ color: "var(--text-strong)", fontWeight: 600 }}>
                          {t("component.export_blueprint_modal.knowledge_selection_limit", {
                            selected: selectedKnowledgeDocumentIds.length,
                            limit: KNOWLEDGE_SELECTION_LIMIT,
                          })}
                        </span>
                        {knowledgeDocuments.length > 0 && (
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={!allKnowledgeDocumentsSelected && selectedKnowledgeDocumentIds.length >= KNOWLEDGE_SELECTION_LIMIT}
                            onClick={() => setSelectedKnowledgeDocumentIds((current) => (
                              allKnowledgeDocumentsSelected
                                ? current.filter((id) => !knowledgeDocuments.some((document) => document.id === id))
                                : [...new Set([...current, ...knowledgeDocuments.map((document) => document.id)])].slice(0, KNOWLEDGE_SELECTION_LIMIT)
                            ))}
                          >
                            {allKnowledgeDocumentsSelected
                              ? t("component.export_blueprint_modal.clear_page")
                              : t("component.export_blueprint_modal.select_page")}
                          </Button>
                        )}
                      </div>

                      {knowledgeDocumentsQuery.isPending && (
                        <div aria-live="polite" style={{ marginTop: 8, color: "var(--text-muted)" }}>
                          {t("component.export_blueprint_modal.loading_knowledge_files")}
                        </div>
                      )}
                      {knowledgeDocumentsQuery.isError && (
                        <div role="alert" style={{ marginTop: 8, color: "var(--text-default)" }}>
                          {t("component.export_blueprint_modal.failed_to_load_knowledge_files")}
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => knowledgeDocumentsQuery.refetch()}
                            style={{ marginLeft: 6 }}
                          >
                            {t("component.export_blueprint_modal.retry")}
                          </Button>
                        </div>
                      )}
                      {knowledgeDocumentsQuery.isSuccess && knowledgeDocuments.length === 0 && (
                        <div style={{ marginTop: 8, color: "var(--text-muted)" }}>
                          {t("component.export_blueprint_modal.no_eligible_knowledge_files")}
                        </div>
                      )}
                      {knowledgeDocuments.length > 0 && (
                        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8, maxHeight: 220, overflowY: "auto" }}>
                          {knowledgeDocuments.map((document) => (
                            <Checkbox
                              key={document.id}
                              size="sm"
                              checked={selectedKnowledgeIds.has(document.id)}
                              disabled={!selectedKnowledgeIds.has(document.id) && selectedKnowledgeDocumentIds.length >= KNOWLEDGE_SELECTION_LIMIT}
                              onChange={(checked) => setSelectedKnowledgeDocumentIds((current) => (
                                checked
                                  ? [...new Set([...current, document.id])].slice(0, KNOWLEDGE_SELECTION_LIMIT)
                                  : current.filter((documentId) => documentId !== document.id)
                              ))}
                              style={{ width: "100%", alignItems: "flex-start" }}
                              label={(
                                <span style={{ minWidth: 0 }}>
                                  <span style={{ display: "block", color: "var(--text-strong)", overflowWrap: "anywhere" }}>
                                    {document.path}
                                  </span>
                                  <span style={{ display: "block", marginTop: 2, color: "var(--text-muted)", fontSize: 11, overflowWrap: "anywhere" }}>
                                    {document.groups.map((group) => group.name).join(", ")}{document.groups_truncated ? ", …" : ""} · {formatPortableSize(document.file_size)}
                                  </span>
                                </span>
                              )}
                            />
                          ))}
                        </div>
                      )}
                      {(knowledgeOffset > 0 || knowledgeDocuments.length === KNOWLEDGE_PAGE_SIZE) && (
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginTop: 8 }}>
                          <Button variant="ghost" size="sm" disabled={knowledgeOffset === 0 || knowledgeDocumentsQuery.isFetching} onClick={() => setKnowledgeOffset((offset) => Math.max(0, offset - KNOWLEDGE_PAGE_SIZE))}>
                            {t("component.export_blueprint_modal.previous_page")}
                          </Button>
                          <span aria-live="polite" style={{ color: "var(--text-muted)" }}>
                            {t("component.export_blueprint_modal.page_number", { page: knowledgeOffset / KNOWLEDGE_PAGE_SIZE + 1 })}
                          </span>
                          <Button variant="ghost" size="sm" disabled={!knowledgeDocumentsQuery.isSuccess || knowledgeDocumentsQuery.isFetching || knowledgeDocuments.length < KNOWLEDGE_PAGE_SIZE} onClick={() => setKnowledgeOffset((offset) => offset + KNOWLEDGE_PAGE_SIZE)}>
                            {t("component.export_blueprint_modal.next_page")}
                          </Button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </Modal>
  );
}
