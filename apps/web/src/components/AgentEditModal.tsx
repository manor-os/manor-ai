import React, { useState, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import { useAuthStore } from "../stores/auth";
import Modal from "./ui/Modal";
import { AiBuildConversation } from "./ai/AiBuildConversation";
import LoadingSpinner from "./ui/LoadingSpinner";
import Button from "./ui/Button";
import Input from "./ui/Input";
import Select from "./ui/Select";
import Toggle from "./ui/Toggle";
import Chip from "./ui/Chip";
import StatusBadge from "./ui/StatusBadge";
import SharedAgentAvatar from "./ui/AgentAvatar";
import { t } from "../lib/i18n";
import { getSkillDescription } from "../pages/skills/skillTypes";
import { formatUserFacingLabel, formatUserFacingText } from "../lib/taskDisplay";
import type { Agent } from "../lib/types";
import {
  type AgentRuntimeProfile,
  AGENT_MODEL_INHERIT_VALUE,
  AgentModelMode,
  RUNTIME_PROFILE_OPTIONS,
  agentModelSelectOptions,
  fixedAgentModel,
  mergeAgentConfig,
  runtimeLearningEnabled,
  runtimeProfileFromConfig,
} from "../lib/agentRuntimeConfig";
import {
  parseTags,
  displayAgentCategory,
  displayAgentTag,
  displayToolName,
  displayToolDescription,
  parseMcpToolName,
  mcpToolActionLabel,
  mcpProviderLabel,
} from "../lib/agentDisplay";
import { PROMPT_VARIABLES, improvePrompt, runTest, insertVariable } from "../lib/agentPromptHelpers";
import { useAgentEditModalStore, closeAgentEditModal } from "../stores/agentEditModal";

// CATEGORIES is small and only used by the modal's category <Select> — kept
// as a local copy rather than importing from Agents.tsx (a page file), since
// Agents.tsx also uses its own copy for the marketplace category filter.
const CATEGORIES = [
  { value: "All", label: t("page.workspaces.filter_all") },
  { value: "Essential", label: t("page.agents.category_essential") },
  { value: "Growth", label: t("page.agents.category_growth") },
  { value: "Specialist", label: t("page.agents.category_specialist") },
  { value: "Property Management", label: t("page.agents.property_management") },
  { value: "Customer Service", label: t("page.agents.category_customer_service") },
];

function GeneratedAgentReviewCard({ agent }: { agent: Agent }) {
  const tags = parseTags(agent.tags).map(displayAgentTag).filter(Boolean);
  const description = formatUserFacingText(agent.description || "");
  const systemPrompt = String(agent.system_prompt || "").trim();
  return (
    <div style={{ display: "grid", gap: 13 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <SharedAgentAvatar
          name={agent.name || t("page.workspace_detail.agent")}
          seed={agent.id || agent.category || agent.description}
          size={46}
          shape="rounded"
        />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            <div
              style={{
                color: "#292524",
                fontSize: 16,
                fontWeight: 800,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={agent.name}
            >
              {agent.name || t("page.workspace_detail.agent")}
            </div>
            <span
              style={{
                borderRadius: 999,
                background: "rgba(67, 107, 101, 0.1)",
                color: "#436b65",
                fontSize: 11,
                fontWeight: 800,
                padding: "3px 7px",
                whiteSpace: "nowrap",
              }}
            >
              {t("page.agent_form.ai_preview_draft")}
            </span>
          </div>
          {description && (
            <p style={{ margin: "6px 0 0", color: "#57534e", fontSize: 13, lineHeight: 1.55 }}>
              {description}
            </p>
          )}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div style={{ borderRadius: 10, background: "#f7f6f3", padding: "9px 10px" }}>
          <div style={{ color: "#a8a29e", fontSize: 10, fontWeight: 800, textTransform: "uppercase" }}>
            {t("page.agent_form.ai_preview_category")}
          </div>
          <div style={{ marginTop: 3, color: "#44403c", fontSize: 12, fontWeight: 700 }}>
            {displayAgentCategory(agent.category)}
          </div>
        </div>
        <div style={{ borderRadius: 10, background: "#f7f6f3", padding: "9px 10px" }}>
          <div style={{ color: "#a8a29e", fontSize: 10, fontWeight: 800, textTransform: "uppercase" }}>
            {t("page.agent_form.ai_preview_tags")}
          </div>
          <div
            style={{
              marginTop: 4,
              display: "flex",
              gap: 5,
              flexWrap: "wrap",
              minHeight: 20,
            }}
          >
            {tags.length ? (
              tags.slice(0, 4).map((tag) => (
                <span
                  key={tag}
                  style={{
                    borderRadius: 999,
                    background: "#fff",
                    border: "1px solid #e7e5e4",
                    color: "#57534e",
                    fontSize: 11,
                    fontWeight: 700,
                    padding: "2px 6px",
                  }}
                >
                  {tag}
                </span>
              ))
            ) : (
              <span style={{ color: "#78716c", fontSize: 12 }}>
                {t("page.agent_form.ai_preview_no_tags")}
              </span>
            )}
          </div>
        </div>
      </div>

      {systemPrompt && (
        <div>
          <div style={{ color: "#a8a29e", fontSize: 10, fontWeight: 800, textTransform: "uppercase", marginBottom: 6 }}>
            {t("page.agent_form.ai_preview_prompt")}
          </div>
          <div
            style={{
              maxHeight: 138,
              overflow: "auto",
              borderRadius: 10,
              border: "1px solid #efede8",
              background: "#fffdfa",
              color: "#44403c",
              fontSize: 12,
              lineHeight: 1.55,
              padding: "9px 10px",
              whiteSpace: "pre-wrap",
            }}
          >
            {systemPrompt}
          </div>
        </div>
      )}
    </div>
  );
}

// Exported (not just modal-local) because Agents.tsx's separate hired-agent
// "Edit Prompt" modal also renders this panel — it isn't exclusive to the
// create/edit modal moved into this file.
export function TestPromptPanel({
  systemPrompt,
  message,
  onMessageChange,
  response,
  loading,
  onRun,
}: {
  systemPrompt: string;
  message: string;
  onMessageChange: (v: string) => void;
  response: string;
  loading: boolean;
  onRun: () => void;
}) {
  return (
    <div className="mt-4 rounded-lg border border-stone-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <p className="m-0 text-xs font-semibold text-stone-700">{t("page.agents.prompt_playground")}</p>
        <span className="text-xs text-stone-500">{t("page.agents.preview_only_not_saved_to_this_agent")}</span>
      </div>

      <p className="mt-2 mb-0 text-xs text-stone-500">
        {t("page.agents.test_how_this_prompt_responds_to_a_sample_user_m")}
      </p>

      <div className="mt-2">
        <Input
          value={message}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
            onMessageChange(e.target.value)
          }
          placeholder={t("page.agents.enter_a_sample_user_message_e_g_how_do_i_handle")}
        />
      </div>

      <div className="mt-2 flex items-center justify-between gap-3">
        <span className="text-xs text-stone-500">
          {!systemPrompt.trim()
            ? t("page.agents.add_a_system_prompt_first_then_run_a_playground_test")
            : t("page.agents.run_this_sample_message_to_preview_the_agent_response")}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={onRun}
          disabled={loading || !message.trim() || !systemPrompt.trim()}
        >
          {loading ? <LoadingSpinner size={12} /> : null}
          {loading ? t("page.agents.testing") : t("page.agents.run_playground_test")}
        </Button>
      </div>

      {response && (
        <div className="mt-3 rounded-md border border-stone-200 bg-stone-50 p-3">
          <p className="m-0 text-xs font-semibold text-stone-700">{t("page.agents.preview_response")}</p>
          <p className="mt-1 whitespace-pre-wrap text-[13px] leading-6 text-stone-700">
            {response}
          </p>
        </div>
      )}
    </div>
  );
}

export default function AgentEditModal() {
  const agentId = useAgentEditModalStore((s) => s.agentId);
  const showModal = agentId !== undefined;
  const isEditing = typeof agentId === "string";

  const queryClient = useQueryClient();
  const toast = useToastStore();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const navigate = useNavigate();

  const [agentMode, setAgentMode] = useState<"ai" | "manual">("ai");
  const [editingAgent, setEditingAgent] = useState<Record<
    string,
    unknown
  > | null>(null);
  // Create/Edit form state
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [formPrompt, setFormPrompt] = useState("");
  const [formAvatarUrl, setFormAvatarUrl] = useState("");
  const [formCategory, setFormCategory] = useState("");
  const [formTags, setFormTags] = useState("");
  const [formLearningEnabled, setFormLearningEnabled] = useState(true);
  const [formTestMsg, setFormTestMsg] = useState("");
  const [formTestResp, setFormTestResp] = useState("");
  const [formTestLoading, setFormTestLoading] = useState(false);
  const [improvingFormPrompt, setImprovingFormPrompt] = useState(false);
  const [formAvatarUploading, setFormAvatarUploading] = useState(false);
  const [formRuntimeProfile, setFormRuntimeProfile] =
    useState<AgentRuntimeProfile>("hosted");
  const [formModel, setFormModel] = useState("");
  const [formTemperature, setFormTemperature] = useState<number | null>(null);
  const [formMaxTokens, setFormMaxTokens] = useState("");

  // Skill attachment in create modal
  const [attachSkillIds, setAttachSkillIds] = useState<string[]>([]);
  const [editSkillIds, setEditSkillIds] = useState<string[]>([]);
  const [origSkillIds, setOrigSkillIds] = useState<string[]>([]);
  const [attachToolIds, setAttachToolIds] = useState<string[]>([]);
  const [editToolIds, setEditToolIds] = useState<string[]>([]);
  const [origToolIds, setOrigToolIds] = useState<string[]>([]);
  const [skillSearch, setSkillSearch] = useState("");
  const [toolSearch, setToolSearch] = useState("");
  const [mcpSearch, setMcpSearch] = useState("");
  const [capabilityTab, setCapabilityTab] = useState<"skills" | "tools" | "mcp">("skills");

  const avatarFileInputRef = useRef<HTMLInputElement>(null);
  const promptTextareaRef = useRef<HTMLTextAreaElement>(null);

  const { data: entitySkills } = useQuery({
    queryKey: ["skills", "list"],
    queryFn: () => api.skills.list({ include_platform: true }),
    enabled: privateApiEnabled && showModal,
  });

  const { data: toolCatalog } = useQuery({
    queryKey: ["agents", "tools", "all-for-create"],
    queryFn: () => api.agents.allToolsForCreate(),
    enabled: privateApiEnabled && showModal,
  });

  const { data: mcpServerStatus } = useQuery({
    queryKey: ["mcp-servers", "agent-settings"],
    queryFn: () => api.integrations.mcpServers(),
    enabled: privateApiEnabled && showModal,
  });

  const { data: modelCatalog } = useQuery({
    queryKey: ["model-catalog"],
    queryFn: () => api.auth.getModelCatalog(),
    enabled: privateApiEnabled && showModal,
    staleTime: 60_000,
  });

  const filteredSkills = ((entitySkills as any[]) || []).filter((skill: any) => {
    if (!skillSearch.trim()) return true;
    const q = skillSearch.toLowerCase();
    const description = getSkillDescription(skill);
    return (
      (skill.display_name || skill.name || "").toLowerCase().includes(q) ||
      description.toLowerCase().includes(q)
    );
  });

  const catalogTools = (toolCatalog as any[]) || [];
  const runtimeTools = catalogTools.filter((tool: any) => !parseMcpToolName(tool?.name));
  const filteredTools = runtimeTools.filter((tool: any) => {
    if (!toolSearch.trim()) return true;
    const q = toolSearch.toLowerCase();
    return (
      (tool.display_name || tool.name || "").toLowerCase().includes(q) ||
      (tool.description || "").toLowerCase().includes(q)
    );
  });

  const skillSelectedIds = editingAgent ? editSkillIds : attachSkillIds;
  const toolSelectedIds = editingAgent ? editToolIds : attachToolIds;
  const selectedToolIdSet = React.useMemo(() => new Set(toolSelectedIds), [toolSelectedIds]);
  const mcpToolsByServer = React.useMemo(() => {
    const byServer = new Map<string, any[]>();
    for (const tool of catalogTools) {
      const parsed = parseMcpToolName(tool?.name);
      if (!parsed) continue;
      const item = { ...tool, mcp_action_key: parsed.actionKey, mcp_server_key: parsed.serverKey };
      const bucket = byServer.get(parsed.serverKey) || [];
      bucket.push(item);
      byServer.set(parsed.serverKey, bucket);
    }
    for (const tools of byServer.values()) {
      tools.sort((a, b) => mcpToolActionLabel(a).localeCompare(mcpToolActionLabel(b)));
    }
    return byServer;
  }, [catalogTools]);
  const selectedSkillCount = skillSelectedIds.length;
  const selectedRuntimeToolCount = runtimeTools.filter((tool: any) =>
    selectedToolIdSet.has(tool.id),
  ).length;
  const mcpActionCount = Array.from(mcpToolsByServer.values()).reduce(
    (sum, tools) => sum + tools.length,
    0,
  );
  const selectedMcpActionCount = Array.from(mcpToolsByServer.values()).reduce(
    (sum, tools) =>
      sum + tools.filter((tool: any) => selectedToolIdSet.has(tool.id)).length,
    0,
  );
  const mcpServers = (mcpServerStatus as any[]) || [];
  const filteredMcpServers = mcpServers.filter((server: any) => {
    if (!mcpSearch.trim()) return true;
    const q = mcpSearch.toLowerCase();
    const tools = mcpToolsByServer.get(server.server_key) || [];
    return (
      String(server.name || "").toLowerCase().includes(q) ||
      String(server.server_key || "").toLowerCase().includes(q) ||
      String(server.hint || "").toLowerCase().includes(q) ||
      tools.some((tool: any) =>
        String(tool.display_name || tool.name || "").toLowerCase().includes(q) ||
        String(tool.description || "").toLowerCase().includes(q)
      )
    );
  });
  const updateSelectedToolIds = React.useCallback(
    (updater: (prev: string[]) => string[]) => {
      if (editingAgent) setEditToolIds(updater);
      else setAttachToolIds(updater);
    },
    [editingAgent],
  );
  const toggleToolId = React.useCallback(
    (toolId: string) => {
      updateSelectedToolIds((prev) =>
        prev.includes(toolId)
          ? prev.filter((id) => id !== toolId)
          : [...prev, toolId],
      );
    },
    [updateSelectedToolIds],
  );

  const resetForm = () => {
    setFormName("");
    setFormDesc("");
    setFormPrompt("");
    setFormAvatarUrl("");
    setFormCategory("");
    setFormLearningEnabled(true);
    setFormRuntimeProfile("hosted");
    setFormModel("");
    setFormTemperature(null);
    setFormMaxTokens("");
    setFormTestMsg("");
    setFormTestResp("");
    setAttachSkillIds([]);
    setEditSkillIds([]);
    setOrigSkillIds([]);
    setAttachToolIds([]);
    setEditToolIds([]);
    setOrigToolIds([]);
    setSkillSearch("");
    setToolSearch("");
    setMcpSearch("");
    setCapabilityTab("skills");
  };

  const closeModal = () => {
    resetForm();
    setEditingAgent(null);
    closeAgentEditModal();
  };

  // Fetch the full agent record + its skill/tool bindings when opened in
  // edit mode. Replaces the old openEdit(agent) which took the full object —
  // callers of openAgentEditModal only pass an id, so this component fetches.
  useEffect(() => {
    if (!showModal) return;
    setAgentMode("manual");
    if (!isEditing) {
      resetForm();
      setEditingAgent(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const agent = await api.agents.get(agentId as string);
        if (cancelled) return;
        setEditingAgent(agent as unknown as Record<string, unknown>);
        setFormName(agent.name || "");
        setFormDesc(formatUserFacingText(agent.description || ""));
        setFormPrompt(agent.system_prompt || "");
        setFormAvatarUrl(agent.avatar_url || "");
        setFormCategory(agent.category || "");
        setFormLearningEnabled(runtimeLearningEnabled(agent.config));
        setFormRuntimeProfile(runtimeProfileFromConfig(agent.config));
        const agentConfig = (agent.config || {}) as Record<string, any>;
        setFormModel(fixedAgentModel(agentConfig));
        setFormTemperature(
          typeof agentConfig.temperature === "number" ? agentConfig.temperature : null,
        );
        setFormMaxTokens(
          typeof agentConfig.max_tokens === "number" ? String(agentConfig.max_tokens) : "",
        );
        const tags = Array.isArray(agent.tags)
          ? agent.tags.map(displayAgentTag).filter(Boolean).join(", ")
          : (agent.tags as unknown as string) || "";
        setFormTags(formatUserFacingText(tags));
        setFormTestMsg("");
        setFormTestResp("");
        const [boundSkills, availableSkills, boundTools] = await Promise.all([
          api.skills.listAgentBindings(agent.id),
          api.skills.listAgentAvailable(agent.id),
          api.agents.getTools(agent.id),
        ]);
        if (cancelled) return;
        const boundSkillIds = (boundSkills || []).map((s: any) => s.id);
        setOrigSkillIds(boundSkillIds);
        setEditSkillIds([...boundSkillIds]);
        const boundToolIds = (boundTools || []).map((t: any) => t.id);
        setOrigToolIds(boundToolIds);
        setEditToolIds([...boundToolIds]);
      } catch {
        if (cancelled) return;
        setOrigSkillIds([]);
        setEditSkillIds([]);
        setOrigToolIds([]);
        setEditToolIds([]);
        toast.error(t("page.agents.failed_to_load_agent"));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, showModal, isEditing]);

  const createMutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.agents.create(data),
    onSuccess: async (agent: any, variables: Record<string, unknown>) => {
      let hasBindingFailures = false;
      if (agent?.id) {
        const bindingTasks: Promise<unknown>[] = [];
        const uniqueToolIds = Array.from(new Set(attachToolIds));
        const uniqueSkillIds = Array.from(new Set(attachSkillIds));
        if (uniqueToolIds.length > 0) {
          bindingTasks.push(api.agents.bindTools(agent.id, uniqueToolIds));
        }
        for (const skillId of uniqueSkillIds) {
          bindingTasks.push(api.skills.bindSkill(agent.id, skillId));
        }
        if (bindingTasks.length > 0) {
          const results = await Promise.allSettled(bindingTasks);
          hasBindingFailures = results.some((r) => r.status === "rejected");
        }
      }
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      closeModal();
        if (hasBindingFailures) {
          toast.error(t("page.agents.agent_created_but_some_tool_skill_bindings_faile"));
        } else {
          toast.success(t("page.agents.agent_created"));
        }
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      api.agents.update(id, data),
    onSuccess: async () => {
      const agentId2 = editingAgent?.id as string;
      if (agentId2) {
        const toAttachSkills = editSkillIds.filter(
          (id: string) => !origSkillIds.includes(id),
        );
        const toDetachSkills = origSkillIds.filter(
          (id: string) => !editSkillIds.includes(id),
        );
        const toAttachTools = editToolIds.filter(
          (id: string) => !origToolIds.includes(id),
        );
        const toDetachTools = origToolIds.filter(
          (id: string) => !editToolIds.includes(id),
        );

        for (const skillId of toAttachSkills) {
          try {
            await api.skills.bindSkill(agentId2, skillId);
          } catch {
            /* non-fatal */
          }
        }
        for (const skillId of toDetachSkills) {
          try {
            await api.skills.unbindSkill(agentId2, skillId);
          } catch {
            /* non-fatal */
          }
        }
        if (toAttachTools.length > 0) {
          try {
            await api.agents.bindTools(agentId2, toAttachTools);
          } catch {
            /* non-fatal */
          }
        }
        if (toDetachTools.length > 0) {
          try {
            await api.agents.unbindTools(agentId2, toDetachTools);
          } catch {
            /* non-fatal */
          }
        }
      }
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      closeModal();
      toast.success(t("page.agents.agent_updated"));
    },
  });

  const handleAvatarUpload = async (file: File) => {
    if (!file.type.startsWith("image/")) {
      toast.error(t("page.agents.please_upload_an_image_file"));
      return;
    }
    setFormAvatarUploading(true);
    try {
      const res = await api.auth.uploadAvatar(file);
      if (res?.avatar_url) {
        setFormAvatarUrl(res.avatar_url);
        toast.success(t("page.agents.avatar_uploaded"));
      } else {
        toast.error(t("page.agents.avatar_upload_failed"));
      }
    } catch {
      toast.error(t("page.agents.avatar_upload_failed"));
    } finally {
      setFormAvatarUploading(false);
    }
  };

  const handleSubmit = () => {
    if (!formName.trim()) return;
    const tags = formTags
      .split(",")
      .map((t: string) => t.trim())
      .filter(Boolean);
    const payload = {
      name: formName,
      description: formDesc,
      system_prompt: formPrompt,
      avatar_url: formAvatarUrl,
      category: formCategory,
      tags,
      config: (() => {
        const config = mergeAgentConfig(
          editingAgent?.config,
          formLearningEnabled,
          formRuntimeProfile,
        );
        const model = formModel.trim();
        if (model) {
          config.model_mode = AgentModelMode.Fixed;
          config.model = model;
        } else {
          config.model_mode = AgentModelMode.Inherit;
          delete config.model;
        }
        if (formTemperature !== null) config.temperature = formTemperature;
        else delete config.temperature;
        const maxTokens = parseInt(formMaxTokens, 10);
        if (Number.isFinite(maxTokens) && maxTokens > 0) config.max_tokens = maxTokens;
        else delete config.max_tokens;
        return config;
      })(),
    };
    if (editingAgent) {
      updateMutation.mutate({ id: editingAgent.id as string, data: payload });
    } else {
      createMutation.mutate(payload);
    }
  };

  const formAvatarSeed = editingAgent
    ? String(editingAgent.id || formCategory || formName)
    : `${formName}::${formDesc}::${formCategory}`;

  if (!showModal) return null;

  return (
      <Modal
        open={showModal}
        onClose={closeModal}
        title={editingAgent ? t("page.agents.edit_agent") : t("page.agents.create_agent")}
        maxWidth={agentMode === "manual" ? "42rem" : "36rem"}
        footer={
          agentMode === "ai" ? undefined : (
            <>
              <Button
                variant="outline"
                onClick={closeModal}
              >
                {t("action.cancel")}
              </Button>
              <Button
                variant="primary"
                onClick={handleSubmit}
                disabled={
                  !formName.trim() ||
                  createMutation.isPending ||
                  updateMutation.isPending
                }
              >
                {createMutation.isPending || updateMutation.isPending
                  ? t("page.agents.saving")
                  : editingAgent
                    ? t("page.agents.update_agent")
                    : t("page.agents.create_agent")}
              </Button>
            </>
          )
        }
      >
        {/* Tabs: ✨ AI vs Manual */}
        <div style={{ display: "flex", gap: 6, marginBottom: 14 }}>
          {(["ai", "manual"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setAgentMode(m)}
              style={{
                flex: 1,
                padding: "8px 10px",
                borderRadius: 8,
                border: `1px solid ${agentMode === m ? "var(--modal-border-strong)" : "var(--modal-border)"}`,
                background: agentMode === m ? "var(--modal-muted-bg)" : "transparent",
                color: agentMode === m ? "var(--text-strong)" : "var(--text-muted)",
                fontSize: 13,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              {m === "ai"
                ? (editingAgent ? t("page.agent_form.ai_edit_mode") : t("page.agent_form.ai_mode"))
                : t("page.skill_form.manual_mode")}
            </button>
          ))}
        </div>

        {agentMode === "ai" ? (
          editingAgent ? (
            <AiBuildConversation
              intro={t("page.agent_form.ai_edit_intro")}
              describePlaceholder={t("page.agent_form.ai_edit_describe_placeholder")}
              answersPlaceholder={t("page.skill_form.ai_answers_placeholder")}
              buildingHint={t("page.skill_form.ai_edit_building_hint")}
              draftQuestions={async () => ({ questions: [] as string[], ready: true })}
              generate={async (p) => {
                try {
                  await api.agents.aiUpdate(editingAgent.id as string, p);
                } catch (e: any) {
                  toast.error(e?.message || t("page.agent_form.ai_failed"));
                  throw e;
                }
                queryClient.invalidateQueries({ queryKey: ["agents"] });
                toast.success(t("page.agent_form.ai_updated"));
                closeModal();
              }}
            />
          ) : (
            <AiBuildConversation
              intro={t("page.agent_form.ai_intro")}
              describePlaceholder={t("page.agent_form.ai_describe_placeholder")}
              answersPlaceholder={t("page.skill_form.ai_answers_placeholder")}
              buildingHint={t("page.skill_form.ai_building_hint")}
              draftQuestions={(p) => api.agents.draftQuestions(p)}
              generate={async (p, onStep) => {
                let draft: Agent;
                try {
                  draft = await api.agents.generateDraftStream(p, onStep);
                } catch (e: any) {
                  toast.error(e?.message || t("page.agent_form.ai_failed"));
                  throw e;
                }
                return {
                  title: t("page.agent_form.ai_review_title"),
                  content: <GeneratedAgentReviewCard agent={draft} />,
                  confirmLabel: t("page.agent_form.ai_confirm_create"),
                  reviseLabel: t("page.agent_form.ai_revise"),
                  onConfirm: async () => {
                    try {
                      await createMutation.mutateAsync({
                        name: draft.name || "New Agent",
                        description: draft.description || "",
                        system_prompt: draft.system_prompt || "",
                        avatar_url: "",
                        category: draft.category || "",
                        tags: draft.tags || [],
                        source: "llm-generated",
                        config: mergeAgentConfig({}, true, "hosted"),
                      });
                    } catch (e: any) {
                      toast.error(e?.message || t("page.agent_form.ai_failed"));
                      throw e;
                    }
                  },
                };
              }}
            />
          )
        ) : (
        <div className="space-y-3">
          <div className="rounded-xl border border-stone-200 bg-white px-3.5 py-3">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3">
                <SharedAgentAvatar
                  name={formName || t("page.skills.agent")}
                  avatarUrl={formAvatarUrl}
                  seed={formAvatarSeed}
                  size={40}
                  shape="rounded"
                />
                <div className="min-w-0">
                  <div className="text-[13px] font-semibold text-stone-900">
                    {formName || t("page.agents.agent_name")}
                  </div>
                  <div className="truncate text-xs text-stone-500">
                    {formDesc || t("page.agents.what_does_this_agent_do")}
                  </div>
                </div>
              </div>
              <input
                ref={avatarFileInputRef}
                type="file"
                accept="image/*"
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
                  const file = e.target.files?.[0];
                  if (file) {
                    void handleAvatarUpload(file);
                  }
                  e.currentTarget.value = "";
                }}
                className="hidden"
              />
              <div className="flex shrink-0 items-center gap-1.5">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={formAvatarUploading}
                  onClick={() => avatarFileInputRef.current?.click()}
                >
                  {formAvatarUploading ? t("page.agents.uploading") : t("page.agents.update_avatar")}
                </Button>
                {formAvatarUrl ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setFormAvatarUrl("")}
                  >
                    {t("page.task_detail.runtime.remove_rule")}
                  </Button>
                ) : null}
              </div>
            </div>
            <div className="grid grid-cols-1 gap-3">
              <Input
                label={t("page.agents.name")}
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                placeholder={t("page.agents.agent_name")}
              />
              <Input
                label={t("page.task_collections.description")}
                value={formDesc}
                onChange={(e) => setFormDesc(e.target.value)}
                placeholder={t("page.agents.what_does_this_agent_do")}
              />
            </div>
          </div>

          <div>
            <label className="manor-label">{t("page.workspaces.category")}</label>
            <Select
              value={formCategory}
              onChange={setFormCategory}
              placeholder={t("page.agents.select_a_category")}
              options={[
                ...(
                  formCategory && !CATEGORIES.some((cat) => cat.value === formCategory)
                    ? [{ value: formCategory, label: displayAgentCategory(formCategory) }]
                    : []
                ),
                ...CATEGORIES.filter((c) => c.value !== "All").map((cat) => ({
                  value: cat.value,
                  label: cat.label,
                })),
              ]}
            />
          </div>

          <Input
            label={t("page.blueprint_detail.tags_csv")}
            value={formTags}
            onChange={(e) => setFormTags(e.target.value)}
            placeholder={t("page.agents.e_g_booking_faq_leasing")}
          />
          <div className="rounded-xl border border-stone-200 bg-stone-50/70 px-3.5 py-3">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="text-[13px] font-semibold text-stone-900">
                  {t("page.agents.runtime_learning")}
                </div>
                <p className="mt-1 text-xs leading-5 text-stone-500">
                  {t("page.agents.runtime_learning_desc")}
                </p>
              </div>
              <Toggle
                checked={formLearningEnabled}
                onChange={() => setFormLearningEnabled((enabled) => !enabled)}
                disabled={createMutation.isPending || updateMutation.isPending}
                aria-label={t("page.agents.runtime_learning")}
              />
            </div>
          </div>
          <div className="rounded-xl border border-stone-200 bg-white px-3.5 py-3">
            <div className="mb-3 text-[13px] font-semibold text-stone-900">
              {t("page.flows.configuration")}
            </div>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <div>
                <label className="manor-label">
                  {t("page.agent_detail.model_override")}
                </label>
                <Select
                  value={formModel || AGENT_MODEL_INHERIT_VALUE}
                  onChange={(selection) =>
                    setFormModel(selection === AGENT_MODEL_INHERIT_VALUE ? "" : selection)
                  }
                  options={agentModelSelectOptions(
                    modelCatalog?.catalog,
                    formModel,
                    t("page.api_keys.default"),
                  )}
                  placeholder={t("page.api_keys.default")}
                  filterable
                />
              </div>
              <div>
                <label className="manor-label">
                  {t("page.agent_detail.temperature")} {formTemperature ?? 0.7}
                </label>
                <input
                  type="range"
                  min="0"
                  max="2"
                  step="0.1"
                  value={formTemperature ?? 0.7}
                  onChange={(e) => setFormTemperature(parseFloat(e.target.value))}
                  className="mt-3 w-full"
                  style={{ accentColor: "#436b65" }}
                />
              </div>
              <Input
                label={t("page.agent_detail.max_tokens")}
                type="number"
                value={formMaxTokens}
                onChange={(e) => setFormMaxTokens(e.target.value)}
                placeholder="4096"
              />
            </div>
          </div>
          <div className="rounded-xl border border-stone-200 bg-white px-3.5 py-3">
            <div className="mb-3 flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="text-[13px] font-semibold text-stone-900">
                  Run method
                </div>
                <p className="mt-1 text-xs leading-5 text-stone-500">
                  This describes where the agent runs. HTTPS endpoints are
                  configured from Workspace &gt; Agents.
                </p>
              </div>
              <span className="shrink-0 rounded-md bg-stone-100 px-2.5 py-1 text-[11px] font-semibold text-stone-600">
                {RUNTIME_PROFILE_OPTIONS.find(
                  (option) => option.key === formRuntimeProfile,
                )?.badge || "Run method"}
              </span>
            </div>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {RUNTIME_PROFILE_OPTIONS.map((option) => {
                const selected = option.key === formRuntimeProfile;
                return (
                  <button
                    key={option.key}
                    type="button"
                    onClick={() => setFormRuntimeProfile(option.key)}
                    className={`rounded-xl border p-3 text-left transition-colors ${
                      selected
                        ? "border-manor-300 bg-manor-50"
                        : "border-stone-200 bg-white hover:bg-stone-50"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className={`text-[13px] font-bold ${
                          selected ? "text-manor-800" : "text-stone-800"
                        }`}
                      >
                        {option.title}
                      </span>
                      <span
                        className={`rounded-md px-2 py-0.5 text-[10px] font-semibold ${
                          selected
                            ? "bg-white text-manor-700"
                            : "bg-stone-100 text-stone-500"
                        }`}
                      >
                        {option.badge}
                      </span>
                    </div>
                    <p className="mt-1.5 text-xs leading-5 text-stone-500">
                      {option.body}
                    </p>
                  </button>
                );
              })}
            </div>
          </div>
          <div className="rounded-xl border border-stone-200 bg-white px-3.5 py-3">
            <div className="mb-2 flex items-center justify-between gap-3">
              <label className="manor-label mb-0">{t("page.skill_form.system_prompt")}</label>
              <button
                type="button"
                onClick={() =>
                  improvePrompt(
                    formPrompt,
                    formName,
                    formDesc,
                    setFormPrompt,
                    setImprovingFormPrompt,
                  )
                }
                disabled={improvingFormPrompt || !formPrompt.trim()}
                className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-semibold text-stone-600 hover:bg-stone-100 disabled:text-stone-400"
              >
                {improvingFormPrompt ? <LoadingSpinner size={10} /> : "✦"}
                {improvingFormPrompt ? t("page.agents.improving") : t("page.agents.improve_with_ai")}
              </button>
            </div>
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[10px] font-bold uppercase tracking-wide text-stone-400">
                {t("page.agents.insert")}
              </span>
              {PROMPT_VARIABLES.map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() =>
                    insertVariable(
                      v,
                      promptTextareaRef,
                      formPrompt,
                      setFormPrompt,
                    )
                  }
                  className="rounded-md border border-stone-200 bg-stone-50 px-2 py-0.5 text-[11px] font-medium text-stone-700 hover:bg-white"
                  style={{ fontFamily: "monospace" }}
                >
                  {v}
                </button>
              ))}
            </div>
            <textarea
              ref={promptTextareaRef}
              value={formPrompt}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                setFormPrompt(e.target.value)
              }
              rows={5}
              placeholder={t("page.agents.you_are_a_helpful_assistant")}
              className="manor-input"
              style={{
                fontFamily: "monospace",
                fontSize: 13,
                lineHeight: 1.5,
                resize: "vertical",
                minHeight: 112,
                padding: "10px 12px",
              }}
            />
          </div>

          <div className="rounded-xl border border-stone-200 bg-white px-3.5 py-3">
            <div className="mb-3 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-[13px] font-semibold text-stone-900">
                  Capabilities
                </div>
                <p className="mt-1 text-xs leading-5 text-stone-500">
                  Choose the reusable skills and runtime actions this agent can use.
                </p>
              </div>
              <span className="shrink-0 rounded-md bg-stone-100 px-2.5 py-1 text-[11px] font-semibold text-stone-600">
                {selectedSkillCount + selectedRuntimeToolCount + selectedMcpActionCount} selected
              </span>
            </div>
            <div className="mb-3 grid grid-cols-3 rounded-lg bg-stone-100 p-1">
              {[
                {
                  key: "skills" as const,
                  label: "Skills",
                  count: selectedSkillCount,
                },
                {
                  key: "tools" as const,
                  label: "Tools",
                  count: selectedRuntimeToolCount,
                },
                {
                  key: "mcp" as const,
                  label: "MCP",
                  count: selectedMcpActionCount,
                },
              ].map((item) => (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setCapabilityTab(item.key)}
                  className={`flex items-center justify-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-semibold transition-colors ${
                    capabilityTab === item.key
                      ? "bg-white text-stone-900 shadow-sm"
                      : "text-stone-500 hover:text-stone-700"
                  }`}
                >
                  <span>{item.label}</span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] ${
                      capabilityTab === item.key
                        ? "bg-stone-100 text-stone-500"
                        : "bg-white/70 text-stone-400"
                    }`}
                  >
                    {item.count}
                  </span>
                </button>
              ))}
            </div>

            {capabilityTab === "skills" && (
              <div>
                <Input
                  value={skillSearch}
                  onChange={(e) => setSkillSearch(e.target.value)}
                  placeholder={t("page.agents.search_skills_by_name_or_description")}
                />
                <div className="mt-2 flex items-center justify-between text-xs text-stone-500">
                  <span>
                    {filteredSkills.length} skill{filteredSkills.length === 1 ? "" : "s"}
                  </span>
                  <span>{selectedSkillCount} selected</span>
                </div>
                <div className="mt-2 max-h-64 overflow-y-auto rounded-xl border border-stone-200 bg-white">
                  {filteredSkills.length === 0 ? (
                    <div className="p-3 text-xs text-stone-400">{t("page.agents.no_skills_available")}</div>
                  ) : (
                    filteredSkills.map((skill: any) => {
                      const sel = skillSelectedIds.includes(skill.id);
                      const description = getSkillDescription(skill);
                      return (
                        <button
                          key={skill.id}
                          type="button"
                          onClick={() => {
                            if (editingAgent) {
                              setEditSkillIds((prev: string[]) =>
                                sel
                                  ? prev.filter((id: string) => id !== skill.id)
                                  : [...prev, skill.id],
                              );
                            } else {
                              setAttachSkillIds((prev: string[]) =>
                                sel
                                  ? prev.filter((id: string) => id !== skill.id)
                                  : [...prev, skill.id],
                              );
                            }
                          }}
                          className={`flex w-full items-start gap-2.5 border-0 border-b border-stone-100 px-3 py-2.5 text-left transition-colors ${sel ? "bg-stone-50" : "bg-white hover:bg-stone-50"}`}
                        >
                          <input type="checkbox" checked={sel} readOnly className="mt-0.5" />
                          <div className="min-w-0">
                            <div className="truncate text-[13px] font-semibold text-stone-900">
                              {formatUserFacingLabel(skill.display_name || skill.name || "")}
                            </div>
                            <p
                              className="mt-0.5 text-xs leading-5 text-stone-500"
                              style={{
                                display: "-webkit-box",
                                WebkitLineClamp: 2,
                                WebkitBoxOrient: "vertical",
                                overflow: "hidden",
                              }}
                            >
                              {formatUserFacingText(description || t("page.workspaces.no_description"))}
                            </p>
                          </div>
                        </button>
                      );
                    })
                  )}
                </div>
              </div>
            )}

            {capabilityTab === "tools" && (
              <div>
              <Input
                value={toolSearch}
                onChange={(e) => setToolSearch(e.target.value)}
                placeholder="Search runtime tools"
              />
              <div className="mt-2 flex items-center justify-between text-xs text-stone-500">
                <span>
                  {filteredTools.length} runtime {t("page.chat_history.tool")}{filteredTools.length !== 1 ? "s" : ""}
                </span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      const allFiltered = Array.from(
                        new Set(filteredTools.map((tool: any) => tool.id)),
                      );
                      updateSelectedToolIds((prev) =>
                        Array.from(new Set([...prev, ...allFiltered])),
                      );
                    }}
                    className="text-stone-600 hover:text-stone-800 font-medium"
                  >
                    {t("page.agents.select_all")}
                  </button>
                  <span className="text-stone-300">|</span>
                  <button
                    type="button"
                    onClick={() => {
                      const runtimeIds = new Set(runtimeTools.map((tool: any) => tool.id));
                      updateSelectedToolIds((prev) => prev.filter((id) => !runtimeIds.has(id)));
                    }}
                    className="text-stone-600 hover:text-stone-800 font-medium"
                  >
                    {t("page.agents.clear_2")}
                  </button>
                </div>
              </div>
              <div className="mt-2 max-h-64 overflow-y-auto rounded-xl border border-stone-200 bg-white divide-y divide-stone-100">
                {filteredTools.length === 0 ? (
                  <div className="p-3 text-xs text-stone-400">{t("page.agents.no_tools_available")}</div>
                ) : (
                  filteredTools.map((tool: any) => {
                    const sel = selectedToolIdSet.has(tool.id);
                    const inactive = (tool.status || "active") !== "active";
                    return (
                      <button
                        key={tool.id}
                        type="button"
                        onClick={() => toggleToolId(tool.id)}
                        className={`w-full text-left flex items-start gap-3 px-3 py-3 transition-colors ${sel ? "bg-stone-50" : "bg-white hover:bg-stone-50"}`}
                      >
                        <input type="checkbox" checked={sel} readOnly className="mt-1" />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center justify-between gap-2">
                            <div className="min-w-0 truncate text-[13px] font-semibold text-stone-900">
                              {displayToolName(tool)}
                            </div>
                            <div className="flex items-center gap-1.5">
                              {tool.category ? (
                                <span className="text-[10px] uppercase tracking-wide text-stone-500 bg-stone-100 px-2 py-0.5 rounded">
                                  {displayAgentCategory(tool.category)}
                                </span>
                              ) : null}
                              {inactive ? (
                                <span className="text-[10px] uppercase tracking-wide text-stone-500 bg-stone-100 px-2 py-0.5 rounded">
                                  {t("page.agents.inactive")}
                                </span>
                              ) : null}
                            </div>
                          </div>
                          <p
                            className="mt-1 text-xs leading-5 text-stone-500"
                            style={{
                              display: "-webkit-box",
                              WebkitLineClamp: 2,
                              WebkitBoxOrient: "vertical",
                              overflow: "hidden",
                            }}
                          >
                            {displayToolDescription(tool) || t("page.workspaces.no_description")}
                          </p>
                        </div>
                      </button>
                    );
                  })
                )}
              </div>
              </div>
            )}

            {capabilityTab === "mcp" && (
              <div>
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[13px] font-semibold text-stone-900">MCP integrations</div>
                    <p className="mt-1 text-xs leading-5 text-stone-500">
                      Bind external actions and verify readiness. {selectedMcpActionCount}/{mcpActionCount} actions selected.
                    </p>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
                    Open Integrations
                  </Button>
                </div>
                <Input
                  value={mcpSearch}
                  onChange={(e) => setMcpSearch(e.target.value)}
                  placeholder="Search MCP integrations or actions"
                  className="mt-3"
                />
                <div className="mt-2 max-h-[24rem] overflow-y-auto rounded-xl border border-stone-200 bg-white divide-y divide-stone-100">
                  {!mcpServerStatus ? (
                    <div className="p-3 text-xs text-stone-400">Loading MCP integrations...</div>
                  ) : filteredMcpServers.length === 0 ? (
                    <div className="p-3 text-xs text-stone-400">No MCP integrations match this search.</div>
                  ) : (
                    filteredMcpServers.map((server: any) => {
                      const tools = mcpToolsByServer.get(server.server_key) || [];
                      const selectedCount = tools.filter((tool: any) => selectedToolIdSet.has(tool.id)).length;
                      const ready = Boolean(server.agent_can_use);
                      const comingSoon = Boolean(server.coming_soon);
                      const connectionCount =
                        (Array.isArray(server.connections) ? server.connections.length : 0) +
                        (Array.isArray(server.entity_accounts) ? server.entity_accounts.length : 0);
                      const allSelected = tools.length > 0 && selectedCount === tools.length;
                      return (
                        <div key={server.server_key} className="p-3">
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="flex flex-wrap items-center gap-2">
                                <div className="text-[13px] font-bold text-stone-900">
                                  {mcpProviderLabel(server)}
                                </div>
                                <StatusBadge
                                  type={ready ? "active" : comingSoon ? "gray" : "warning"}
                                  dot={!comingSoon}
                                >
                                  {ready ? "Ready" : comingSoon ? "Soon" : "Needs setup"}
                                </StatusBadge>
                                <Chip variant="slate" size="sm">
                                  {selectedCount}/{tools.length} actions
                                </Chip>
                                {connectionCount > 0 && (
                                  <Chip variant="green" size="sm">
                                    {connectionCount} connection{connectionCount === 1 ? "" : "s"}
                                  </Chip>
                                )}
                              </div>
                              <p
                                className="mt-1 text-xs leading-5 text-stone-500"
                                style={{
                                  display: "-webkit-box",
                                  WebkitLineClamp: 2,
                                  WebkitBoxOrient: "vertical",
                                  overflow: "hidden",
                                }}
                              >
                                {formatUserFacingText(server.hint || server.description || "Connect this integration before the agent executes these actions.")}
                              </p>
                            </div>
                            <Button
                              variant={ready ? "outline" : "primary"}
                              size="sm"
                              onClick={() => navigate("/integrations")}
                            >
                              {ready ? "Manage" : "Complete setup"}
                            </Button>
                          </div>

                          {tools.length === 0 ? (
                            <div className="mt-3 rounded-md border border-dashed border-stone-200 bg-stone-50 px-3 py-2 text-xs text-stone-500">
                              No MCP actions are discovered for this integration yet.
                            </div>
                          ) : (
                            <>
                              <div className="mt-3 flex items-center justify-between text-xs text-stone-500">
                                <span>{tools.length} available action{tools.length === 1 ? "" : "s"}</span>
                                <button
                                  type="button"
                                  className="font-medium text-stone-600 hover:text-stone-800"
                                  onClick={() => {
                                    const ids = tools.map((tool: any) => tool.id);
                                    const idSet = new Set(ids);
                                    updateSelectedToolIds((prev) =>
                                      allSelected
                                        ? prev.filter((id) => !idSet.has(id))
                                        : Array.from(new Set([...prev, ...ids])),
                                    );
                                  }}
                                >
                                  {allSelected ? "Clear integration" : "Select all actions"}
                                </button>
                              </div>
                              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                                {tools.map((tool: any) => {
                                  const sel = selectedToolIdSet.has(tool.id);
                                  return (
                                    <button
                                      key={tool.id}
                                      type="button"
                                      onClick={() => toggleToolId(tool.id)}
                                      className={`flex min-h-[72px] items-start gap-2 rounded-lg border px-3 py-2 text-left transition-colors ${
                                        sel
                                          ? "border-manor-200 bg-manor-50"
                                          : "border-stone-200 bg-white hover:bg-stone-50"
                                      }`}
                                    >
                                      <input type="checkbox" checked={sel} readOnly className="mt-1" />
                                      <div className="min-w-0">
                                        <div className="truncate text-[13px] font-semibold text-stone-900">
                                          {mcpToolActionLabel(tool)}
                                        </div>
                                        <p
                                          className="mt-0.5 text-xs leading-5 text-stone-500"
                                          style={{
                                            display: "-webkit-box",
                                            WebkitLineClamp: 2,
                                            WebkitBoxOrient: "vertical",
                                            overflow: "hidden",
                                          }}
                                        >
                                          {displayToolDescription(tool) || tool.name}
                                        </p>
                                      </div>
                                    </button>
                                  );
                                })}
                              </div>
                            </>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Test Prompt Panel */}
          <TestPromptPanel
            systemPrompt={formPrompt}
            message={formTestMsg}
            onMessageChange={setFormTestMsg}
            response={formTestResp}
            loading={formTestLoading}
            onRun={() =>
              runTest(
                formPrompt,
                formTestMsg,
                setFormTestResp,
                setFormTestLoading,
              )
            }
          />
        </div>
        )}
      </Modal>
  );
}
