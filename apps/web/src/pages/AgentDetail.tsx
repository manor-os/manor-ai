import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { AgentDeploymentResponse } from "../lib/api";
import type { Agent } from "../lib/types";
import { relativeTime, formatDateFull } from "../lib/format";
import TabSwitcher from "../components/ui/TabSwitcher";
import AgentAvatar from "../components/ui/AgentAvatar";
import StatusBadge from "../components/ui/StatusBadge";
import SearchInput from "../components/ui/SearchInput";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Toggle from "../components/ui/Toggle";
import { IconWarning, IconChevronLeft, IconPlus, IconDownload } from "../components/icons";

import { t } from "../lib/i18n";
import { getAgentDescription } from "../lib/localizedContent";
import { getSkillDescription } from "./skills/skillTypes";
/* -- helpers ------------------------------------------------ */

const FALLBACK_COLORS = [
  { bg: "#e5eeeb", fg: "#436b65" },
  { bg: "#e3e9f1", fg: "#3f57a0" },
  { bg: "#f3e5ed", fg: "#be185d" },
  { bg: "#ece9f5", fg: "#6443a0" },
  { bg: "#f3ecd6", fg: "#936027" },
  { bg: "#dceae3", fg: "#3f7361" },
  { bg: "#e8eff4", fg: "#426c87" },
  { bg: "#f1dddb", fg: "#a23e38" },
];

function getFallbackColor(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return FALLBACK_COLORS[Math.abs(hash) % FALLBACK_COLORS.length];
}

const CATEGORIES = [
  { value: "Customer Support", label: t("page.agent_detail.category_customer_support") },
  { value: "Development", label: t("page.agent_detail.category_development") },
  { value: "Marketing", label: t("page.agent_detail.category_marketing") },
  { value: "Sales", label: t("page.agent_detail.category_sales") },
  { value: "Analytics", label: t("page.agent_detail.category_analytics") },
  { value: "Operations", label: t("page.agent_detail.category_operations") },
  { value: "HR", label: t("page.agent_detail.category_hr") },
];

type Tab = "overview" | "tools" | "skills" | "executions" | "settings";

function objectConfig(value: unknown): Record<string, any> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, any>;
}

function runtimeLearningEnabled(config: unknown): boolean {
  const runtimeLearning = objectConfig(objectConfig(config).runtime_learning);
  return runtimeLearning.enabled !== false;
}

function mergeRuntimeLearningConfig(config: unknown, enabled: boolean): Record<string, any> {
  const next = { ...objectConfig(config) };
  next.runtime_learning = { ...objectConfig(next.runtime_learning), enabled };
  return next;
}

function agentConnectionInfo(agent: Pick<Agent, "config" | "source" | "is_template">): {
  label: string;
  detail: string;
  type: "success" | "info" | "warning" | "inactive";
  endpoint?: string;
} {
  const connection = objectConfig(objectConfig(agent.config).runtime_connection);
  const source = String(connection.source || "").toLowerCase();
  const endpoint = String(connection.endpoint_url || "").trim();
  if (source === "https") {
    return {
      label: "HTTPS",
      detail: "Runs through a generic HTTPS agent endpoint when added to a workspace.",
      type: "info",
      endpoint,
    };
  }
  if (agent.is_template) {
    return {
      label: "Template",
      detail: "A reusable agent template that can be added to workspaces.",
      type: "success",
    };
  }
  return {
    label: "Manor Hosted",
    detail: "Runs on Manor Hosted by default. CLI or HTTPS connections are chosen when adding it to a workspace.",
    type: "success",
  };
}

/* -- component --------------------------------------------- */

export default function AgentDetail() {
  const { agentId } = useParams<{ agentId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [tab, setTab] = useState<Tab>("overview");

  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [promptDraft, setPromptDraft] = useState("");
  const [editingConfig, setEditingConfig] = useState(false);
  const [configDraft, setConfigDraft] = useState<Record<string, any> & {
    model?: string;
    temperature?: number;
    max_tokens?: number;
  }>({});
  const [editingMeta, setEditingMeta] = useState(false);
  const [categoryDraft, setCategoryDraft] = useState("");
  const [tagsDraft, setTagsDraft] = useState("");

  const [showBindModal, setShowBindModal] = useState(false);
  const [toolSearch, setToolSearch] = useState("");
  const [selectedToolIds, setSelectedToolIds] = useState<string[]>([]);
  const [showBindSkillModal, setShowBindSkillModal] = useState(false);
  const [skillSearch, setSkillSearch] = useState("");
  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>([]);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [expandedExecId, setExpandedExecId] = useState<string | null>(null);

  /* -- queries -------------------------------------------- */

  const { data: agent, isLoading, error } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => api.agents.get(agentId!),
    enabled: !!agentId,
  });

  const { data: tools } = useQuery({
    queryKey: ["agent-tools", agentId],
    queryFn: () => api.agents.getTools(agentId!),
    enabled: !!agentId,
  });

  const { data: toolCatalog } = useQuery({
    queryKey: ["tool-catalog"],
    queryFn: () => api.agents.toolCatalog(),
    enabled: showBindModal,
  });

  const { data: boundSkills } = useQuery({
    queryKey: ["agent-skills", agentId],
    queryFn: () => api.skills.listAgentBindings(agentId!),
    enabled: !!agentId,
  });

  const { data: availableSkills } = useQuery({
    queryKey: ["agent-skills-available", agentId],
    queryFn: () => api.skills.listAgentAvailable(agentId!),
    enabled: !!agentId && showBindSkillModal,
  });

  const { data: executions } = useQuery({
    queryKey: ["executions", agentId],
    queryFn: () => api.executions.list({ agent_id: agentId, limit: 50 }),
    enabled: !!agentId && (tab === "executions" || tab === "overview"),
  });

  const { data: deployments } = useQuery({
    queryKey: ["agent-deployments", agentId],
    queryFn: () => api.agents.deployments(agentId!),
    enabled: !!agentId && tab === "overview",
  });

  /* -- mutations ------------------------------------------ */

  const updateMutation = useMutation({
    mutationFn: (data: Partial<Agent>) => api.agents.update(agentId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.agents.delete(agentId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      navigate("/agents");
    },
  });

  const bindToolsMutation = useMutation({
    mutationFn: (toolIds: string[]) => api.agents.bindTools(agentId!, toolIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-tools", agentId] });
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
      setShowBindModal(false);
      setSelectedToolIds([]);
      setToolSearch("");
    },
  });

  const unbindToolMutation = useMutation({
    mutationFn: (toolIds: string[]) => api.agents.unbindTools(agentId!, toolIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-tools", agentId] });
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
    },
  });

  const bindSkillsMutation = useMutation({
    mutationFn: (skillIds: string[]) =>
      Promise.all(skillIds.map((skillId) => api.skills.bindSkill(agentId!, skillId))),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-skills", agentId] });
      queryClient.invalidateQueries({ queryKey: ["agent-skills-available", agentId] });
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
      setShowBindSkillModal(false);
      setSelectedSkillIds([]);
      setSkillSearch("");
    },
  });

  const unbindSkillMutation = useMutation({
    mutationFn: (skillId: string) => api.skills.unbindSkill(agentId!, skillId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-skills", agentId] });
      queryClient.invalidateQueries({ queryKey: ["agent-skills-available", agentId] });
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] });
    },
  });

  /* -- effects -------------------------------------------- */

  useEffect(() => {
    if (agent) {
      setNameDraft(agent.name);
      setPromptDraft(agent.system_prompt || "");
      setConfigDraft(agent.config || {});
      setCategoryDraft(agent.category || "");
      setTagsDraft(Array.isArray(agent.tags) ? agent.tags.join(", ") : "");
    }
  }, [agent]);

  /* -- handlers ------------------------------------------- */

  const saveName = () => {
    if (nameDraft.trim() && nameDraft !== agent?.name) {
      updateMutation.mutate({ name: nameDraft.trim() });
    }
    setEditingName(false);
  };

  const savePrompt = () => {
    updateMutation.mutate({ system_prompt: promptDraft });
    setEditingPrompt(false);
  };

  const saveConfig = () => {
    updateMutation.mutate({ config: configDraft } as Partial<Agent>);
    setEditingConfig(false);
  };

  const saveMeta = () => {
    updateMutation.mutate({
      category: categoryDraft,
      tags: tagsDraft.split(",").map((t) => t.trim()).filter(Boolean),
    });
    setEditingMeta(false);
  };

  const cloneAgent = () => {
    if (!agent) return;
    api.agents
      .create({
        name: `${agent.name} ${t("page.agent_detail.copy_suffix")}`,
        description: agent.description,
        system_prompt: agent.system_prompt,
        category: agent.category,
        tags: agent.tags,
        config: agent.config,
      })
      .then((newAgent) => {
        queryClient.invalidateQueries({ queryKey: ["agents"] });
        navigate(`/agents/${newAgent.id}`);
      });
  };

  const exportConfig = () => {
    if (!agent) return;
    const blob = new Blob([JSON.stringify(agent, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${agent.name.replace(/\s+/g, "_").toLowerCase()}_config.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  /* -- render --------------------------------------------- */

  if (isLoading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "96px 0" }}>
        <LoadingSpinner size={28} />
      </div>
    );
  }

  if (error || !agent) {
    return (
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "96px 0", textAlign: "center" }}>
        <div style={{ width: 64, height: 64, borderRadius: 20, background: "#f1dddb", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 16 }}>
          <IconWarning size={32} className="text-red-300" />
        </div>
        <p style={{ fontSize: 15, fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.agent_not_found")}</p>
        <Button variant="primary" onClick={() => navigate("/agents")}>
          {t("page.agent_detail.back_to_agents")}
        </Button>
      </div>
    );
  }

  const agentDescription = getAgentDescription(agent);

  const color = getFallbackColor(agent.name);
  const execItems = executions?.items || [];
  const toolCount = tools?.length ?? agent.tool_count ?? 0;
  const skillCount = boundSkills?.length ?? agent.skill_count ?? 0;
  const deploymentItems = (deployments || []) as AgentDeploymentResponse[];
  const connectionInfo = agentConnectionInfo(agent);
  const agentLearningEnabled = runtimeLearningEnabled(agent.config);
  const toggleAgentLearning = () => {
    updateMutation.mutate({
      config: mergeRuntimeLearningConfig(agent.config, !agentLearningEnabled),
    } as Partial<Agent>);
  };

  const tabs = [
    { key: "overview", label: t("page.agent_detail.overview") },
    { key: "tools", label: t("page.workspace_detail.tools"), count: toolCount },
    { key: "skills", label: t("page.agent_detail.skills"), count: skillCount },
    { key: "executions", label: t("page.agent_detail.executions"), count: execItems.length },
    { key: "settings", label: t("page.agent_detail.actions") },
  ];

  const filteredCatalog = (toolCatalog || []).filter((t: any) => {
    const bound = (tools || []).map((bt: any) => bt.id);
    const matchesSearch =
      !toolSearch ||
      (t.name || "").toLowerCase().includes(toolSearch.toLowerCase()) ||
      (t.description || "").toLowerCase().includes(toolSearch.toLowerCase());
    return !bound.includes(t.id) && matchesSearch;
  });

  const filteredAvailableSkills = (availableSkills || []).filter((skill: any) => {
    const bound = (boundSkills || []).map((s: any) => s.id);
    const description = getSkillDescription(skill);
    const matchesSearch =
      !skillSearch ||
      (skill.display_name || skill.name || "").toLowerCase().includes(skillSearch.toLowerCase()) ||
      description.toLowerCase().includes(skillSearch.toLowerCase());
    return !bound.includes(skill.id) && matchesSearch;
  });

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      {/* Back + Header */}
      <div style={{ marginBottom: 24 }}>
        <button
          onClick={() => navigate("/agents")}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 13,
            color: "#78716c",
            background: "transparent",
            border: "none",
            cursor: "pointer",
            marginBottom: 16,
            minHeight: 36,
            padding: "0 2px",
          }}
        >
          <IconChevronLeft size={16} />
          {t("page.agent_detail.back_to_agents")}
        </button>

        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 14,
            flexWrap: "wrap",
            padding: "4px 0 2px",
          }}
        >
          <AgentAvatar
            name={agent.name}
            avatarUrl={agent.avatar_url}
            seed={agent.id}
            size={48}
            shape="rounded"
          />

          <div style={{ flex: 1, minWidth: 220 }}>
            {editingName ? (
              <input
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                onBlur={saveName}
                onKeyDown={(e) => e.key === "Enter" && saveName()}
                autoFocus
                className="manor-input"
                style={{ fontSize: 20, fontWeight: 800, height: "auto", padding: "4px 8px" }}
              />
            ) : (
              <h1
                onClick={() => setEditingName(true)}
                style={{ fontSize: 22, fontWeight: 800, color: "#292524", margin: 0, cursor: "pointer", lineHeight: 1.2 }}
              >
                {agent.name}
              </h1>
            )}
            {agentDescription && (
              <p style={{ fontSize: 13, color: "#78716c", margin: "4px 0 0", maxWidth: 720, lineHeight: 1.45 }}>{agentDescription}</p>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
              <StatusBadge type={agent.status === "active" ? "success" : "inactive"} dot>
                {agent.status === "active" ? t("page.workspaces.filter_active") : agent.status || t("page.workspaces.draft")}
              </StatusBadge>
              <StatusBadge type={connectionInfo.type}>
                {connectionInfo.label}
              </StatusBadge>
              <span
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  padding: "2px 8px",
                  borderRadius: 99,
                  background: agent.source === "custom" ? "#f3e8ff" : "#dceae3",
                  color: agent.source === "custom" ? "#6f4ba8" : "#437f6b",
                }}
              >
                {agent.source === "custom" ? t("page.agent_detail.custom") : t("page.agent_detail.template")}
              </span>
            </div>
          </div>
          <Button variant="outline" size="sm" onClick={() => navigate(`/agents?edit=${agent.id}`)}>
            {t("page.agents.edit_agent")}
          </Button>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ marginBottom: 24 }}>
        <TabSwitcher tabs={tabs} value={tab} onChange={(k) => setTab(k as Tab)} wrap />
      </div>

      {/* Tab Content */}
      <div style={{ flex: 1, overflowY: "auto" }}>
        {/* ====== OVERVIEW ====== */}
        {tab === "overview" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
            {/* Quick stats */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 150px), 1fr))", gap: 10 }}>
              {[
                { label: t("page.agent_detail.total_executions"), value: execItems.length },
                {
                  label: t("page.agent_dashboard.kpi_avg_response"),
                  value: execItems.length
                    ? `${(execItems.reduce((s: number, e: any) => s + (e.duration_ms || 0), 0) / execItems.length / 1000).toFixed(1)}s`
                    : "--",
                },
                { label: "Workspace uses", value: deploymentItems.length },
                { label: "Capabilities", value: toolCount + skillCount },
              ].map((stat, i) => (
                <div key={i} className="glass-card" style={{ padding: "12px 14px" }}>
                  <p style={{ fontSize: 10, fontWeight: 700, color: "#a8a29e", textTransform: "uppercase", letterSpacing: "0.03em", margin: 0 }}>
                    {stat.label}
                  </p>
                  <p style={{ fontSize: 18, fontWeight: 800, color: "#292524", margin: "4px 0 0" }}>{stat.value}</p>
                </div>
              ))}
            </div>

            {/* Connection + workspace usage */}
            <div className="glass-card" style={{ padding: 20 }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start", flexWrap: "wrap", marginBottom: 16 }}>
                <div style={{ minWidth: 0 }}>
                  <h3 style={{ fontSize: 14, fontWeight: 800, color: "#44403c", margin: 0 }}>Connection</h3>
                  <p style={{ margin: "6px 0 0", fontSize: 13, lineHeight: 1.5, color: "#78716c" }}>
                    {connectionInfo.detail}
                  </p>
                  {connectionInfo.endpoint ? (
                    <p style={{ margin: "8px 0 0", fontSize: 12, color: "#78716c" }}>
                      Endpoint <span style={{ fontFamily: "monospace", color: "#44403c" }}>{connectionInfo.endpoint}</span>
                    </p>
                  ) : null}
                </div>
                <StatusBadge type={connectionInfo.type}>{connectionInfo.label}</StatusBadge>
              </div>

              {deploymentItems.length === 0 ? (
                <div style={{ border: "1px dashed rgba(28,25,23,0.06)", borderRadius: 12, padding: 14, background: "rgba(250,250,249,0.65)" }}>
                  <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: "#44403c" }}>Not used in a workspace yet</p>
                  <p style={{ margin: "4px 0 0", fontSize: 12, color: "#78716c" }}>
                    Add it from a workspace to choose Manor Hosted, CLI, HTTPS, or a custom profile for that workspace.
                  </p>
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {deploymentItems.map((deployment) => (
                    <div
                      key={deployment.id}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "minmax(0, 1fr) auto auto",
                        gap: 12,
                        alignItems: "center",
                        border: "1px solid rgba(28,25,23,0.06)",
                        borderRadius: 12,
                        padding: "10px 12px",
                        background: "#fff",
                      }}
                    >
                      <div style={{ minWidth: 0 }}>
                        <p style={{ margin: 0, fontSize: 13, fontWeight: 800, color: "#44403c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {deployment.workspace_name || "Workspace"}
                        </p>
                        <p style={{ margin: "3px 0 0", fontSize: 11, color: "#a8a29e" }}>
                          service <span style={{ fontFamily: "monospace" }}>{deployment.service_key || "unscoped"}</span>
                        </p>
                      </div>
                      <StatusBadge type={(deployment.workers || []).length > 0 ? "success" : "warning"}>
                        {(deployment.workers || []).length > 0 ? `${deployment.workers.length} worker${deployment.workers.length === 1 ? "" : "s"}` : "Hosted"}
                      </StatusBadge>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => deployment.workspace_id && navigate(`/workspaces/${deployment.workspace_id}?tab=agents`)}
                        disabled={!deployment.workspace_id}
                      >
                        Open
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* System prompt */}
            <div className="glass-card" style={{ padding: 20 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
                <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.skill_form.system_prompt")}</h3>
                {editingPrompt ? (
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button variant="outline" size="sm" onClick={() => setEditingPrompt(false)}>{t("action.cancel")}</Button>
                    <Button variant="primary" size="sm" onClick={savePrompt}>{t("action.save")}</Button>
                  </div>
                ) : (
                  <button onClick={() => setEditingPrompt(true)} style={{ fontSize: 12, fontWeight: 600, color: "#436b65", background: "transparent", border: "none", cursor: "pointer", minHeight: 32, padding: "6px 8px", borderRadius: 8 }}>{t("action.edit")}</button>
                )}
              </div>
              {editingPrompt ? (
                <Textarea
                  value={promptDraft}
                  onChange={(e) => setPromptDraft(e.target.value)}
                  rows={8}
                />
              ) : (
                <div style={{ background: "rgba(28,25,23,0.04)", borderRadius: 12, padding: "12px 16px", fontSize: 13, color: "#57534e", fontFamily: "monospace", whiteSpace: "pre-wrap", maxHeight: 256, overflowY: "auto" }}>
                  {agent.system_prompt || t("page.agent_detail.no_system_prompt_configured")}
                </div>
              )}
            </div>

            {/* Config */}
            <div className="glass-card" style={{ padding: 20 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
                <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.flows.configuration")}</h3>
                {editingConfig ? (
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button variant="outline" size="sm" onClick={() => setEditingConfig(false)}>{t("action.cancel")}</Button>
                    <Button variant="primary" size="sm" onClick={saveConfig}>{t("action.save")}</Button>
                  </div>
                ) : (
                  <button onClick={() => setEditingConfig(true)} style={{ fontSize: 12, fontWeight: 600, color: "#436b65", background: "transparent", border: "none", cursor: "pointer", minHeight: 32, padding: "6px 8px", borderRadius: 8 }}>{t("action.edit")}</button>
                )}
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 16 }}>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 4 }}>{t("page.agent_detail.model_override")}</label>
                  {editingConfig ? (
                    <Input value={configDraft.model || ""} onChange={(e) => setConfigDraft({ ...configDraft, model: e.target.value })} placeholder={t("page.agent_detail.e_g_gpt_4o")} />
                  ) : (
                    <p style={{ fontSize: 13, color: "#44403c", margin: 0 }}>{agent.config?.model || t("page.api_keys.default")}</p>
                  )}
                </div>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 4 }}>
                    {t("page.agent_detail.temperature")} {editingConfig ? configDraft.temperature ?? 0.7 : agent.config?.temperature ?? 0.7}
                  </label>
                  {editingConfig ? (
                    <input type="range" min="0" max="2" step="0.1" value={configDraft.temperature ?? 0.7} onChange={(e) => setConfigDraft({ ...configDraft, temperature: parseFloat(e.target.value) })} style={{ width: "100%", accentColor: "#436b65" }} />
                  ) : (
                    <div style={{ width: "100%", background: "#f5f5f4", borderRadius: 99, height: 8, marginTop: 8 }}>
                      <div style={{ background: "#436b65", height: 8, borderRadius: 99, width: `${((agent.config?.temperature ?? 0.7) / 2) * 100}%` }} />
                    </div>
                  )}
                </div>
                <div>
                  <label style={{ display: "block", fontSize: 12, fontWeight: 500, color: "#78716c", marginBottom: 4 }}>{t("page.agent_detail.max_tokens")}</label>
                  {editingConfig ? (
                    <Input type="number" value={String(configDraft.max_tokens || "")} onChange={(e) => setConfigDraft({ ...configDraft, max_tokens: parseInt(e.target.value) || undefined })} placeholder="4096" />
                  ) : (
                    <p style={{ fontSize: 13, color: "#44403c", margin: 0 }}>{agent.config?.max_tokens || t("page.api_keys.default")}</p>
                  )}
                </div>
              </div>
            </div>

            {/* Category + Tags */}
            <div className="glass-card" style={{ padding: 20 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
                <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.agent_detail.category_tags")}</h3>
                {editingMeta ? (
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button variant="outline" size="sm" onClick={() => setEditingMeta(false)}>{t("action.cancel")}</Button>
                    <Button variant="primary" size="sm" onClick={saveMeta}>{t("action.save")}</Button>
                  </div>
                ) : (
                  <button onClick={() => setEditingMeta(true)} style={{ fontSize: 12, fontWeight: 600, color: "#436b65", background: "transparent", border: "none", cursor: "pointer", minHeight: 32, padding: "6px 8px", borderRadius: 8 }}>{t("action.edit")}</button>
                )}
              </div>
              {editingMeta ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  <select value={categoryDraft} onChange={(e) => setCategoryDraft(e.target.value)} className="manor-input">
                    <option value="">{t("page.agent_detail.no_category")}</option>
                    {CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                  </select>
                  <Input value={tagsDraft} onChange={(e) => setTagsDraft(e.target.value)} placeholder={t("page.agent_detail.comma_separated_tags")} />
                </div>
              ) : (
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                  {agent.category && (
                    <span style={{ fontSize: 11, fontWeight: 600, padding: "2px 10px", borderRadius: 99, background: "#e3ebe8", color: "#436b65" }}>{agent.category}</span>
                  )}
                  {agent.tags && (Array.isArray(agent.tags) ? agent.tags : []).map((tag, i) => (
                    <span key={i} style={{ fontSize: 11, padding: "2px 10px", borderRadius: 8, background: "#f5f5f4", color: "#78716c" }}>{tag}</span>
                  ))}
                  {!agent.category && (!agent.tags || agent.tags.length === 0) && (
                    <span style={{ fontSize: 12, color: "#a8a29e" }}>{t("page.agent_detail.no_category_or_tags_set")}</span>
                  )}
                </div>
              )}
            </div>

            {/* Dates */}
            <div className="glass-card" style={{ padding: 20 }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: "0 0 12px 0" }}>{t("page.agent_detail.details")}</h3>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 16, fontSize: 13 }}>
                <div>
                  <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.dashboard.created")}</span>
                  <p style={{ color: "#44403c", margin: "2px 0 0 0" }}>{formatDateFull((agent as any).created_at)}</p>
                </div>
                <div>
                  <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.agent_detail.last_updated")}</span>
                  <p style={{ color: "#44403c", margin: "2px 0 0 0" }}>{formatDateFull((agent as any).updated_at)}</p>
                </div>
              </div>
            </div>
          </div>
	        )}

	        {/* ====== TOOLS ====== */}
        {tab === "tools" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.agent_detail.bound_tools")}{toolCount})</h3>
              <Button variant="primary" size="sm" onClick={() => setShowBindModal(true)}>
                <IconPlus size={14} />
                {t("page.agent_detail.bind_tool")}
              </Button>
            </div>

            {(tools || []).length === 0 ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "64px 0", textAlign: "center" }}>
                <div style={{ width: 56, height: 56, borderRadius: 20, background: "#f5f5f4", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 12 }}>
                  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17l-5.1-5.1M20.49 3.51l-5.1 5.1M6.32 20.49l5.1-5.1m3.07-3.07l5.1-5.1M12 3v2.25M12 18.75V21M6.75 12H4.5m15 0h-2.25M8.636 8.636l-1.591-1.591m10.91 10.91l-1.591-1.591m0-7.728l1.591-1.591M7.045 18.955l1.591-1.591" />
                  </svg>
                </div>
                <p style={{ fontSize: 14, fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.no_tools_bound")}</p>
                <p style={{ fontSize: 12, color: "#a8a29e", marginTop: 4 }}>{t("page.agent_detail.bind_tools_to_extend_this_agent_s_capabilities")}</p>
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12 }}>
                {(tools || []).map((tool: any) => (
                  <div key={tool.id} className="glass-card-sm" style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                    <div style={{ width: 36, height: 36, borderRadius: 10, background: "#e3e9f1", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#5f84bd" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17l-5.1-5.1M20.49 3.51l-5.1 5.1M6.32 20.49l5.1-5.1m3.07-3.07l5.1-5.1" />
                      </svg>
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <h4 style={{ fontSize: 13, fontWeight: 700, color: "#44403c", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tool.name}</h4>
                      {tool.description && (
                        <p style={{ fontSize: 12, color: "#78716c", marginTop: 2, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{tool.description}</p>
                      )}
                      {tool.category && (
                        <span style={{ display: "inline-block", marginTop: 4, fontSize: 10, fontWeight: 600, padding: "2px 8px", borderRadius: 99, background: "#e3e9f1", color: "#4869ac" }}>{tool.category}</span>
                      )}
                    </div>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => unbindToolMutation.mutate([tool.id])}
                      disabled={unbindToolMutation.isPending}
                    >
                      {t("page.skills.unbind")}
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {/* Bind Tool Modal */}
            <Modal
              open={showBindModal}
              onClose={() => { setShowBindModal(false); setSelectedToolIds([]); setToolSearch(""); }}
              title={t("page.agent_detail.bind_tools")}
              footer={
                <>
                  <Button variant="outline" onClick={() => { setShowBindModal(false); setSelectedToolIds([]); }}>{t("action.cancel")}</Button>
                  <Button
                    variant="primary"
                    onClick={() => bindToolsMutation.mutate(selectedToolIds)}
                    disabled={selectedToolIds.length === 0 || bindToolsMutation.isPending}
                    loading={bindToolsMutation.isPending}
                  >
                    {bindToolsMutation.isPending
                      ? t("page.agent_detail.binding")
                      : t("page.agent_detail.bind_selected_tools").replace(
                          "{count}",
                          String(selectedToolIds.length),
                        )}
                  </Button>
                </>
              }
            >
              <SearchInput value={toolSearch} onChange={setToolSearch} placeholder={t("page.agent_detail.search_tools")} className="mb-4" />
              <div style={{ maxHeight: 256, overflowY: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
                {filteredCatalog.length === 0 ? (
                  <p style={{ fontSize: 13, color: "#a8a29e", textAlign: "center", padding: 24 }}>{t("page.agent_detail.no_tools_available")}</p>
                ) : (
                  filteredCatalog.map((tool: any) => (
                    <label
                      key={tool.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 12,
                        padding: 12,
                        borderRadius: 12,
                        cursor: "pointer",
                        transition: "all 0.2s",
                        background: selectedToolIds.includes(tool.id) ? "#e3ebe8" : "transparent",
                        border: selectedToolIds.includes(tool.id) ? "1px solid rgba(79,125,117,0.3)" : "1px solid transparent",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={selectedToolIds.includes(tool.id)}
                        onChange={(e) => {
                          setSelectedToolIds(
                            e.target.checked
                              ? [...selectedToolIds, tool.id]
                              : selectedToolIds.filter((id) => id !== tool.id)
                          );
                        }}
                        style={{ accentColor: "#436b65" }}
                      />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <p style={{ fontSize: 13, fontWeight: 600, color: "#44403c", margin: 0 }}>{tool.name}</p>
                        {tool.description && (
                          <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tool.description}</p>
                        )}
                      </div>
                    </label>
                  ))
                )}
              </div>
            </Modal>
          </div>
        )}

        {/* ====== SKILLS ====== */}
        {tab === "skills" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.agent_detail.bound_skills")}{skillCount})</h3>
              <Button variant="primary" size="sm" onClick={() => setShowBindSkillModal(true)}>
                <IconPlus size={14} />
                {t("page.agent_detail.bind_skill")}
              </Button>
            </div>

            {(boundSkills || []).length === 0 ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "64px 0", textAlign: "center" }}>
                <div style={{ width: 56, height: 56, borderRadius: 20, background: "#f2f6f5", color: "#436b65", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 12, fontWeight: 800, fontSize: 20 }}>
                  S
                </div>
                <p style={{ fontSize: 14, fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.no_skills_bound")}</p>
                <p style={{ fontSize: 12, color: "#a8a29e", marginTop: 4 }}>{t("page.agent_detail.bind_skills_to_extend_this_agent_s_capabilities")}</p>
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))", gap: 12 }}>
                {(boundSkills || []).map((skill: any) => {
                  const description = getSkillDescription(skill);
                  return (
                    <div key={skill.id} className="glass-card-sm" style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                      <div style={{ width: 36, height: 36, borderRadius: 10, background: "#f2f6f5", color: "#436b65", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, fontWeight: 800 }}>
                        {(skill.display_name || skill.name || "S").charAt(0).toUpperCase()}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <h4 style={{ fontSize: 13, fontWeight: 700, color: "#44403c", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{skill.display_name || skill.name}</h4>
                        {description && (
                          <p style={{ fontSize: 12, color: "#78716c", marginTop: 2, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{description}</p>
                        )}
                        {skill.category && (
                          <span style={{ display: "inline-block", marginTop: 4, fontSize: 10, fontWeight: 600, padding: "2px 8px", borderRadius: 99, background: "#dceae3", color: "#3f7361" }}>{skill.category}</span>
                        )}
                      </div>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => unbindSkillMutation.mutate(skill.id)}
                        disabled={unbindSkillMutation.isPending}
                      >
                        {t("page.skills.unbind")}
                      </Button>
                    </div>
                  );
                })}
              </div>
            )}

            <Modal
              open={showBindSkillModal}
              onClose={() => { setShowBindSkillModal(false); setSelectedSkillIds([]); setSkillSearch(""); }}
              title={t("page.agent_detail.bind_skills")}
              footer={
                <>
                  <Button variant="outline" onClick={() => { setShowBindSkillModal(false); setSelectedSkillIds([]); setSkillSearch(""); }}>{t("action.cancel")}</Button>
                  <Button
                    variant="primary"
                    onClick={() => bindSkillsMutation.mutate(selectedSkillIds)}
                    disabled={selectedSkillIds.length === 0 || bindSkillsMutation.isPending}
                    loading={bindSkillsMutation.isPending}
                  >
                    {bindSkillsMutation.isPending
                      ? t("page.agent_detail.binding")
                      : t("page.agent_detail.bind_selected_skills").replace(
                          "{count}",
                          String(selectedSkillIds.length),
                        )}
                  </Button>
                </>
              }
            >
              <SearchInput value={skillSearch} onChange={setSkillSearch} placeholder={t("page.agent_detail.search_skills")} className="mb-4" />
              <div style={{ maxHeight: 320, overflowY: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
                {filteredAvailableSkills.length === 0 ? (
                  <p style={{ fontSize: 13, color: "#a8a29e", textAlign: "center", padding: 24 }}>{t("page.agent_detail.no_skills_available")}</p>
                ) : (
                  filteredAvailableSkills.map((skill: any) => {
                    const description = getSkillDescription(skill);
                    return (
                      <label
                        key={skill.id}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 12,
                          padding: 12,
                          borderRadius: 12,
                          cursor: "pointer",
                          transition: "all 0.2s",
                          background: selectedSkillIds.includes(skill.id) ? "#e3ebe8" : "transparent",
                          border: selectedSkillIds.includes(skill.id) ? "1px solid rgba(79,125,117,0.3)" : "1px solid transparent",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={selectedSkillIds.includes(skill.id)}
                          onChange={(e) => {
                            setSelectedSkillIds(
                              e.target.checked
                                ? [...selectedSkillIds, skill.id]
                                : selectedSkillIds.filter((id) => id !== skill.id)
                            );
                          }}
                          style={{ accentColor: "#436b65" }}
                        />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <p style={{ fontSize: 13, fontWeight: 600, color: "#44403c", margin: 0 }}>{skill.display_name || skill.name}</p>
                          {description && (
                            <p style={{ fontSize: 12, color: "#a8a29e", margin: "2px 0 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{description}</p>
                          )}
                        </div>
                      </label>
                    );
                  })
                )}
              </div>
            </Modal>
          </div>
        )}

        {/* ====== EXECUTIONS ====== */}
        {tab === "executions" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>{t("page.agent_detail.recent_executions")}{execItems.length})</h3>

            {execItems.length === 0 ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "64px 0", textAlign: "center" }}>
                <div style={{ width: 56, height: 56, borderRadius: 20, background: "#f5f5f4", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 12 }}>
                  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75z" />
                  </svg>
                </div>
                <p style={{ fontSize: 14, fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.no_executions_yet")}</p>
              </div>
            ) : (
              <div className="glass-card" style={{ overflow: "hidden", padding: 0 }}>
                <table className="glass-table" style={{ width: "100%", fontSize: 13 }}>
                  <thead>
                    <tr>
                      <th>{t("page.agent_dashboard.status")}</th>
                      <th>{t("page.agent_dashboard.input")}</th>
                      <th>{t("page.agent_detail.output")}</th>
                      <th>{t("page.agent_detail.turns")}</th>
                      <th>{t("page.agent_dashboard.duration")}</th>
                      <th>{t("page.agent_dashboard.date")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {execItems.map((exec: any) => (
                      <>
                        <tr
                          key={exec.id}
                          onClick={() => setExpandedExecId(expandedExecId === exec.id ? null : exec.id)}
                          style={{ cursor: "pointer", transition: "all 0.2s" }}
                        >
                          <td style={{ padding: "12px 16px" }}>
                            <StatusBadge
                              type={exec.status === "completed" ? "success" : exec.status === "failed" ? "danger" : exec.status === "running" ? "info" : "inactive"}
                              dot
                            >
                              {exec.status || t("page.browser_sessions.unknown")}
                            </StatusBadge>
                          </td>
                          <td style={{ padding: "12px 16px", color: "#57534e", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 180 }}>
                            {exec.input_preview || exec.input?.substring(0, 60) || "--"}
                          </td>
                          <td style={{ padding: "12px 16px", color: "#57534e", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 180 }}>
                            {exec.output_preview || exec.output?.substring(0, 60) || "--"}
                          </td>
                          <td style={{ padding: "12px 16px", color: "#57534e" }}>{exec.turns ?? "--"}</td>
                          <td style={{ padding: "12px 16px", color: "#57534e" }}>
                            {exec.duration_ms ? `${(exec.duration_ms / 1000).toFixed(1)}s` : "--"}
                          </td>
                          <td style={{ padding: "12px 16px", color: "#a8a29e", fontSize: 12 }}>
                            {relativeTime(exec.created_at)}
                          </td>
                        </tr>
                        {expandedExecId === exec.id && (
                          <tr key={`${exec.id}-expand`}>
                            <td colSpan={6} style={{ padding: "16px 16px", background: "rgba(250,250,249,0.5)" }}>
                              <div style={{ display: "flex", flexDirection: "column", gap: 12, fontSize: 12 }}>
                                <div>
                                  <span style={{ fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.full_input")}</span>
                                  <pre style={{ marginTop: 4, background: "rgba(255,255,255,0.7)", borderRadius: 10, padding: 12, overflow: "auto", color: "#57534e", whiteSpace: "pre-wrap" }}>
                                    {exec.input || "--"}
                                  </pre>
                                </div>
                                <div>
                                  <span style={{ fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.full_output")}</span>
                                  <pre style={{ marginTop: 4, background: "rgba(255,255,255,0.7)", borderRadius: 10, padding: 12, overflow: "auto", color: "#57534e", whiteSpace: "pre-wrap" }}>
                                    {exec.output || "--"}
                                  </pre>
                                </div>
                                {exec.tools_used && (
                                  <div>
                                    <span style={{ fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.tools_used")} </span>
                                    <span style={{ color: "#57534e" }}>{Array.isArray(exec.tools_used) ? exec.tools_used.join(", ") : exec.tools_used}</span>
                                  </div>
                                )}
                                {exec.token_usage && (
                                  <div>
                                    <span style={{ fontWeight: 600, color: "#78716c" }}>{t("page.agent_detail.token_usage")} </span>
                                    <span style={{ color: "#57534e" }}>{typeof exec.token_usage === "object" ? JSON.stringify(exec.token_usage) : exec.token_usage}</span>
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
            )}
          </div>
        )}

        {/* ====== SETTINGS ====== */}
        {tab === "settings" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 640 }}>
            <div className="glass-card" style={{ padding: 20 }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start" }}>
                <div style={{ minWidth: 0 }}>
                  <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: 0 }}>
                    {t("page.agent_detail.runtime_learning")}
                  </h3>
                  <p style={{ margin: "6px 0 0", fontSize: 13, lineHeight: 1.5, color: "#78716c" }}>
                    {t("page.agent_detail.runtime_learning_desc")}
                  </p>
                </div>
                <Toggle
                  checked={agentLearningEnabled}
                  onChange={toggleAgentLearning}
                  disabled={updateMutation.isPending}
                  aria-label={t("page.agent_detail.runtime_learning")}
                />
              </div>
            </div>

            <div className="glass-card" style={{ padding: 20 }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#44403c", margin: "0 0 16px 0" }}>{t("page.custom_fields.actions")}</h3>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
                <Button variant="outline" onClick={cloneAgent}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 01-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H6.75a9.06 9.06 0 011.5.124m7.5 10.376h3.375c.621 0 1.125-.504 1.125-1.125V11.25c0-4.46-3.243-8.161-7.5-8.876a9.06 9.06 0 00-1.5-.124H9.375c-.621 0-1.125.504-1.125 1.125v3.5m7.5 10.375H9.375a1.125 1.125 0 01-1.125-1.125v-9.25m12 6.625v-1.875a3.375 3.375 0 00-3.375-3.375h-1.5a1.125 1.125 0 01-1.125-1.125v-1.5a3.375 3.375 0 00-3.375-3.375H9.75" />
                  </svg>
                  {t("page.agent_detail.clone_agent")}
                </Button>
                <Button variant="outline" onClick={() => updateMutation.mutate({ is_public: !agent.is_public })}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
                    {agent.is_public ? (
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 10.5V6.75a4.5 4.5 0 119 0v3.75M3.75 21.75h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H3.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                    ) : (
                      <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                    )}
                  </svg>
                  {agent.is_public ? t("page.agent_detail.make_private") : t("page.agent_detail.make_public")}
                </Button>
                <Button variant="outline" onClick={exportConfig}>
                  <IconDownload size={16} />
                  {t("page.agent_detail.export_json")}
                </Button>
              </div>
            </div>

            {/* Danger zone */}
            <div className="glass-card" style={{ padding: 20, border: "1px solid rgba(214,95,89,0.2)" }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#c14a44", margin: "0 0 12px 0" }}>{t("page.agent_detail.danger_zone")}</h3>
              <button
                onClick={() => setShowDeleteConfirm(true)}
                style={{
                  padding: "8px 16px",
                  border: "2px solid #ddafac",
                  color: "#c14a44",
                  fontSize: 13,
                  fontWeight: 600,
                  borderRadius: 12,
                  background: "transparent",
                  cursor: "pointer",
                  transition: "background 0.2s",
                }}
              >
                {t("page.agent_detail.delete_agent")}
              </button>
            </div>

            <ConfirmDialog
              open={showDeleteConfirm}
              onClose={() => setShowDeleteConfirm(false)}
              onConfirm={() => deleteMutation.mutate()}
              title={t("page.agent_detail.delete_agent")}
              message={t("page.agent_detail.are_you_sure_this_cannot_be_undone")}
              confirmLabel={deleteMutation.isPending ? t("page.task_collections.deleting") : t("page.agent_detail.yes_delete")}
              danger
            />
          </div>
        )}
      </div>
    </div>
  );
}
