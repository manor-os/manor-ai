import React, { useEffect, useState, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, type AgentDeploymentResponse } from "../lib/api";
import { useToastStore } from "../stores/toast";
import { useAuthStore } from "../stores/auth";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import Dropdown from "../components/ui/Dropdown";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import { SkeletonCard } from "../components/ui/Skeleton";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Card from "../components/ui/Card";
import Chip from "../components/ui/Chip";
import SmartToolbar from "../components/ui/SmartToolbar";
import StatusBadge from "../components/ui/StatusBadge";
import SharedAgentAvatar from "../components/ui/AgentAvatar";
import CompactCard from "../components/ui/CompactCard";
import { openDetail, closeDetail } from "../stores/detail";
import { useAgentEditModalStore, openAgentEditModal } from "../stores/agentEditModal";
import { TestPromptPanel } from "../components/AgentEditModal";
import { t } from "../lib/i18n";
import { getAgentDescription } from "../lib/localizedContent";
import { formatUserFacingText } from "../lib/taskDisplay";
import { parseTags, displayAgentCategory, displayAgentTag, displayToolName, displayToolDescription } from "../lib/agentDisplay";
import type { Agent } from "../lib/types";
import {
  PROMPT_VARIABLES,
  improvePrompt,
  runTest,
  insertVariable,
} from "../lib/agentPromptHelpers";
import { objectConfig } from "../lib/agentRuntimeConfig";
import {
  IconAgent,
  IconEdit,
  IconTrash,
  IconSearch,
  IconWrench,
  IconPlus,
  IconUpload,
} from "../components/icons";

type Tab =
  | "my"
;
type AgentDeploymentMap = Record<string, AgentDeploymentResponse[]>;

const CATEGORIES = [
  { value: "All", label: t("page.workspaces.filter_all") },
  { value: "Essential", label: t("page.agents.category_essential") },
  { value: "Growth", label: t("page.agents.category_growth") },
  { value: "Specialist", label: t("page.agents.category_specialist") },
  { value: "Property Management", label: t("page.agents.property_management") },
  { value: "Customer Service", label: t("page.agents.category_customer_service") },
];


function agentConnectionInfo(agent: Record<string, any>): {
  label: string;
  detail: string;
  bg: string;
  fg: string;
} {
  const connection = objectConfig(objectConfig(agent.config).runtime_connection);
  const source = String(connection.source || "").toLowerCase();
  if (source === "https") {
    return {
      label: "HTTPS",
      detail: "Connected from a workspace",
      bg: "#f1f3f9",
      fg: "#494596",
    };
  }
  return {
    label: "Manor Hosted",
    detail: "Default connection",
    bg: "#f1f6f3",
    fg: "#3f7361",
  };
}


/* ── Fallback avatar colours ── */
const FALLBACK_COLORS = [
  { bg: "#efedea", fg: "#57534e" },
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

function AgentAvatar({
  name,
  avatarUrl,
  seed,
  size = 60,
}: {
  name: string;
  avatarUrl?: string;
  seed?: string;
  size?: number;
}) {
  // Delegates to the shared generator (simple line-drawing character face),
  // keeping the rounded-square shape the page's containers expect.
  return (
    <SharedAgentAvatar
      name={name}
      avatarUrl={avatarUrl}
      seed={seed}
      size={size}
      shape="rounded"
    />
  );
}

function displayAgentDescription(agent: any): string {
  const text = formatUserFacingText(getAgentDescription(agent));
  const genericMatch = text.match(/^General agent for ['"]?(.+?)['"]? capability\.?$/i);
  if (genericMatch?.[1]) return `Handles ${genericMatch[1]} work.`;
  return text;
}

function uniqueItems(items: string[], limit: number): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of items) {
    const normalized = item.trim();
    if (!normalized) continue;
    const key = normalized.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(normalized);
    if (out.length >= limit) break;
  }
  return out;
}

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function readTextFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(t("page.agents.import_read_failed")));
    reader.readAsText(file);
  });
}

function importedAgentRows(value: unknown): Record<string, any>[] {
  if (Array.isArray(value)) return value.filter(isRecord);
  if (isRecord(value) && Array.isArray(value.agents)) return value.agents.filter(isRecord);
  if (isRecord(value) && isRecord(value.agent)) return [value.agent];
  return isRecord(value) ? [value] : [];
}

function agentImportPayload(raw: Record<string, any>) {
  const name = String(raw.name || raw.agent_name || raw.title || "").trim();
  const systemPrompt = String(
    raw.system_prompt || raw.systemPrompt || raw.prompt || raw.instructions || "",
  ).trim();
  const description = String(raw.description || raw.summary || "").trim();
  const tags = parseTags(raw.tags);
  const config = isRecord(raw.config) ? raw.config : {};
  return {
    valid: !!name && !!systemPrompt,
    reason: !name
      ? t("page.agents.import_missing_name")
      : !systemPrompt
        ? t("page.agents.import_missing_prompt")
        : "",
    payload: {
      name,
      description,
      system_prompt: systemPrompt,
      avatar_url: String(raw.avatar_url || raw.avatarUrl || ""),
      category: String(raw.category || raw.role || t("page.agents.imported") || "Imported"),
      tags,
      source: "custom",
      config: {
        ...config,
        import_source: config.import_source || "local_file",
      },
    },
    toolIds: Array.isArray(raw.tool_ids) ? raw.tool_ids.filter((id: any) => typeof id === "string") : [],
    skillIds: Array.isArray(raw.skill_ids) ? raw.skill_ids.filter((id: any) => typeof id === "string") : [],
  };
}

function ImportAgentsDialog({
  open,
  onClose,
  onImported,
}: {
  open: boolean;
  onClose: () => void;
  onImported: () => void;
}) {
  const toast = useToastStore();
  const [fileName, setFileName] = useState("");
  const [rows, setRows] = useState<ReturnType<typeof agentImportPayload>[]>([]);
  const [error, setError] = useState("");
  const [importing, setImporting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setFileName("");
    setRows([]);
    setError("");
    setImporting(false);
  }, [open]);

  const validRows = rows.filter((row) => row.valid);
  const invalidRows = rows.filter((row) => !row.valid);

  const onFileChange = async (file?: File | null) => {
    setRows([]);
    setError("");
    setFileName(file?.name || "");
    if (!file) return;
    try {
      const parsed = JSON.parse(await readTextFile(file));
      const next = importedAgentRows(parsed).map(agentImportPayload);
      if (next.length === 0) {
        setError(t("page.agents.import_invalid_agents"));
        return;
      }
      setRows(next);
    } catch (err: any) {
      setError(err?.message || t("page.agents.import_invalid_json"));
    }
  };

  const doImport = async () => {
    if (validRows.length === 0) {
      setError(t("page.agents.import_select_file_first"));
      return;
    }
    setImporting(true);
    setError("");
    let imported = 0;
    let failed = 0;
    try {
      for (const row of validRows) {
        try {
          const agent = await api.agents.create(row.payload);
          const bindings: Promise<unknown>[] = [];
          if (agent?.id && row.toolIds.length > 0) {
            bindings.push(api.agents.bindTools(agent.id, row.toolIds));
          }
          if (agent?.id && row.skillIds.length > 0) {
            for (const skillId of row.skillIds) {
              bindings.push(api.skills.bindSkill(agent.id, skillId));
            }
          }
          if (bindings.length > 0) await Promise.allSettled(bindings);
          imported += 1;
        } catch {
          failed += 1;
        }
      }
      if (imported > 0) {
        toast.success(
          t("page.agents.imported_agents_count").replace("{count}", String(imported)),
        );
        onImported();
        onClose();
      }
      if (failed > 0 || imported === 0) {
        toast.error(t("page.agents.import_failed_count").replace("{count}", String(failed || validRows.length)));
      }
    } finally {
      setImporting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={importing ? () => {} : onClose}
      title={t("page.agents.import_agents")}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={importing}>{t("action.cancel")}</Button>
          <Button variant="primary" onClick={doImport} disabled={validRows.length === 0 || importing} loading={importing}>
            {importing
              ? t("page.agents.importing_agents")
              : t("page.agents.import_agents_count").replace("{count}", String(validRows.length))}
          </Button>
        </>
      }
    >
      <div style={{ display: "grid", gap: 14 }}>
        <p style={{ margin: 0, color: "#78716c", fontSize: 13, lineHeight: 1.55 }}>
          {t("page.agents.import_agents_desc")}
        </p>
        <label style={{
          border: "1px dashed rgba(67,107,101,0.32)",
          borderRadius: 14,
          background: "rgba(241,246,243,0.56)",
          padding: 16,
          cursor: "pointer",
          display: "grid",
          gap: 6,
        }}>
          <input
            type="file"
            accept="application/json,.json"
            onChange={(e) => onFileChange(e.target.files?.[0])}
            style={{ display: "none" }}
          />
          <span style={{ display: "flex", alignItems: "center", gap: 8, color: "#436b65", fontSize: 13, fontWeight: 800 }}>
            <IconUpload size={15} />
            {fileName || t("page.agents.choose_agents_json")}
          </span>
          <span style={{ color: "#78716c", fontSize: 12 }}>
            {t("page.agents.choose_agents_json_hint")}
          </span>
        </label>
        {rows.length > 0 && (
          <div style={{ border: "1px solid #e7e5e4", borderRadius: 12, overflow: "hidden" }}>
            {rows.slice(0, 8).map((row, idx) => (
              <div
                key={`${row.payload.name}-${idx}`}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 10,
                  padding: "9px 11px",
                  borderTop: idx === 0 ? "none" : "1px solid #f5f5f4",
                  fontSize: 12,
                }}
              >
                <span style={{ color: row.valid ? "#292524" : "#b91c1c", fontWeight: 700, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {row.payload.name || t("page.agents.untitled_import")}
                </span>
                <span style={{ color: row.valid ? "#436b65" : "#b91c1c", flexShrink: 0 }}>
                  {row.valid ? t("page.import_skills_dialog.valid") : row.reason}
                </span>
              </div>
            ))}
            {rows.length > 8 && (
              <div style={{ padding: "8px 11px", color: "#78716c", fontSize: 12 }}>
                {t("page.agents.import_more_agents").replace("{count}", String(rows.length - 8))}
              </div>
            )}
          </div>
        )}
        {invalidRows.length > 0 && (
          <div style={{ color: "#a16207", fontSize: 12, lineHeight: 1.5 }}>
            {t("page.agents.import_invalid_count").replace("{count}", String(invalidRows.length))}
          </div>
        )}
        {error && <div style={{ color: "#b91c1c", fontSize: 12, lineHeight: 1.5 }}>{error}</div>}
      </div>
    </Modal>
  );
}


function agentWorkspaceItems(deployments?: AgentDeploymentResponse[]) {
  const byWorkspace = new Map<
    string,
    { id: string; name: string; serviceKey: string | null; status: string }
  >();
  for (const deployment of deployments || []) {
    if (!deployment.workspace_id) continue;
    const current = byWorkspace.get(deployment.workspace_id);
    if (current && current.status === "active") continue;
    byWorkspace.set(deployment.workspace_id, {
      id: deployment.workspace_id,
      name: deployment.workspace_name || "Workspace",
      serviceKey: deployment.service_key || null,
      status: deployment.status || "active",
    });
  }
  return Array.from(byWorkspace.values());
}

export default function Agents() {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const promptTextareaRef = useRef<HTMLTextAreaElement>(null);
  const closingEditAgentIdRef = useRef<string | null>(null);

  const [tab, setTab] = useState<Tab>("my");
  const [showImportModal, setShowImportModal] = useState(false);
  const [mySearch, setMySearch] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [hoveredAction, setHoveredAction] = useState<string | null>(null);


  // Recommendation widget

  // Prompt editor (for subscribed/hired agents)
  const [promptModal, setPromptModal] = useState(false);
  const [promptAgent, setPromptAgent] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [editPromptText, setEditPromptText] = useState("");
  const [showOriginalPrompt, setShowOriginalPrompt] = useState(false);
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [promptTestMsg, setPromptTestMsg] = useState("");
  const [promptTestResp, setPromptTestResp] = useState("");
  const [promptTestLoading, setPromptTestLoading] = useState(false);
  const [improvingPrompt, setImprovingPrompt] = useState(false);


  const { data: myAgents, isLoading: myLoading } = useQuery({
    queryKey: ["agents", "my"],
    queryFn: () => api.agents.list(),
  });
  const myAgentIds = React.useMemo(
    () =>
      ((myAgents as any[]) || [])
        .map((agent: any) => String(agent.id || ""))
        .filter(Boolean)
        .sort(),
    [myAgents],
  );

  const { data: agentDeploymentsById } = useQuery<AgentDeploymentMap>({
    queryKey: ["agent-deployments", "my", myAgentIds.join(",")],
    queryFn: async () => {
      const entries = await Promise.all(
        myAgentIds.map(async (agentId) => {
          try {
            const deployments = await api.agents.deployments(agentId);
            return [agentId, deployments] as const;
          } catch {
            return [agentId, []] as const;
          }
        }),
      );
      return Object.fromEntries(entries);
    },
    enabled: tab === "my" && myAgentIds.length > 0,
  });


  const { data: subscriptions } = useQuery({
    queryKey: ["agents", "subscriptions"],
    queryFn: () => api.agents.subscriptions(),
    enabled: privateApiEnabled,
  });


  const editAgentId = searchParams.get("edit");

  const deleteIsUnsubscribeRef = React.useRef(false);
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.agents.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      if (deleteIsUnsubscribeRef.current) {
        toast.success(t("page.agents.unsubscribed"));
        deleteIsUnsubscribeRef.current = false;
      } else {
        toast.success(t("page.agents.agent_deleted"));
      }
    },
  });

  const clearEditParam = () => {
    if (!searchParams.has("edit")) return;
    const next = new URLSearchParams(searchParams);
    next.delete("edit");
    setSearchParams(next, { replace: true });
  };

  React.useEffect(() => {
    if (!editAgentId) {
      closingEditAgentIdRef.current = null;
      return;
    }
    if (closingEditAgentIdRef.current === editAgentId) return;
    if (!myAgents) return;
    const target = myAgents.find((agent: any) => agent.id === editAgentId);
    if (target) {
      openAgentEditModal(editAgentId);
    } else if (!myLoading) {
      clearEditParam();
    }
  }, [editAgentId, myAgents, myLoading]);

  // The old closeAgentModal() used to clear the ?edit= URL param when the
  // modal closed (so it wouldn't immediately reopen). Now that the modal's
  // close logic lives in AgentEditModal.tsx and only touches the store,
  // watch the store here to clear the param when it closes.
  //
  // The store's "closed" state (agentId === undefined) is also its initial
  // state, so it's indistinguishable from "never opened yet" — without the
  // prevAgentEditModalOpenAgentIdRef check below, this effect would fire on
  // the very first render of a `?edit=<id>` deep link (before the effect
  // above has had a chance to call openAgentEditModal) and immediately wipe
  // the query param, so the modal never opens. Only clear on a genuine
  // open -> closed transition.
  const agentEditModalOpenAgentId = useAgentEditModalStore((s) => s.agentId);
  const prevAgentEditModalOpenAgentIdRef = useRef(agentEditModalOpenAgentId);
  React.useEffect(() => {
    const prevAgentId = prevAgentEditModalOpenAgentIdRef.current;
    prevAgentEditModalOpenAgentIdRef.current = agentEditModalOpenAgentId;
    if (prevAgentId !== undefined && agentEditModalOpenAgentId === undefined && editAgentId) {
      closingEditAgentIdRef.current = editAgentId;
      clearEditParam();
    }
  }, [agentEditModalOpenAgentId, editAgentId]);


  // Unsubscribe = delete the local copy
  const handleUnsubscribe = (localAgentId: string) => {
    deleteIsUnsubscribeRef.current = true;
    deleteMutation.mutate(localAgentId);
  };


  const savePrompt = async () => {
    if (!promptAgent) return;
    setSavingPrompt(true);
    try {
      await api.agents.update(promptAgent.id as string, {
        system_prompt: editPromptText,
      });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      toast.success(t("page.agents.prompt_saved"));
      setPromptModal(false);
    } catch {
      // error toast from api client
    } finally {
      setSavingPrompt(false);
    }
  };


  const tabs = [
    { key: "my", label: t("page.agents.my_agents"), count: myAgents?.length },
  ];
  const visibleMyAgents = ((myAgents || []) as any[]).filter((a: any) => {
    if (!mySearch.trim()) return true;
    const q = mySearch.toLowerCase();
    return (
      (a.name || "").toLowerCase().includes(q) ||
      getAgentDescription(a).toLowerCase().includes(q)
    );
  });
  let pageSubtitle = t("page.agents.manage_your_ai_agents");
  let emptyMyAgentsDescription = t("page.agents.no_my_agents_desc_oss");

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        padding: 0,
        overflow: "hidden",
        position: "relative",
        zIndex: 10,
      }}
    >
      {/* Header */}
      <PageHeader
        title={t("nav.agents")}
        subtitle={pageSubtitle}
        tabs={(
          <TabSwitcher
            tabs={tabs}
            value={tab}
            onChange={(k) => setTab(k as Tab)}
            wrap
            className="agents-view-tabs"
          />
        )}
        toolbar={tab === "my" ? (
          <SmartToolbar
            searchValue={mySearch}
            onSearchChange={setMySearch}
            searchPlaceholder={t("page.agents.search_agents")}
            className="w-full sm:w-64"
          />
        ) : undefined}
        actions={tab === "my" ? (
          <Dropdown
            align="right"
            trigger={<PageHeaderAddButton label={t("page.agents.add_agent")} caret className="agents-add-button" />}
            items={[
              { key: "create", label: t("page.agents.create_agent"), icon: <IconPlus size={14} /> },
              { key: "import", label: t("page.agents.import_agents"), icon: <IconUpload size={14} /> },
            ]}
            onSelect={(key) => {
              if (key === "create") openAgentEditModal();
              if (key === "import") setShowImportModal(true);
            }}
          />
        ) : undefined}
      />

      {/* ═══ MY AGENTS TAB ═══ */}
      {tab === "my" && (
        <div style={{ flex: 1, overflowY: "auto", padding: 0 }}>
          {myLoading ? (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))",
                gap: "24px",
                padding: 0,
              }}
            >
              <SkeletonCard />
              <SkeletonCard />
              <SkeletonCard />
            </div>
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))",
                gap: "24px",
                alignItems: "start",
              }}
            >
              {visibleMyAgents.length === 0 ? (
                <div
                  style={{
                    gridColumn: "1 / -1",
                    minHeight: 220,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    padding: 24,
                  }}
                >
                  <EmptyState
                    icon={<IconAgent size={32} style={{ color: "#d6d3d1" }} />}
                    title={
                      mySearch.trim()
                        ? t("page.agents.no_matching_agents")
                        : t("page.agents.no_my_agents")
                    }
                    description={
                      mySearch.trim()
                        ? t("page.agents.no_matching_agents_desc")
                        : emptyMyAgentsDescription
                    }
                  />
                </div>
              ) : (
                visibleMyAgents.map((agent: any) => {
                  const tags = parseTags(agent.tags);
                  const isHired =
                    false;
                  const description = displayAgentDescription(agent);
                  const connectionInfo = agentConnectionInfo(agent);
                  const workspaceItems = agentWorkspaceItems(
                    agentDeploymentsById?.[agent.id] || [],
                  );
                  return (
                    <CompactCard
                    key={agent.id}
                    icon={
                      <AgentAvatar
                        name={agent.name}
                        avatarUrl={isHired ? undefined : agent.avatar_url}
                        seed={agent.id || agent.category}
                        size={34}
                      />
                    }
                    title={agent.name}
                    subtitle={
                      description ||
                      (agent.category
                        ? displayAgentCategory(agent.category)
                        : connectionInfo.label)
                    }
                    meta={
                      <span
                        title={connectionInfo.label}
                        style={{
                          width: 7,
                          height: 7,
                          borderRadius: "50%",
                          background: "currentColor",
                        }}
                      />
                    }
                    metaTone={agent.status === "active" ? "connected" : "muted"}
                    onClick={() =>
                      openDetail({
                        icon: (
                          <AgentAvatar
                            name={agent.name}
                            avatarUrl={isHired ? undefined : agent.avatar_url}
                            seed={agent.id || agent.category}
                            size={48}
                          />
                        ),
                        title: agent.name,
                        subtitle: `${
                          isHired
                            ? t("page.agents.hired")
                            : t("page.agent_detail.custom")
                        } · ${connectionInfo.label}`,
                        badges: (
                          <>
                            <StatusBadge
                              type={agent.status === "active" ? "active" : "inactive"}
                              dot
                              pulse={agent.status === "active"}
                            >
                              {agent.status === "active"
                                ? t("page.agents.live")
                                : t("page.agents.off")}
                            </StatusBadge>
                            {tags.slice(0, 4).map((tag, i) => (
                              <Chip key={`${agent.id}-d-${tag}-${i}`} variant="slate" size="sm">
                                {displayAgentTag(tag)}
                              </Chip>
                            ))}
                          </>
                        ),
                        body: (
                          <>
                            <p style={{ margin: 0, color: "#44403c" }}>{description}</p>
                            <div
                              style={{
                                fontSize: 10,
                                fontWeight: 700,
                                color: "#a8a29e",
                                textTransform: "uppercase",
                                letterSpacing: "0.06em",
                                margin: "16px 0 8px",
                              }}
                            >
                              Workspaces
                            </div>
                            {workspaceItems.length > 0 ? (
                              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                                {workspaceItems.map((workspace) => (
                                  <span
                                    key={workspace.id}
                                    style={{
                                      display: "inline-flex",
                                      alignItems: "center",
                                      gap: 5,
                                      padding: "3px 9px",
                                      borderRadius: 8,
                                      background: "#f5f5f4",
                                      color: "#57534e",
                                      fontSize: 11,
                                      fontWeight: 600,
                                    }}
                                  >
                                    <span
                                      style={{
                                        width: 5,
                                        height: 5,
                                        borderRadius: "50%",
                                        background:
                                          workspace.status === "active"
                                            ? "#4f9c84"
                                            : "#d6d3d1",
                                      }}
                                    />
                                    {workspace.name}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              <div style={{ fontSize: 12, color: "#a8a29e", fontWeight: 600 }}>
                                Not assigned to a workspace
                              </div>
                            )}
                          </>
                        ),
                        primaryAction: {
                          label: "Manage",
                          onClick: () => {
                            closeDetail();
                            navigate(`/agents/${agent.id}`);
                          },
                        },
                        secondaryActions: [
                          {
                            label: t("action.edit"),
                            icon: <IconEdit size={16} />,
                            onClick: () => {
                              closeDetail();
                              openAgentEditModal(agent.id);
                            },
                          },
                        ],
                        dangerAction: {
                          label: isHired
                            ? t("page.agents.unsubscribe")
                            : t("action.delete"),
                          icon: <IconTrash size={16} />,
                          onClick: () => {
                            closeDetail();
                            setDeleteTarget(agent.id);
                          },
                        },
                      })
                    }
                  />
                  );
                })
              )}
            </div>
          )}
        </div>
      )}


      {/* ═══ Edit Prompt Modal (hired agents) ═══ */}
      <Modal
        open={promptModal}
        onClose={() => {
          setPromptModal(false);
          setPromptAgent(null);
        }}
        title={`${t("page.agents.edit_prompt")} — ${(promptAgent?.name as string) || ""}`}
        maxWidth="40rem"
        footer={
          <>
            <Button variant="outline" onClick={() => setPromptModal(false)}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={savePrompt}
              disabled={savingPrompt}
            >
              {savingPrompt ? t("page.agents.saving") : t("page.agents.save_prompt")}
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          {/* Variables bar */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              flexWrap: "wrap",
              gap: 6,
              padding: "8px 12px",
              background: "#fafaf9",
              borderRadius: 10,
              border: "1px solid rgba(28,25,23,0.06)",
            }}
          >
            <span
              style={{
                fontSize: 11,
                fontWeight: 700,
                color: "#78716c",
                textTransform: "uppercase",
                letterSpacing: "0.08em",
              }}
            >
              {t("page.agents.insert")}
            </span>
            {PROMPT_VARIABLES.map((v) => (
              <button
                key={v}
                onClick={() =>
                  insertVariable(
                    v,
                    promptTextareaRef,
                    editPromptText,
                    setEditPromptText,
                  )
                }
                style={{
                  padding: "3px 10px",
                  background: "#fff",
                  color: "#44403c",
                  fontSize: 11,
                  fontWeight: 700,
                  borderRadius: 6,
                  cursor: "pointer",
                  border: "1px solid rgba(28,25,23,0.06)",
                  fontFamily: "monospace",
                }}
              >
                {v}
              </button>
            ))}
          </div>

          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              onClick={() =>
                improvePrompt(
                  editPromptText,
                  (promptAgent?.name as string) || "",
                  (promptAgent?.description as string) || "",
                  setEditPromptText,
                  setImprovingPrompt,
                )
              }
              disabled={improvingPrompt || !editPromptText.trim()}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                fontSize: 11,
                color:
                  improvingPrompt || !editPromptText.trim()
                    ? "#a8a29e"
                    : "#57534e",
                background: "none",
                border: "none",
                cursor:
                  improvingPrompt || !editPromptText.trim()
                    ? "not-allowed"
                    : "pointer",
                fontWeight: 600,
              }}
            >
              {improvingPrompt ? <LoadingSpinner size={10} /> : "✦"}
              {improvingPrompt ? t("page.agents.improving_2") : t("page.agents.improve_with_ai")}
            </button>
          </div>

          <textarea
            ref={promptTextareaRef}
            value={editPromptText}
            onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
              setEditPromptText(e.target.value)
            }
            rows={9}
            placeholder={t("page.agents.enter_system_prompt")}
            className="manor-input"
            style={{
              fontFamily: "monospace",
              fontSize: 12.5,
              lineHeight: 1.6,
              resize: "vertical",
            }}
          />

          {/* View original prompt collapsible */}
          {!!promptAgent?.system_prompt && (
            <div
              style={{
                border: "1px solid #f5f5f4",
                borderRadius: 10,
                overflow: "hidden",
              }}
            >
              <div
                onClick={() => setShowOriginalPrompt(!showOriginalPrompt)}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "10px 14px",
                  background: "#fafaf9",
                  cursor: "pointer",
                  fontSize: 12,
                  fontWeight: 700,
                  color: "#78716c",
                }}
              >
                <span>{t("page.agents.view_original_template_prompt")}</span>
                <span>{showOriginalPrompt ? "▲" : "▼"}</span>
              </div>
              {showOriginalPrompt && (
                <div style={{ padding: "12px 14px", background: "#fff" }}>
                  <pre
                    style={{
                      fontFamily: "monospace",
                      fontSize: 11.5,
                      color: "#78716c",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                      margin: 0,
                      lineHeight: 1.6,
                    }}
                  >
                    {promptAgent.system_prompt as string}
                  </pre>
                </div>
              )}
            </div>
          )}

          <TestPromptPanel
            systemPrompt={editPromptText}
            message={promptTestMsg}
            onMessageChange={setPromptTestMsg}
            response={promptTestResp}
            loading={promptTestLoading}
            onRun={() =>
              runTest(
                editPromptText,
                promptTestMsg,
                setPromptTestResp,
                setPromptTestLoading,
              )
            }
          />
        </div>
      </Modal>

      {/* ═══ Delete Confirmation ═══ */}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => {
          if (deleteTarget) {
            const agent = (myAgents || []).find(
              (a: any) => a.id === deleteTarget,
            );
            deleteIsUnsubscribeRef.current =
              false;
            deleteMutation.mutate(deleteTarget);
            setDeleteTarget(null);
          }
        }}
        title={(() => {
          const agent = (myAgents || []).find(
            (a: any) => a.id === deleteTarget,
          );
          return t("page.agent_detail.delete_agent");
        })()}
        message={(() => {
          const agent = (myAgents || []).find(
            (a: any) => a.id === deleteTarget,
          );
          return t("page.agent_detail.are_you_sure_this_cannot_be_undone");
        })()}
        confirmLabel={(() => {
          const agent = (myAgents || []).find(
            (a: any) => a.id === deleteTarget,
          );
          return t("action.delete");
        })()}
        danger
      />

      <ImportAgentsDialog
        open={showImportModal}
        onClose={() => setShowImportModal(false)}
        onImported={() => {
          queryClient.invalidateQueries({ queryKey: ["agents"] });
          setTab("my");
        }}
      />

    </div>
  );
}

/* ── Helper: action button style ── */
function actionBtnStyle(
  isHovered: boolean,
  accentColor: string,
  isDanger = false,
): React.CSSProperties {
  const borderColor = isHovered
    ? isDanger
      ? "#ddafac"
      : accentColor
    : "#f5f5f4";
  const color = isHovered ? (isDanger ? "#d65f59" : accentColor) : "#a8a29e";
  const shadow = isHovered
    ? isDanger
      ? "0 0 0 3px rgba(214,95,89,0.15)"
      : `0 0 0 3px rgba(79,125,117,0.2)`
    : "none";
  return {
    width: 34,
    height: 34,
    borderRadius: "50%",
    background: isHovered ? "#fff" : "#fafaf9",
    border: `1px solid ${borderColor}`,
    color,
    fontSize: 14,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    cursor: "pointer",
    transition: "all 0.2s",
    flexShrink: 0,
    boxShadow: shadow,
    transform: isHovered ? "scale(1.08)" : "none",
  };
}
