import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type GovernancePresetKey } from "../lib/api";
import { useToastStore } from "../stores/toast";
import type { Workspace, WorkspaceStaff } from "../lib/types";
import { useAuthStore } from "../stores/auth";
import { useIsCollaborator } from "../lib/collaboratorSession";
import { canManageWorkspace } from "../lib/permissions";
import BlueprintUpgradeDialog from "../components/blueprints/BlueprintUpgradeDialog";
import BlueprintAgentSelection, { blueprintAgentSubscriptions } from "../components/blueprints/BlueprintAgentSelection";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import ContextMenu from "../components/ui/ContextMenu";
import Dropdown from "../components/ui/Dropdown";
import WorkspaceAppCard, {
  WORKSPACE_APP_CARD_MIN_WIDTH,
  workspaceAppCardGridStyle,
} from "../components/ui/WorkspaceAppCard";
import SmartToolbar from "../components/ui/SmartToolbar";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Select from "../components/ui/Select";
import Textarea from "../components/ui/Textarea";
import Chip from "../components/ui/Chip";
import { CardGridSkeleton, ListRowsSkeleton } from "../components/ui/Skeleton";
import WorkspaceIconTile, { getWorkspacePresentation } from "../components/ui/WorkspaceIcon";
import {
  IconAgent,
  IconBuilding,
  IconChat,
  IconChecklist,
  IconDocument,
  IconEdit,
  IconInfo,
  IconLock,
  IconPlus,
  IconTrash,
  IconUpload,
  IconWorkspace,
} from "../components/icons";
import { t } from "../lib/i18n";
import { formatDate } from "../lib/format";
import {
  formatBlueprintVariableValue,
  parseBlueprintVariableValue,
} from "../lib/blueprintVariables.mjs";
import { isBlueprintPackageName, readBlueprintPackagePayload } from "../lib/blueprintPackage";
import { resolveBlueprintChannelSelections } from "../lib/blueprintSetup.mjs";
import { userAvatarImage } from "../lib/userAvatar";
import { workspaceChatActionCount } from "../lib/workspaceActionCount";

interface WorkspaceForm {
  name: string;
  description: string;
  category: string;
}

type WorkspaceView =
  | "workspaces"
  ;

const emptyForm: WorkspaceForm = { name: "", description: "", category: "" };
const WORKSPACE_VIEW_KEYS: WorkspaceView[] = [
  "workspaces",
];
const WORKSPACE_INTRO_MOTION_URL = "/assets/workspace/workspace-intro-original.mp4";
const WORKSPACE_INTRO_DARK_MOTION_URL = "/assets/workspace/workspace-intro-dark.mp4";
const WORKSPACE_INTRO_PLAYBACK_RATE = 0.65;

function currentWorkspaceIntroMotionUrl() {
  if (typeof document === "undefined") return WORKSPACE_INTRO_MOTION_URL;
  return document.documentElement.dataset.theme === "dark"
    ? WORKSPACE_INTRO_DARK_MOTION_URL
    : WORKSPACE_INTRO_MOTION_URL;
}

function WorkspaceIntroPanel() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [motionUrl, setMotionUrl] = useState(currentWorkspaceIntroMotionUrl);
  const points = [
    { label: t("page.workspaces.intro_point_context"), Icon: IconDocument },
    { label: t("page.workspaces.intro_point_agents"), Icon: IconAgent },
    { label: t("page.workspaces.intro_point_review"), Icon: IconChecklist },
  ];

  useEffect(() => {
    if (typeof document === "undefined") return undefined;

    const syncMotionUrl = () => setMotionUrl(currentWorkspaceIntroMotionUrl());
    const observer = new MutationObserver(syncMotionUrl);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    syncMotionUrl();

    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const applyPlaybackRate = () => {
      video.playbackRate = WORKSPACE_INTRO_PLAYBACK_RATE;
    };

    applyPlaybackRate();
    video.loop = true;
    video.addEventListener("loadedmetadata", applyPlaybackRate);

    return () => {
      video.removeEventListener("loadedmetadata", applyPlaybackRate);
    };
  }, [motionUrl]);

  return (
    <section className="workspace-intro-panel">
      <div className="workspace-intro-copy">
        <div className="workspace-intro-kicker">
          <IconWorkspace size={14} />
          {t("page.workspaces.intro_kicker")}
        </div>
        <h2 className="workspace-intro-title">{t("page.workspaces.intro_title")}</h2>
        <p className="workspace-intro-description">{t("page.workspaces.intro_description")}</p>
        <div className="workspace-intro-points">
          {points.map(({ label, Icon }) => (
            <span key={label} className="workspace-intro-point">
              <Icon size={13} />
              {label}
            </span>
          ))}
        </div>
      </div>
      <div className="workspace-intro-visual" aria-hidden="true">
        <video
          key={motionUrl}
          ref={videoRef}
          className="workspace-intro-video"
          autoPlay
          muted
          loop
          playsInline
          preload="auto"
          disablePictureInPicture
          controls={false}
        >
          <source src={motionUrl} type="video/mp4" />
        </video>
      </div>
    </section>
  );
}

function creatorInitial(workspace: Workspace): string {
  const source = workspace.created_by_name || workspace.created_by_email || "";
  const nameParts = source.trim().split(/\s+/).filter(Boolean);
  if (nameParts.length >= 2) return `${nameParts[0][0]}${nameParts[1][0]}`.toUpperCase();
  const first = source.includes("@") ? source.split("@")[0]?.[0] : source[0];
  return (first || "?").toUpperCase();
}

function WorkspaceCreatorSummary({ workspace }: { workspace: Workspace }) {
  const creatorName = workspace.created_by_name || workspace.created_by_email || t("common.unknown");
  const creatorEmail = workspace.created_by_name && workspace.created_by_email ? workspace.created_by_email : "";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
      <div
        style={{
          width: 36,
          height: 36,
          borderRadius: 12,
          flex: "0 0 36px",
          overflow: "hidden",
          background: "linear-gradient(135deg, rgba(95,146,138,0.16), rgba(148,163,184,0.14))",
          color: "#4f7d75",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 12,
          fontWeight: 850,
        }}
      >
        {userAvatarImage(workspace.created_by_avatar_url) ? (
          <img
            src={userAvatarImage(workspace.created_by_avatar_url)}
            alt=""
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          creatorInitial(workspace)
        )}
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 10, fontWeight: 800, color: "var(--text-muted)", marginBottom: 3 }}>
          {t("page.workspace_detail.field_created_by")}
        </div>
        <div style={{ fontSize: 13, fontWeight: 760, color: "var(--text-strong)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {creatorName}
        </div>
        {creatorEmail && (
          <div style={{ fontSize: 12, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {creatorEmail}
          </div>
        )}
      </div>
    </div>
  );
}

function metadataStat(label: string, value: string) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 10, fontWeight: 800, color: "var(--text-muted)", marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-default)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {value}
      </div>
    </div>
  );
}

/**
 * Access summary inside the edit modal.
 *
 * Workspace access lives on the detail page (Configure → Staff → Share
 * workspace). That is three levels deep, so people editing a workspace here
 * had no idea who could open it. This surfaces the current scope in one line
 * and links to the real control — deliberately read-only, so the access mode
 * and member list keep a single source of truth.
 */
function WorkspaceAccessRow({
  workspace,
  staffRows,
  currentUser,
  onManage,
}: {
  workspace: Workspace;
  staffRows: WorkspaceStaff[] | undefined;
  currentUser: Parameters<typeof canManageWorkspace>[0];
  onManage: () => void;
}) {
  const entityVisible = (workspace.settings as any)?.access_mode === "entity_visible";
  const memberCount = (staffRows || []).filter(
    (row) => (row.status ?? "active") === "active",
  ).length;
  const canManage = canManageWorkspace(currentUser, staffRows || []);

  const hairline = "0.5px solid rgba(29, 29, 31,0.06)";
  const detail = entityVisible
    ? t("page.workspaces.access_entity_visible_hint")
    : memberCount === 1
      ? t("page.workspaces.access_member_hint_one")
      : memberCount > 1
        ? t("page.workspaces.access_members_hint").replace("{count}", String(memberCount))
        : t("page.workspaces.access_members_only_hint");

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 11,
        padding: "13px 2px",
        borderTop: hairline,
        borderBottom: hairline,
      }}
    >
      <span style={{ color: "var(--text-muted)", display: "flex", flex: "none" }} aria-hidden="true">
        {entityVisible ? <IconBuilding size={17} /> : <IconLock size={17} />}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--text-strong)" }}>
          {entityVisible
            ? t("page.workspaces.access_entity_visible")
            : t("page.workspaces.access_members_only")}
        </div>
        <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginTop: 2 }}>{detail}</div>
      </div>
      {canManage && (
        <button
          type="button"
          onClick={onManage}
          style={{
            fontSize: 12.5,
            fontWeight: 600,
            color: "var(--accent)",
            background: "transparent",
            border: "none",
            cursor: "pointer",
            padding: "4px 2px",
            flex: "none",
          }}
        >
          {t("page.workspaces.access_manage")}
        </button>
      )}
    </div>
  );
}

// The sidebar has a place for each view, so a bare /workspaces is always the
// Workspace list; the marketplace is only ever asked for by name.
function parseWorkspaceView(value: string | null, includeMarketplace = true): WorkspaceView {
  const fallbackView: WorkspaceView = "workspaces";
  const parsed = WORKSPACE_VIEW_KEYS.includes(value as WorkspaceView)
    ? (value as WorkspaceView)
    : fallbackView;
  return parsed;
}

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function readTextFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(t("page.workspaces.import_read_failed")));
    reader.readAsText(file);
  });
}

function blueprintPayloadFromJson(value: unknown): Record<string, any> | null {
  if (!isRecord(value)) return null;
  const candidate = isRecord(value.payload) ? value.payload : value;
  if (!isRecord(candidate)) return null;
  if (!isRecord(candidate.manifest) && !isRecord(candidate.recipe)) return null;
  return candidate;
}

function ImportWorkspaceDialog({
  open,
  onClose,
  onImported,
}: {
  open: boolean;
  onClose: () => void;
  onImported: (workspaceId: string) => void;
}) {
  const toast = useToastStore();
  const [fileName, setFileName] = useState("");
  const [payload, setPayload] = useState<Record<string, any> | null>(null);
  // A .manor package installs as the file itself, so its carried files come along.
  const [packageFile, setPackageFile] = useState<File | null>(null);
  const [workspaceName, setWorkspaceName] = useState("");
  const [mode, setMode] = useState<"live" | "simulate">("live");
  const [preset, setPreset] = useState<GovernancePresetKey>("automatic");
  const { data: presets } = useQuery({
    queryKey: ["governance-presets"],
    queryFn: () => api.blueprints.governancePresets(),
    enabled: open,
  });
  const [createMissingAgents, setCreateMissingAgents] = useState(true);
  const [selectedAgentIds, setSelectedAgentIds] = useState<Record<string, string>>({});
  const [variableValues, setVariableValues] = useState<Record<string, string>>({});
  const [channelConfigIds, setChannelConfigIds] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [importing, setImporting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setFileName("");
    setPayload(null);
    setPackageFile(null);
    setWorkspaceName("");
    setMode("live");
    setPreset("automatic");
    setCreateMissingAgents(true);
    setSelectedAgentIds({});
    setVariableValues({});
    setChannelConfigIds({});
    setError("");
    setImporting(false);
  }, [open]);

  const onFileChange = async (file?: File | null) => {
    setError("");
    setPayload(null);
    setPackageFile(null);
    setSelectedAgentIds({});
    setVariableValues({});
    setChannelConfigIds({});
    setFileName(file?.name || "");
    if (!file) return;
    try {
      const isPackage = isBlueprintPackageName(file.name);
      const parsed = isPackage ? await readBlueprintPackagePayload(file) : JSON.parse(await readTextFile(file));
      const nextPayload = blueprintPayloadFromJson(parsed);
      if (!nextPayload) {
        setError(t("page.workspaces.import_invalid_blueprint"));
        return;
      }
      setPayload(nextPayload);
      if (isPackage) setPackageFile(file);
      const manifest = isRecord(nextPayload.manifest) ? nextPayload.manifest : {};
      const title = typeof manifest.title === "string" ? manifest.title : "";
      if (title && !workspaceName) setWorkspaceName(title);
    } catch (err: any) {
      setError(err?.message || t("page.workspaces.import_invalid_json"));
    }
  };

  const installVariables = (payload?.contract && isRecord(payload.contract)
    && Array.isArray(payload.contract.variables)
    ? payload.contract.variables : [])
    .filter((item): item is Record<string, any> => isRecord(item) && typeof item.key === "string");
  const variableOverrides: Record<string, unknown> = {};
  const invalidVariableKeys = new Set<string>();
  for (const variable of installVariables) {
    const input = variableValues[variable.key] ?? "";
    if (!input.trim()) continue;
    const parsed = parseBlueprintVariableValue(input, variable.default);
    if (parsed.ok) variableOverrides[variable.key] = parsed.value;
    else invalidVariableKeys.add(variable.key);
  }
  const missingRequiredVariable = installVariables.some((variable) => (
    variable.required && !(variableValues[variable.key] ?? formatBlueprintVariableValue(variable.default)).trim()
  ));
  const serializedVariables = JSON.stringify(variableOverrides);
  const preflightQuery = useQuery({
    queryKey: ["install-payload-preflight", payload, mode, serializedVariables, channelConfigIds],
    queryFn: () => api.blueprints.installPayloadPreflight({
      payload: payload!, mode, variable_values: variableOverrides, channel_config_ids: channelConfigIds,
    }),
    enabled: open && !!payload && !missingRequiredVariable && invalidVariableKeys.size === 0,
    refetchOnMount: "always",
  });
  const resolvedChannelConfigIds = resolveBlueprintChannelSelections(
    preflightQuery.data?.requirements ?? [], channelConfigIds,
  );
  const hasLiveBlocker = mode === "live" && (preflightQuery.isPending || preflightQuery.isError
    || (preflightQuery.data?.requirements ?? []).some((requirement) => (
      requirement.blocking && !(requirement.kind === "channel" && requirement.resource_options.length
        ? requirement.ready && Boolean(requirement.requirement_key && resolvedChannelConfigIds[requirement.requirement_key])
        : requirement.ready)
    )));

  const doImport = async () => {
    if (!payload) {
      setError(t("page.workspaces.import_select_file_first"));
      return;
    }
    setImporting(true);
    setError("");
    try {
      const options = {
        mode,
        workspace_name: workspaceName.trim() || undefined,
        create_missing_agents: createMissingAgents,
        agent_ids_by_service_key: selectedAgentIds,
        governance_preset: preset,
        variable_values: variableOverrides,
        channel_config_ids: resolvedChannelConfigIds,
      };
      const result = packageFile
        ? await api.blueprints.installPackage(packageFile, options)
        : await api.blueprints.installPayload({ payload, ...options });
      toast.success(t("page.workspaces.imported_workspace"));
      onImported(result.workspace_id);
      onClose();
    } catch (err: any) {
      setError(err?.message || t("page.workspaces.import_failed"));
      toast.error(t("page.workspaces.import_failed"), err?.message);
    } finally {
      setImporting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={importing ? () => {} : onClose}
      title={t("page.workspaces.import_workspace")}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={importing}>{t("action.cancel")}</Button>
          <Button variant="primary" onClick={doImport} disabled={!payload || importing || missingRequiredVariable || invalidVariableKeys.size > 0 || hasLiveBlocker} loading={importing}>
            {importing ? t("page.workspaces.importing_workspace") : t("page.workspaces.import_workspace")}
          </Button>
        </>
      }
    >
      <div style={{ display: "grid", gap: 14 }}>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13, lineHeight: 1.55 }}>
          {t("page.workspaces.import_workspace_desc")}
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
            accept="application/json,.json,.manor"
            onChange={(e) => onFileChange(e.target.files?.[0])}
            style={{ display: "none" }}
          />
          <span style={{ display: "flex", alignItems: "center", gap: 8, color: "#436b65", fontSize: 13, fontWeight: 800 }}>
            <IconUpload size={15} />
            {fileName || t("page.workspaces.choose_blueprint_json")}
          </span>
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>
            {t("page.workspaces.choose_blueprint_json_hint")}
          </span>
        </label>
        <Input
          label={t("page.workspaces.import_workspace_name")}
          value={workspaceName}
          onChange={(e) => setWorkspaceName(e.target.value)}
          placeholder={t("page.workspaces.workspace_name")}
        />
        {payload && (
          <BlueprintAgentSelection
            open={open}
            subscriptions={blueprintAgentSubscriptions(payload)}
            selectedAgentIds={selectedAgentIds}
            onChange={setSelectedAgentIds}
          />
        )}
        {installVariables.map((variable) => (
          <Input
            key={variable.key}
            label={`${variable.label || variable.key}${variable.required ? " *" : ""}`}
            value={variableValues[variable.key] ?? ""}
            onChange={(event) => setVariableValues((current) => ({ ...current, [variable.key]: event.target.value }))}
            placeholder={formatBlueprintVariableValue(variable.default)}
            error={invalidVariableKeys.has(variable.key) ? t("page.workspaces.import_invalid_blueprint") : undefined}
          />
        ))}
        {preflightQuery.data?.requirements.filter((requirement) => requirement.kind === "channel" && requirement.resource_options.length > 1).map((requirement) => (
          <label key={requirement.requirement_key} style={{ display: "grid", gap: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-muted)" }}>{requirement.label}</span>
            <select
              value={requirement.requirement_key ? resolvedChannelConfigIds[requirement.requirement_key] || "" : ""}
              onChange={(event) => requirement.requirement_key && setChannelConfigIds((current) => ({ ...current, [requirement.requirement_key!]: event.target.value }))}
              style={{ height: 38, borderRadius: 10, border: "0.5px solid #e5e5ea", padding: "0 10px", background: "var(--surface-panel)", color: "var(--text-strong)" }}
            >
              <option value="">{t("page.workspace_detail.select_account")}</option>
              {requirement.resource_options.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
            </select>
          </label>
        ))}
        {(missingRequiredVariable || invalidVariableKeys.size > 0 || hasLiveBlocker) && payload && (
          <div style={{ color: "#b91c1c", fontSize: 12, lineHeight: 1.5 }}>
            {preflightQuery.data?.requirements.filter((requirement) => requirement.blocking && !requirement.ready).map((requirement) => requirement.reason).join(" ") || t("page.workspaces.import_invalid_blueprint")}
          </div>
        )}
        <label style={{ display: "grid", gap: 6 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-muted)" }}>{t("page.workspaces.import_mode")}</span>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "live" | "simulate")}
            style={{ height: 38, borderRadius: 10, border: "0.5px solid #e5e5ea", padding: "0 10px", background: "var(--surface-panel)", color: "var(--text-strong)" }}
          >
            <option value="live">{t("page.workspaces.import_mode_live")}</option>
            <option value="simulate">{t("page.workspaces.import_mode_simulate")}</option>
          </select>
        </label>
        <div style={{ display: "grid", gap: 6 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-muted)" }}>{t("approval_settings.local_mode")}</span>
          <Select
            value={preset}
            onChange={(value) => setPreset(value as GovernancePresetKey)}
            options={(presets ?? []).map((p) => ({ value: p.key, label: t(p.key === "follow_default" ? "approval_settings.follow" : `approval_settings.${p.key}`), description: p.summary }))}
            ariaLabel={t("approval_settings.local_mode")}
            disabled={!presets}
          />
        </div>
        <label style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--text-muted)", fontSize: 13 }}>
          <input
            type="checkbox"
            checked={createMissingAgents}
            onChange={(e) => setCreateMissingAgents(e.target.checked)}
          />
          {t("page.workspaces.create_missing_agents")}
        </label>
        {error && <div style={{ color: "#b91c1c", fontSize: 12, lineHeight: 1.5 }}>{error}</div>}
      </div>
    </Modal>
  );
}

function ListFold({ label, open, onToggle }: { label: string; open: boolean; onToggle: (open: boolean) => void }) {
  return (
    <button type="button" className="workspace-list-fold-toggle" aria-expanded={open} onClick={() => onToggle(!open)}>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true" style={{ transform: open ? "rotate(90deg)" : undefined, transition: "transform 0.15s" }}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
      </svg>
      {label}
    </button>
  );
}

export default function Workspaces() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [showModal, setShowModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [editing, setEditing] = useState<Workspace | null>(null);
  const currentUser = useAuthStore((s) => s.user);
  // An external collaborator sees only their granted Workspaces: no drafts,
  // creation, marketplace or trash, all of which the server refuses them.
  const collaborator = useIsCollaborator();
  // Workspace membership for the row being edited — powers the access summary
  // in the edit modal. Only fetched while that modal is open so the list page
  // doesn't issue a staff query per workspace.
  const { data: editingStaff } = useQuery({
    queryKey: ["workspace-staff", editing?.id],
    queryFn: () => api.workspaces.staff.list(editing!.id),
    enabled: showModal && !!editing?.id,
  });
  const [form, setForm] = useState<WorkspaceForm>(emptyForm);
  const [upgrading, setUpgrading] = useState<Workspace | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmDraftDelete, setConfirmDraftDelete] = useState<string | null>(null);
  const [showDrafts, setShowDrafts] = useState<boolean | null>(null);
  const [search, setSearch] = useState("");
  let view = parseWorkspaceView(searchParams.get("view"), false);

  useEffect(() => {
    const requestedView = searchParams.get("view");
    if (!requestedView || requestedView === view) return;
    const params = new URLSearchParams(searchParams);
    params.delete("view");
    setSearchParams(params, { replace: true });
  }, [searchParams, setSearchParams, view]);

  const { data: workspaces, isLoading } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
  });

  const { data: activeDrafts } = useQuery({
    queryKey: ["workspace-drafts", "active"],
    queryFn: async () => {
      const [a, r] = await Promise.all([
        api.workspaceDrafts.list("active"),
        api.workspaceDrafts.list("ready"),
      ]);
      return [...a, ...r].sort((x, y) =>
        (y.updated_at || y.created_at || "").localeCompare(x.updated_at || x.created_at || ""),
      );
    },
    enabled: !collaborator,
  });

  const [tileMenu, setTileMenu] = useState<{ ws: Workspace; x: number; y: number } | null>(null);
  const longPressTimerRef = useRef<number | undefined>(undefined);
  const longPressedRef = useRef(false);
  const cancelLongPress = () => window.clearTimeout(longPressTimerRef.current);
  const allWs = workspaces || [];
  const deleteTarget = confirmDelete
    ? allWs.find((ws: Workspace) => ws.id === confirmDelete)
    : null;
  const canDeleteTarget = deleteTarget?.can_delete === true;
  const totalTasks = allWs.reduce((sum, ws: any) => sum + ((ws.stats?.tasks) || 0), 0);
  const activeTasks = allWs.reduce((sum, ws: any) => sum + ((ws.stats?.tasks_active) || 0), 0);
  const totalGoals = allWs.reduce((sum, ws: any) => sum + ((ws.stats?.goals) || 0), 0);
  const totalAgents = allWs.reduce((sum, ws: any) => sum + ((ws.stats?.agents) || 0), 0);
  const hasWorkspaceActivity = totalTasks > 0 || totalGoals > 0 || totalAgents > 0;

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<Workspace> }) =>
      api.workspaces.update(id, data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["workspaces"] }); closeModal(); toast.success(t("page.workspaces.workspace_updated")); },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.workspaces.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.invalidateQueries({ queryKey: ["workspaces-trash"] });
      setConfirmDelete(null);
      toast.success(t("page.workspaces.moved_to_trash"));
    },
  });

  const [showTrash, setShowTrash] = useState(false);
  const {
    data: trashedWorkspaces,
    isLoading: trashLoading,
    isError: trashFailed,
    refetch: refetchTrash,
  } = useQuery({
    queryKey: ["workspaces-trash"],
    queryFn: () => api.workspaces.trash(),
    enabled: showTrash,
  });
  const { data: graceDaysResp } = useQuery({
    queryKey: ["workspaces-grace-days"],
    queryFn: () => api.workspaces.graceDays(),
    enabled: !collaborator,
    staleTime: 60 * 60 * 1000,
  });
  const graceDays = graceDaysResp?.grace_days ?? 30;

  const restoreMutation = useMutation({
    mutationFn: (id: string) => api.workspaces.restore(id),
    onSuccess: (restored) => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.invalidateQueries({ queryKey: ["workspaces-trash"] });
      const unmapped = restored.unmapped_agents ?? [];
      if (unmapped.length) {
        toast.warning(
          t("page.workspaces.restored"),
          t("page.workspaces.restored_unmapped_agents", {
            names: unmapped.map((agent) => agent.agent_name || agent.service_key || agent.agent_id).join(", "),
          }),
        );
      } else {
        toast.success(t("page.workspaces.restored"));
      }
    },
    onError: (err: Error) => toast.error(t("page.workspaces.restore_failed"), err.message),
  });

  const [trashSelection, setTrashSelection] = useState<string[]>([]);
  const [confirmPurge, setConfirmPurge] = useState<string[] | null>(null);
  const deletableTrashIds = (trashedWorkspaces ?? []).filter((ws) => ws.can_delete).map((ws) => ws.id);
  const allDeletableTrashSelected = deletableTrashIds.length > 0 && deletableTrashIds.every((id) => trashSelection.includes(id));

  function toggleTrashSelection(id: string) {
    setTrashSelection((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  const purgeMutation = useMutation({
    mutationFn: async (ids: string[]) => {
      if (ids.length === 1) {
        await api.workspaces.purgeTrashed(ids[0]);
        return { purged: ids, failed: [] };
      }
      return api.workspaces.purgeTrashedMany(ids);
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.invalidateQueries({ queryKey: ["workspaces-trash"] });
      setTrashSelection([]);
      setConfirmPurge(null);
      if (result.failed.length === 0) {
        toast.success(
          result.purged.length === 1
            ? t("page.workspaces.deleted_now")
            : t("page.workspaces.deleted_now_many").replace("{count}", String(result.purged.length)),
        );
      } else {
        toast.error(
          t("page.workspaces.delete_now_partial").replace("{count}", String(result.failed.length)),
          result.failed[0].reason,
        );
      }
    },
    onError: (err: Error) => {
      setConfirmPurge(null);
      toast.error(err.message);
    },
  });

  function daysUntilPurge(deletedAt?: string | null): number {
    if (!deletedAt) return 0;
    const deleted = new Date(deletedAt).getTime();
    const purgeAt = deleted + graceDays * 86400 * 1000;
    return Math.max(0, Math.ceil((purgeAt - Date.now()) / (86400 * 1000)));
  }

  const togglePauseMutation = useMutation({
    mutationFn: (ws: Workspace) => ws.status === "active"
      ? api.workspaces.pause(ws.id)
      : api.workspaces.resume(ws.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      toast.success(t("page.workspaces.workspace_updated"));
    },
    onError: (err: Error) => toast.error(t("page.dashboard.failed"), err.message),
  });

  const deleteDraftMutation = useMutation({
    mutationFn: (id: string) => api.workspaceDrafts.abandon(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-drafts", "active"] });
      setConfirmDraftDelete(null);
      toast.success(t("page.workspaces.draft_deleted"));
    },
    onError: (err: Error) => toast.error(t("page.workspaces.could_not_delete_draft"), err.message),
  });

  function openCreate() { navigate("/workspaces/new"); }
  function openEdit(ws: Workspace) {
    setEditing(ws);
    setForm({ name: ws.name, description: ws.description || "", category: ws.category || "" });
    setShowModal(true);
  }
  function closeModal() { setShowModal(false); setEditing(null); setForm(emptyForm); }
  function handleSave() { if (editing) updateMutation.mutate({ id: editing.id, data: form }); }
  useEffect(() => {
    if (confirmDelete && !canDeleteTarget) setConfirmDelete(null);
  }, [canDeleteTarget, confirmDelete]);
  const isSaving = updateMutation.isPending;

  const filtered = allWs.filter((ws: Workspace) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      ws.name.toLowerCase().includes(q) ||
      (ws.description || "").toLowerCase().includes(q) ||
      (ws.category || "").toLowerCase().includes(q)
    );
  });

  const isMarketplace =
    false;


  return (
    <div className="workspaces-page relative z-10 flex h-full min-h-0 flex-col overflow-hidden">
      <PageHeader
        title={
          t("page.workspaces.title")
        }
        subtitle={
          undefined}
        tabs={
          undefined}
        toolbar={!isMarketplace && (allWs.length > 6 || search) ? (
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.workspaces.search_placeholder")}
            className="w-full sm:w-64"
          />
        ) : undefined}
      />

      {
      (
      <>

      {/* Edit Modal */}
      <Modal
        open={showModal}
        onClose={closeModal}
        title={t("page.workspaces.edit_workspace")}
        footer={
          <>
            <Button variant="outline" onClick={closeModal}>{t("action.cancel")}</Button>
            <Button variant="primary" disabled={!form.name || isSaving} onClick={handleSave}>
              {isSaving ? t("page.workspaces.saving") : t("page.workspaces.update")}
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Input label={t("page.workspaces.name")} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t("page.workspaces.workspace_name")} />
          <Textarea label={t("page.workspaces.description")} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} rows={3} placeholder={t("page.task_collections.optional_description")} />
          <Input label={t("page.workspaces.category")} value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} placeholder={t("page.workspaces.category_placeholder")} />
          {editing && <WorkspaceAccessRow
            workspace={editing}
            staffRows={editingStaff}
            currentUser={currentUser}
            onManage={() => {
              closeModal();
              navigate(`/workspaces/${editing.id}?tab=staff`);
            }}
          />}
          {editing?.can_manage && <Button variant="outline" onClick={()=>{setShowModal(false);navigate(`/workspaces/${editing.id}?tab=collaboration`);}}>{t("page.collaboration.manage")}</Button>}
          {editing && (
            <div
              style={{
                border: "0.5px solid rgba(29, 29, 31, 0.07)",
                borderRadius: 16,
                background: "linear-gradient(135deg, rgba(250,250,249,0.92), rgba(245,245,244,0.58))",
                padding: 12,
                display: "grid",
                gap: 12,
              }}
            >
              <WorkspaceCreatorSummary workspace={editing} />
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12, paddingTop: 10, borderTop: "0.5px solid rgba(29, 29, 31, 0.06)" }}>
                {metadataStat(t("page.workspace_detail.field_created"), formatDate(editing.created_at))}
                {metadataStat(t("page.workspace_detail.field_updated"), formatDate(editing.updated_at))}
              </div>
            </div>
          )}
          {updateMutation.isError && <p className="text-red-600 text-sm">{(updateMutation.error as Error).message}</p>}
        </div>
      </Modal>

      {upgrading && (
        <BlueprintUpgradeDialog
          open
          onClose={() => setUpgrading(null)}
          workspaceId={upgrading.id}
          workspaceName={upgrading.name}
        />
      )}
      <ConfirmDialog open={canDeleteTarget} onClose={() => setConfirmDelete(null)} onConfirm={() => { if (deleteTarget?.can_delete) deleteMutation.mutate(deleteTarget.id); }} title={t("page.workspaces.delete_workspace")} message={t("page.workspaces.delete_workspace_msg").replace("{days}", String(graceDays))} confirmLabel={t("page.workspaces.move_to_trash")} danger />
      <ConfirmDialog open={!!confirmDraftDelete} onClose={() => setConfirmDraftDelete(null)} onConfirm={() => { if (confirmDraftDelete) deleteDraftMutation.mutate(confirmDraftDelete); }} title={t("page.workspaces.delete_draft")} message={t("page.workspaces.delete_draft_msg")} confirmLabel={t("action.delete")} danger />
      <ConfirmDialog open={!!confirmPurge} onClose={() => setConfirmPurge(null)} onConfirm={() => { if (confirmPurge) purgeMutation.mutate(confirmPurge); }} title={t("page.workspaces.delete_now")} message={confirmPurge && confirmPurge.length > 1 ? t("page.workspaces.delete_now_many_msg").replace("{count}", String(confirmPurge.length)) : t("page.workspaces.delete_now_msg")} confirmLabel={t("page.workspaces.delete_now")} danger />
      <ImportWorkspaceDialog
        open={showImportModal}
        onClose={() => setShowImportModal(false)}
        onImported={(workspaceId) => {
          queryClient.invalidateQueries({ queryKey: ["workspaces"] });
          navigate(`/workspaces/${workspaceId}`);
        }}
      />

      {/* Scrollable content */}
      <div style={{ flex: 1, overflowY: "auto", padding: "0 0 24px" }}>
        {!isLoading && allWs.length === 0 && <WorkspaceIntroPanel />}

        {/* One icon per Workspace, like apps on a home screen: the tile opens its
            conversation, everything else lives in the ⋯ menu. */}
        <div className="workspace-grid">
          {isLoading ? (
            Array.from({ length: 4 }, (_, index) => (
              <div key={index} className="workspace-tile" aria-hidden="true">
                <span className="workspace-tile-icon workspace-tile-icon--empty" />
              </div>
            ))
          ) : filtered.map((ws: Workspace) => {
            const chatActionCount = workspaceChatActionCount(ws);
            const isWorkspaceSimulation = Boolean(
              (ws.settings as Record<string, unknown> | undefined)?.sandbox === true ||
              ws.kind === "sandbox",
            );
            const updateStatus = ws.blueprint_update?.status;
            const hasUpdate = updateStatus === "update_available" || updateStatus === "reconfigure_required";
            // Working on its own is the normal state; a caption only says the exception.
            const caption = hasUpdate
              ? (updateStatus === "reconfigure_required"
                ? t("page.workspaces.blueprint_reconfigure_required")
                : t("page.workspaces.blueprint_update_available"))
              : isWorkspaceSimulation
                ? t("component.simulation_artifact_gallery.workspace_simulation")
                : ws.status === "paused"
                  ? t("page.workspaces.mode_manual")
                  : ws.status !== "active"
                    ? ws.status
                    : "";
            const openChat = () => navigate(`/chat?workspace=${encodeURIComponent(ws.id)}`);
            return (
              <div
                key={ws.id}
                role="link"
                tabIndex={0}
                className="workspace-tile"
                title={ws.description || undefined}
                onClick={() => {
                  // The lift of a long press is not a tap.
                  if (longPressedRef.current) longPressedRef.current = false;
                  else openChat();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") openChat();
                  if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
                    e.preventDefault();
                    const rect = e.currentTarget.getBoundingClientRect();
                    setTileMenu({ ws, x: rect.left + rect.width / 2, y: rect.bottom });
                  }
                }}
                onContextMenu={(e) => {
                  e.preventDefault();
                  setTileMenu({ ws, x: e.clientX, y: e.clientY });
                }}
                onPointerDown={(e) => {
                  if (e.pointerType !== "touch") return;
                  const { clientX: x, clientY: y } = e;
                  cancelLongPress();
                  longPressTimerRef.current = window.setTimeout(() => {
                    longPressedRef.current = true;
                    setTileMenu({ ws, x, y });
                  }, 500);
                }}
                onPointerUp={cancelLongPress}
                onPointerMove={cancelLongPress}
                onPointerCancel={cancelLongPress}
              >
                <span className="workspace-tile-icon">
                  <WorkspaceIconTile workspace={ws} size={72} iconSize={34} style={{ borderRadius: 18 }} />
                  {chatActionCount > 0 && (
                    <span className="workspace-tile-badge" title={t("page.workspaces.review_workspace_chat")}>
                      {chatActionCount}
                    </span>
                  )}
                </span>
                <span className="workspace-tile-name">{ws.name}</span>
                {caption && <span className="workspace-tile-caption">{caption}</span>}
              </div>
            );
          })}

          {!isLoading && !collaborator && !search && (
            <Dropdown
              align="left"
              trigger={(
                <button type="button" className="workspace-tile workspace-tile--add" aria-label={t("page.workspaces.add_workspace")}>
                  <span className="workspace-tile-icon workspace-tile-icon--add"><IconPlus size={24} /></span>
                  <span className="workspace-tile-name">{t("page.workspaces.new_short")}</span>
                </button>
              )}
              items={[
                { key: "create", label: t("page.workspaces.create_workspace"), icon: <IconPlus size={14} /> },
                { key: "import", label: t("page.workspaces.import_workspace"), icon: <IconUpload size={14} /> },
              ]}
              onSelect={(key) => {
                if (key === "create") openCreate();
                if (key === "import") setShowImportModal(true);
              }}
            />
          )}

          {/* Empty search state */}
          {!isLoading && filtered.length === 0 && search && (
            <div style={{ gridColumn: "1 / -1" }}>
              <EmptyState
                icon={
                  <svg style={{ width: 32, height: 32, color: "var(--text-faint)" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
                  </svg>
                }
                title={t("page.workspaces.no_match_search")}
                description={t("page.workspaces.try_different_search")}
              />
            </div>
          )}
        </div>

        {tileMenu && (
          <ContextMenu
            x={tileMenu.x}
            y={tileMenu.y}
            onClose={() => setTileMenu(null)}
            items={[
              { label: t("page.apps.configure"), onClick: () => navigate(`/workspaces/${tileMenu.ws.id}`) },
              ...(tileMenu.ws.blueprint_update?.status === "update_available" || tileMenu.ws.blueprint_update?.status === "reconfigure_required" ? [{
                label: tileMenu.ws.blueprint_update?.status === "reconfigure_required"
                  ? t("page.workspaces.blueprint_reconfigure_title")
                  : t("page.workspaces.blueprint_update_title"),
                onClick: () => setUpgrading(tileMenu.ws),
              }] : []),
              {
                label: tileMenu.ws.status === "active" ? t("page.workspaces.pause") : t("page.workspaces.resume"),
                onClick: () => togglePauseMutation.mutate(tileMenu.ws),
              },
              { label: t("action.edit"), onClick: () => openEdit(tileMenu.ws) },
              ...(tileMenu.ws.can_delete === true ? [{ label: t("action.delete"), danger: true, onClick: () => setConfirmDelete(tileMenu.ws.id) }] : []),
            ]}
          />
        )}

        {/* In-progress drafts come after what already runs, folded unless there is nothing else to see. */}
        {!collaborator && (activeDrafts?.length ?? 0) > 0 && (
          <div className="workspace-list-fold">
            <ListFold
              label={`${t("page.workspaces.drafts")} (${activeDrafts!.length})`}
              open={showDrafts ?? (!isLoading && allWs.length === 0)}
              onToggle={(open) => setShowDrafts(open)}
            />
            {(showDrafts ?? (!isLoading && allWs.length === 0)) && (
              <div className="workspace-list">
                {activeDrafts!.map((d) => {
                  const openDraft = () => navigate(`/workspaces/new?draft=${d.id}`);
                  return (
                    <div
                      key={d.id}
                      role="link"
                      tabIndex={0}
                      className="workspace-row"
                      onClick={openDraft}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") openDraft();
                      }}
                    >
                      <span className="workspace-row-glyph" aria-hidden="true"><IconDocument size={18} /></span>
                      <div className="workspace-row-copy">
                        <div className="workspace-row-name">
                          <span>{(d.fields?.name as string) || t("page.workspaces.untitled_draft")}</span>
                        </div>
                        {d.status === "ready" && <div className="workspace-row-desc">{t("page.workspaces.ready_to_create")}</div>}
                      </div>
                      <button
                        type="button"
                        className="workspace-row-more"
                        aria-label={t("action.delete")}
                        title={t("action.delete")}
                        onClick={(e) => { e.stopPropagation(); setConfirmDraftDelete(d.id); }}
                      >
                        <IconTrash size={14} />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {/* Trash — soft-deleted workspaces in the grace window */}
        {!collaborator && (
          <div className="workspace-list-fold">
            <ListFold
              label={`${t("page.workspaces.trash")}${trashedWorkspaces ? ` (${trashedWorkspaces.length})` : ""}`}
              open={showTrash}
              onToggle={setShowTrash}
            />
            {showTrash && (
              trashLoading ? (
                <ListRowsSkeleton rows={2} avatar={false} action />
              ) : trashFailed ? (
                <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--text-muted)", fontSize: 13 }}>
                  <span>{t("page.tasks.load_more_failed")}</span>
                  <Button size="sm" variant="outline" onClick={() => void refetchTrash()}>{t("page.tasks.retry_load_more")}</Button>
                </div>
              ) : trashedWorkspaces?.length ? (
                <>
                  {deletableTrashIds.length > 0 && (
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 8 }}>
                      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-muted)" }}>
                        <input
                          type="checkbox"
                          checked={allDeletableTrashSelected}
                          onChange={() => setTrashSelection(allDeletableTrashSelected ? [] : deletableTrashIds)}
                        />
                        {t("page.workspaces.select_all")}
                      </label>
                      {trashSelection.length > 0 && (
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => setConfirmPurge(trashSelection)}
                          loading={purgeMutation.isPending}
                        >
                          {t("page.workspaces.delete_selected").replace("{count}", String(trashSelection.length))}
                        </Button>
                      )}
                    </div>
                  )}
                  <div className="workspace-list">
                    {trashedWorkspaces.map((ws) => (
                      <div key={ws.id} className="workspace-row workspace-row--static">
                        {ws.can_delete && (
                          <input
                            type="checkbox"
                            aria-label={ws.name}
                            checked={trashSelection.includes(ws.id)}
                            onChange={() => toggleTrashSelection(ws.id)}
                          />
                        )}
                        <span className="workspace-row-glyph" aria-hidden="true"><IconTrash size={16} /></span>
                        <div className="workspace-row-copy">
                          <div className="workspace-row-name"><span>{ws.name}</span></div>
                          <div className="workspace-row-desc">{t("page.workspaces.days_until_purge").replace("{days}", String(daysUntilPurge(ws.deleted_at)))}</div>
                        </div>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => restoreMutation.mutate(ws.id)}
                          loading={restoreMutation.isPending}
                        >
                          {t("page.workspaces.restore")}
                        </Button>
                        {ws.can_delete && (
                          <Button
                            size="sm"
                            variant="danger"
                            onClick={() => setConfirmPurge([ws.id])}
                            loading={purgeMutation.isPending}
                          >
                            {t("page.workspaces.delete_now")}
                          </Button>
                        )}
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p className="workspace-list-fold-empty">{t("page.knowledge.trash_is_empty")}</p>
              )
            )}
          </div>
        )}
      </div>
      </>
      )}
    </div>
  );
}
