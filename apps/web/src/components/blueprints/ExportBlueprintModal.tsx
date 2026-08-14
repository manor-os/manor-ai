/**
 * Export-as-blueprint modal — opened from a workspace detail page.
 * Captures title/slug/summary/tags + section toggles, then POSTs to
 * /api/v1/workspaces/{id}/export-blueprint and navigates to the new
 * blueprint's detail page on success.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Textarea from "../ui/Textarea";
import Checkbox from "../ui/Checkbox";
import { api } from "../../lib/api";
import { useToastStore } from "../../stores/toast";
import { t } from "../../lib/i18n";


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

const SECTIONS: SectionToggle[] = [
  { key: "include_subscriptions",         label: t("component.export_blueprint_modal.agent_subscriptions"),  defaultOn: true,  hint: "Service-key → agent bindings (resolved by slug on install)." },
  { key: "include_goals",                 label: t("nav.goals"),                defaultOn: true,  hint: "Targets + measurement schedule. Runtime values are stripped." },
  { key: "include_stats",                 label: t("page.workspace_stats.title"), defaultOn: true,  hint: "Collection definitions only. Observation history is never exported." },
  { key: "include_scheduled_jobs",        label: t("page.blueprint_detail.scheduled_jobs"),       defaultOn: true,  hint: "Cron triggers (last_run_at dropped)." },
  { key: "include_workflows",             label: "Workflows",                  defaultOn: true,  hint: "Workspace workflow graphs, variables, and active bindings." },
  { key: "include_custom_fields",         label: t("component.export_blueprint_modal.custom_field_defs"),    defaultOn: true,  hint: "Per-workspace field schemas for tasks/clients." },
  { key: "include_governance",            label: t("page.blueprint_detail.governance_policy"),    defaultOn: true,  hint: "Current policy snapshot — operator picks a preset overlay on install." },
  { key: "include_channel_requirements",  label: t("component.export_blueprint_modal.channel_requirements"), defaultOn: true,  hint: "Just the *types* of channel needed — no credentials." },
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
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [includes, setIncludes] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(SECTIONS.map((s) => [s.key, s.defaultOn])),
  );

  // Re-prime defaults when re-opened.
  useEffect(() => {
    if (open) {
      setTitle(workspaceName);
      setSlug(defaultSlug(workspaceName));
      setSummary("");
      setDescription("");
      setTagsInput("");
      setAuthorHandle("");
      setReplaceExisting(false);
      setIncludes(Object.fromEntries(SECTIONS.map((s) => [s.key, s.defaultOn])));
    }
  }, [open, workspaceName]);

  const exportMutation = useMutation({
    mutationFn: () =>
      api.workspaces.exportBlueprint(workspaceId, {
        slug,
        title,
        summary: summary || undefined,
        description: description || undefined,
        tags: tagsInput.split(",").map((t) => t.trim()).filter(Boolean),
        author_handle: authorHandle || undefined,
        knowledge_pack_mode: includes.include_memory_files ? "inline_text" : "skeleton",
        replace_existing: replaceExisting,
        ...includes,
      }),
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
  const canSubmit = title.trim().length > 0 && slugValid && !exportMutation.isPending;

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

        <div style={{ padding: "2px 0" }}>
          <Checkbox
            checked={replaceExisting}
            onChange={setReplaceExisting}
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
            {SECTIONS.map((s) => (
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
                  onChange={(checked) => setIncludes((previous) => ({
                    ...previous,
                    [s.key]: checked,
                    ...(s.key === "include_knowledge_packs" && !checked
                      ? { include_memory_files: false }
                      : {}),
                    ...(s.key === "include_embedded_agents" && !checked
                      ? { include_starter_memory: false }
                      : {}),
                  }))}
                  style={{ width: "100%", alignItems: "flex-start" }}
                  label={(
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: "block", fontWeight: 500, color: "var(--text-strong)" }}>{s.label}</span>
                      <span style={{ display: "block", color: "var(--text-muted)", fontSize: 11, marginTop: 2 }}>{s.hint}</span>
                    </span>
                  )}
                />
              </div>
            ))}
          </div>
        </div>
      </div>
    </Modal>
  );
}
