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
import { AiBuildConversation } from "../components/ai/AiBuildConversation";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import { SkeletonCard } from "../components/ui/Skeleton";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Card from "../components/ui/Card";
import Chip from "../components/ui/Chip";
import SmartToolbar from "../components/ui/SmartToolbar";
import Input from "../components/ui/Input";
import Select from "../components/ui/Select";
import StatusBadge from "../components/ui/StatusBadge";
import SharedAgentAvatar from "../components/ui/AgentAvatar";
import CompactCard from "../components/ui/CompactCard";
import { openDetail, closeDetail } from "../stores/detail";
import Toggle from "../components/ui/Toggle";
import { t } from "../lib/i18n";
import { getAgentDescription } from "../lib/localizedContent";
import { getSkillDescription } from "./skills/skillTypes";
import { formatUserFacingLabel, formatUserFacingText } from "../lib/taskDisplay";
import type { Agent } from "../lib/types";
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
type AgentRuntimeProfile =
  | "hosted"
  | "https";

const RUNTIME_PROFILE_OPTIONS: Array<{
  key: AgentRuntimeProfile;
  title: string;
  body: string;
  badge: string;
}> = [
  {
    key: "hosted",
    title: "Manor Hosted",
    body: "Runs on Manor's hosted agent service. No extra connection required.",
    badge: "Default",
  },
  {
    key: "https",
    title: "HTTPS endpoint",
    body: "Runs through a workspace-bound HTTPS agent endpoint.",
    badge: "Remote",
  },
];

const CATEGORIES = [
  { value: "All", label: t("page.workspaces.filter_all") },
  { value: "Essential", label: t("page.agents.category_essential") },
  { value: "Growth", label: t("page.agents.category_growth") },
  { value: "Specialist", label: t("page.agents.category_specialist") },
  { value: "Property Management", label: t("page.agents.property_management") },
  { value: "Customer Service", label: t("page.agents.category_customer_service") },
];


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
  const runtimeLearning = { ...objectConfig(next.runtime_learning), enabled };
  next.runtime_learning = runtimeLearning;
  return next;
}

function runtimeProfileFromConfig(config: unknown): AgentRuntimeProfile {
  const connection = objectConfig(objectConfig(config).runtime_connection);
  const source = String(connection.source || "").toLowerCase();
  const tool = String(connection.tool || "").toLowerCase();
  if (source === "https") return "https";
  return "hosted";
}

function runtimeConnectionForProfile(
  profile: AgentRuntimeProfile,
  config: unknown,
): Record<string, unknown> {
  const existing = objectConfig(objectConfig(config).runtime_connection);
  if (profile === "https") {
    return { ...existing, source: "https" };
  }
  return { source: "manor_hosted" };
}

function mergeAgentConfig(
  config: unknown,
  runtimeLearningEnabled: boolean,
  runtimeProfile: AgentRuntimeProfile,
): Record<string, any> {
  const next = mergeRuntimeLearningConfig(config, runtimeLearningEnabled);
  next.runtime_connection = runtimeConnectionForProfile(runtimeProfile, config);
  return next;
}

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

function parseTags(tags: string | string[] | undefined): string[] {
  if (!tags) return [];
  if (Array.isArray(tags)) return tags;
  return String(tags)
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

function displayAgentDescription(agent: any): string {
  const text = formatUserFacingText(getAgentDescription(agent));
  const genericMatch = text.match(/^General agent for ['"]?(.+?)['"]? capability\.?$/i);
  if (genericMatch?.[1]) return `Handles ${genericMatch[1]} work.`;
  return text;
}

function displayAgentCategory(category?: string | null): string {
  const raw = String(category || "").trim();
  if (!raw) return t("page.workspace_detail.agent");
  if (raw.toLowerCase() === "mcp") return "Connector";
  if (raw.toLowerCase() === "builtin") return "Built-in";
  if (raw.toLowerCase() === "builtinsystem") return "Built-in";
  return formatUserFacingLabel(raw) || t("page.workspace_detail.agent");
}

function displayAgentTag(tag?: string | null): string {
  const raw = String(tag || "").trim();
  if (!raw) return "";
  if (raw === "auto_created") return "Workspace generated";
  return formatUserFacingLabel(raw);
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


function GeneratedAgentReviewCard({ agent }: { agent: Agent }) {
  const tags = parseTags(agent.tags).map(displayAgentTag).filter(Boolean);
  const description = formatUserFacingText(agent.description || "");
  const systemPrompt = String(agent.system_prompt || "").trim();
  return (
    <div style={{ display: "grid", gap: 13 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <AgentAvatar
          name={agent.name || t("page.workspace_detail.agent")}
          seed={agent.id || agent.category || agent.description}
          size={46}
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

function displayToolName(tool: any): string {
  return formatUserFacingText(formatUserFacingLabel(tool?.display_name || tool?.name || ""))
    .replace(/^MCP\s+/i, "")
    .replace(/\bMCP\b/g, "Connector")
    .trim();
}

function displayToolDescription(tool: any): string {
  const raw = String(tool?.description || "").trim();
  if (!raw) return "";
  const normalized = formatUserFacingText(raw)
    .replace(/\[MCP:[^\]]+\]\s*/gi, "")
    .replace(/\bSystem tool:\s*/gi, "Built-in tool: ")
    .replace(/\bmcp__([a-z0-9_]+)__([a-z0-9_]+)/gi, (_match, server, action) => `${formatUserFacingLabel(server)} ${formatUserFacingLabel(action)}`)
    .replace(/\s+/g, " ")
    .trim();
  if (/^Built-in tool:\s*$/i.test(normalized)) return "Built-in tool available to this agent.";
  return normalized;
}

function parseMcpToolName(name?: string | null): { serverKey: string; actionKey: string } | null {
  const raw = String(name || "").trim();
  if (!raw.startsWith("mcp__")) return null;
  const parts = raw.split("__");
  if (parts.length < 3) return null;
  const serverKey = parts[1]?.trim();
  const actionKey = parts.slice(2).join("__").trim();
  if (!serverKey || !actionKey) return null;
  return { serverKey, actionKey };
}

function mcpToolActionLabel(tool: any): string {
  const parsed = parseMcpToolName(tool?.name);
  const raw = parsed?.actionKey || tool?.display_name || tool?.name || "";
  return formatUserFacingLabel(raw);
}

function mcpProviderLabel(server: any): string {
  return formatUserFacingLabel(server?.name || server?.server_key || "MCP");
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

/* ─── Test Prompt Panel ─── */
function TestPromptPanel({
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

export default function Agents() {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const promptTextareaRef = useRef<HTMLTextAreaElement>(null);
  const avatarFileInputRef = useRef<HTMLInputElement>(null);
  const closingEditAgentIdRef = useRef<string | null>(null);

  const [tab, setTab] = useState<Tab>("my");
  const [showModal, setShowModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  // "ai" = conversational Build/Edit with AI; "manual" = the full form.
  const [agentMode, setAgentMode] = useState<"ai" | "manual">("ai");
  const [editingAgent, setEditingAgent] = useState<Record<
    string,
    unknown
  > | null>(null);
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
  const editAgentId = searchParams.get("edit");

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
      resetForm();
      setShowModal(false);
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
      const agentId = editingAgent?.id as string;
      if (agentId) {
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
            await api.skills.bindSkill(agentId, skillId);
          } catch {
            /* non-fatal */
          }
        }
        for (const skillId of toDetachSkills) {
          try {
            await api.skills.unbindSkill(agentId, skillId);
          } catch {
            /* non-fatal */
          }
        }
        if (toAttachTools.length > 0) {
          try {
            await api.agents.bindTools(agentId, toAttachTools);
          } catch {
            /* non-fatal */
          }
        }
        if (toDetachTools.length > 0) {
          try {
            await api.agents.unbindTools(agentId, toDetachTools);
          } catch {
            /* non-fatal */
          }
        }
      }
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      closeAgentModal();
      toast.success(t("page.agents.agent_updated"));
    },
  });

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

  const resetForm = () => {
    setFormName("");
    setFormDesc("");
    setFormPrompt("");
    setFormAvatarUrl("");
    setFormCategory("");
    setFormLearningEnabled(true);
    setFormRuntimeProfile("hosted");
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

  const clearEditParam = () => {
    if (!searchParams.has("edit")) return;
    const next = new URLSearchParams(searchParams);
    next.delete("edit");
    setSearchParams(next, { replace: true });
  };

  const closeAgentModal = () => {
    if (editAgentId) {
      closingEditAgentIdRef.current = editAgentId;
    }
    clearEditParam();
    setShowModal(false);
    setEditingAgent(null);
    resetForm();
  };

  const openEdit = async (agent: Record<string, unknown>) => {
    setEditingAgent(agent);
    setAgentMode("manual");
    setFormName((agent.name as string) || "");
    setFormDesc(formatUserFacingText((agent.description as string) || ""));
    setFormPrompt((agent.system_prompt as string) || "");
    setFormAvatarUrl((agent.avatar_url as string) || "");
    setFormCategory((agent.category as string) || "");
    setFormLearningEnabled(runtimeLearningEnabled(agent.config));
    setFormRuntimeProfile(runtimeProfileFromConfig(agent.config));
    const tags = Array.isArray(agent.tags)
      ? (agent.tags as string[]).map(displayAgentTag).filter(Boolean).join(", ")
      : (agent.tags as string) || "";
    setFormTags(formatUserFacingText(tags));
    setFormTestMsg("");
    setFormTestResp("");
    // Load current agent skills/tools
    try {
      const [boundSkills, availableSkills, boundTools] = await Promise.all([
        api.skills.listAgentBindings(agent.id as string),
        api.skills.listAgentAvailable(agent.id as string),
        api.agents.getTools(agent.id as string),
      ]);
      const seen = new Set<string>();
      const all: any[] = [];
      for (const s of [...(boundSkills || []), ...(availableSkills || [])]) {
        if (!seen.has(s.id)) {
          seen.add(s.id);
          all.push(s);
        }
      }
      const boundSkillIds = (boundSkills || []).map((s: any) => s.id);
      setOrigSkillIds(boundSkillIds);
      setEditSkillIds([...boundSkillIds]);

      const boundToolIds = (boundTools || []).map((t: any) => t.id);
      setOrigToolIds(boundToolIds);
      setEditToolIds([...boundToolIds]);
    } catch {
      setOrigSkillIds([]);
      setEditSkillIds([]);
      setOrigToolIds([]);
      setEditToolIds([]);
    }
    setShowModal(true);
  };

  React.useEffect(() => {
    if (!editAgentId) {
      closingEditAgentIdRef.current = null;
      return;
    }
    if (closingEditAgentIdRef.current === editAgentId) return;
    if (showModal || editingAgent || !myAgents) return;
    const target = myAgents.find((agent: any) => agent.id === editAgentId);
    if (target) {
      void openEdit(target as unknown as Record<string, unknown>);
    } else if (!myLoading) {
      clearEditParam();
    }
  }, [editAgentId, showModal, editingAgent, myAgents, myLoading]);

  // "Add agent" opens the modal defaulting to the AI ✨ tab; the Manual tab
  // is right there.
  const openCreate = () => {
    resetForm();
    setEditingAgent(null);
    clearEditParam();
    setAgentMode("ai");
    setShowModal(true);
  };

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
      config: mergeAgentConfig(
        editingAgent?.config,
        formLearningEnabled,
        formRuntimeProfile,
      ),
    };
    if (editingAgent) {
      updateMutation.mutate({ id: editingAgent.id as string, data: payload });
    } else {
      createMutation.mutate(payload);
    }
  };


  // Unsubscribe = delete the local copy
  const handleUnsubscribe = (localAgentId: string) => {
    deleteIsUnsubscribeRef.current = true;
    deleteMutation.mutate(localAgentId);
  };


  // Improve prompt with AI
  const improvePrompt = async (
    currentPrompt: string,
    agentName: string,
    description: string,
    onResult: (improved: string) => void,
    setLoading: (v: boolean) => void,
  ) => {
    if (!currentPrompt.trim()) return;
    setLoading(true);
    try {
      const meta = `${t("page.agents.improve_prompt_meta_agent").replace("{name}", agentName)}${description ? `\n${t("page.agents.improve_prompt_meta_description").replace("{description}", description)}` : ""}`;
      const systemPrompt = t("page.agents.improve_prompt_system_prompt")
        .replace("{agentName}", agentName)
        .replace("{meta}", meta)
        .replace("{currentPrompt}", currentPrompt);
      const res = await api.agents.previewPrompt(
        systemPrompt,
        t("page.agents.improve_prompt_user_message"),
      );
      let improved = (res.response || "").trim();
      improved = improved.replace(/^[\s\S]*?---\s*\n/m, "").trim();
      improved = improved
        .replace(/^```[\s\S]*?\n/, "")
        .replace(/\n```\s*$/, "")
        .trim();
      improved = improved.replace(/^\*\*Improved .*?\*\*:?\s*/i, "").trim();
      improved = improved
        .replace(/^Here'?s the improved prompt.*?:\s*/i, "")
        .trim();
      if (improved && improved.length > 20) {
        onResult(improved);
        toast.success(t("page.agents.prompt_improved"));
      } else {
        toast.error(t("page.agents.could_not_improve_try_adding_more_details_first"));
      }
    } catch {
      toast.error(t("page.agents.failed_to_improve_prompt"));
    } finally {
      setLoading(false);
    }
  };

  const runTest = async (
    systemPrompt: string,
    testMsg: string,
    setResp: (v: string) => void,
    setLoading: (v: boolean) => void,
  ) => {
    if (!testMsg.trim() || !systemPrompt.trim()) return;
    setLoading(true);
    setResp("");
    try {
      const res = await api.agents.previewPrompt(systemPrompt, testMsg);
      setResp(res.response || "");
    } catch {
      setResp(t("page.agents.request_failed"));
    } finally {
      setLoading(false);
    }
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
  const PROMPT_VARIABLES = [
    "{{agentName}}",
    "{{clientName}}",
    "{{entityName}}",
  ];
  let pageSubtitle = t("page.agents.manage_your_ai_agents");
  let emptyMyAgentsDescription = t("page.agents.no_my_agents_desc_oss");
  const formAvatarSeed = editingAgent
    ? String(editingAgent.id || formCategory || formName)
    : `${formName}::${formDesc}::${formCategory}`;

  const insertVariable = (
    v: string,
    ref: React.RefObject<HTMLTextAreaElement>,
    text: string,
    setText: (t: string) => void,
  ) => {
    const el = ref.current;
    if (el) {
      const start = el.selectionStart;
      const end = el.selectionEnd;
      const newText = text.slice(0, start) + v + text.slice(end);
      setText(newText);
      setTimeout(() => {
        el.selectionStart = el.selectionEnd = start + v.length;
        el.focus();
      }, 0);
    } else {
      setText(text + v);
    }
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        padding: "1rem",
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
            trigger={<PageHeaderAddButton label={t("page.agents.add_agent")} caret />}
            items={[
              { key: "create", label: t("page.agents.create_agent"), icon: <IconPlus size={14} /> },
              { key: "import", label: t("page.agents.import_agents"), icon: <IconUpload size={14} /> },
            ]}
            onSelect={(key) => {
              if (key === "create") openCreate();
              if (key === "import") setShowImportModal(true);
            }}
          />
        ) : undefined}
      />

      {/* ═══ MY AGENTS TAB ═══ */}
      {tab === "my" && (
        <div style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
          {myLoading ? (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))",
                gap: "24px",
                padding: "8px",
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
                  const connectionVariant: "teal" | "purple" | "blue" =
                    connectionInfo.label === "HTTPS"
                      ? "purple"
                      : connectionInfo.label.includes("CLI")
                        ? "blue"
                        : "teal";
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
                              openEdit(
                                agent as unknown as Record<string, unknown>,
                              );
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


      {/* ═══ Create/Edit Modal ═══ */}
      <Modal
        open={showModal}
        onClose={closeAgentModal}
        title={editingAgent ? t("page.agents.edit_agent") : t("page.agents.create_agent")}
        maxWidth={agentMode === "manual" ? "42rem" : "36rem"}
        footer={
          agentMode === "ai" ? undefined : (
            <>
              <Button
                variant="outline"
                onClick={closeAgentModal}
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
                closeAgentModal();
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
                <AgentAvatar
                  name={formName || t("page.skills.agent")}
                  avatarUrl={formAvatarUrl}
                  seed={formAvatarSeed}
                  size={40}
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
            <div className="mb-3 flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="text-[13px] font-semibold text-stone-900">
                  Run method
                </div>
                <p className="mt-1 text-xs leading-5 text-stone-500">
                  This describes where the agent runs. Workspace service
                  bindings
                  and HTTPS endpoints are configured from
                  Workspace &gt; Agents.
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
