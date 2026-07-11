import { useEffect, useRef, useState, lazy, Suspense } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import type { Workspace } from "../lib/types";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import Dropdown from "../components/ui/Dropdown";
import StatusBadge from "../components/ui/StatusBadge";
import GlassCard from "../components/ui/GlassCard";
import SmartToolbar from "../components/ui/SmartToolbar";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Chip from "../components/ui/Chip";
import FilterPills from "../components/ui/FilterPills";
import ItemCard from "../components/ui/ItemCard";
import WorkspaceIconTile, { getWorkspacePresentation } from "../components/ui/WorkspaceIcon";
import {
  IconAgent,
  IconChat,
  IconChecklist,
  IconDocument,
  IconEdit,
  IconPlus,
  IconTrash,
  IconUpload,
  IconWorkspace,
} from "../components/icons";
import { t } from "../lib/i18n";
import { formatDate } from "../lib/format";
const ManorOffice = lazy(() => import("./ManorOffice"));
const WorkspaceGoalGraph = lazy(() => import("../components/ui/WorkspaceGoalGraph"));

interface WorkspaceForm {
  name: string;
  description: string;
  category: string;
}

type WorkspaceView =
  | "workspaces"
  | "office"
  | "goals";

const emptyForm: WorkspaceForm = { name: "", description: "", category: "" };
const WORKSPACE_VIEW_KEYS: WorkspaceView[] = [
  "workspaces",
  "office",
  "goals",
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
function positiveStat(value: unknown) {
  const count = Number(value || 0);
  return Number.isFinite(count) && count > 0 ? count : 0;
}

function workspaceChatActionCount(ws: Pick<Workspace, "stats">) {
  const stats = ws.stats || {};
  const hasChatActionStats = [
    stats.chat_pending_actions,
    stats.proposal_actions,
    stats.failed_actions,
  ].some((value) => value !== undefined && value !== null);

  if (!hasChatActionStats) {
    return positiveStat(stats.pending_actions ?? stats.hitl_tasks ?? 0);
  }

  return (
    positiveStat(stats.chat_pending_actions) +
    positiveStat(stats.proposal_actions) +
    positiveStat(stats.failed_actions)
  );
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
        {workspace.created_by_avatar_url ? (
          <img
            src={workspace.created_by_avatar_url}
            alt=""
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          creatorInitial(workspace)
        )}
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 10, fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.08em", color: "#a8a29e", marginBottom: 3 }}>
          {t("page.workspace_detail.field_created_by")}
        </div>
        <div style={{ fontSize: 13, fontWeight: 760, color: "#292524", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {creatorName}
        </div>
        {creatorEmail && (
          <div style={{ fontSize: 12, color: "#78716c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
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
      <div style={{ fontSize: 10, fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.08em", color: "#a8a29e", marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 12, fontWeight: 700, color: "#44403c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {value}
      </div>
    </div>
  );
}

function parseWorkspaceView(value: string | null, includeMarketplace = true): WorkspaceView {
  const parsed = WORKSPACE_VIEW_KEYS.includes(value as WorkspaceView)
    ? (value as WorkspaceView)
    : "workspaces";
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
  const [workspaceName, setWorkspaceName] = useState("");
  const [mode, setMode] = useState<"live" | "simulate">("live");
  const [createMissingAgents, setCreateMissingAgents] = useState(true);
  const [error, setError] = useState("");
  const [importing, setImporting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setFileName("");
    setPayload(null);
    setWorkspaceName("");
    setMode("live");
    setCreateMissingAgents(true);
    setError("");
    setImporting(false);
  }, [open]);

  const onFileChange = async (file?: File | null) => {
    setError("");
    setPayload(null);
    setFileName(file?.name || "");
    if (!file) return;
    try {
      const parsed = JSON.parse(await readTextFile(file));
      const nextPayload = blueprintPayloadFromJson(parsed);
      if (!nextPayload) {
        setError(t("page.workspaces.import_invalid_blueprint"));
        return;
      }
      setPayload(nextPayload);
      const manifest = isRecord(nextPayload.manifest) ? nextPayload.manifest : {};
      const title = typeof manifest.title === "string" ? manifest.title : "";
      if (title && !workspaceName) setWorkspaceName(title);
    } catch (err: any) {
      setError(err?.message || t("page.workspaces.import_invalid_json"));
    }
  };

  const doImport = async () => {
    if (!payload) {
      setError(t("page.workspaces.import_select_file_first"));
      return;
    }
    setImporting(true);
    setError("");
    try {
      const result = await api.blueprints.installPayload({
        payload,
        mode,
        workspace_name: workspaceName.trim() || undefined,
        create_missing_agents: createMissingAgents,
        governance_preset: "standard",
      });
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
          <Button variant="primary" onClick={doImport} disabled={!payload || importing} loading={importing}>
            {importing ? t("page.workspaces.importing_workspace") : t("page.workspaces.import_workspace")}
          </Button>
        </>
      }
    >
      <div style={{ display: "grid", gap: 14 }}>
        <p style={{ margin: 0, color: "#78716c", fontSize: 13, lineHeight: 1.55 }}>
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
            accept="application/json,.json"
            onChange={(e) => onFileChange(e.target.files?.[0])}
            style={{ display: "none" }}
          />
          <span style={{ display: "flex", alignItems: "center", gap: 8, color: "#436b65", fontSize: 13, fontWeight: 800 }}>
            <IconUpload size={15} />
            {fileName || t("page.workspaces.choose_blueprint_json")}
          </span>
          <span style={{ color: "#78716c", fontSize: 12 }}>
            {t("page.workspaces.choose_blueprint_json_hint")}
          </span>
        </label>
        <Input
          label={t("page.workspaces.import_workspace_name")}
          value={workspaceName}
          onChange={(e) => setWorkspaceName(e.target.value)}
          placeholder={t("page.workspaces.workspace_name")}
        />
        <label style={{ display: "grid", gap: 6 }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: "#57534e" }}>{t("page.workspaces.import_mode")}</span>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "live" | "simulate")}
            style={{ height: 38, borderRadius: 10, border: "1px solid #e7e5e4", padding: "0 10px", background: "#fff", color: "#292524" }}
          >
            <option value="live">{t("page.workspaces.import_mode_live")}</option>
            <option value="simulate">{t("page.workspaces.import_mode_simulate")}</option>
          </select>
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 8, color: "#57534e", fontSize: 13 }}>
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

export default function Workspaces() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [showModal, setShowModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [editing, setEditing] = useState<Workspace | null>(null);
  const [form, setForm] = useState<WorkspaceForm>(emptyForm);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmDraftDelete, setConfirmDraftDelete] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  let view = parseWorkspaceView(searchParams.get("view"), false);


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
  });

  const allWs = workspaces || [];
  const activeCount = allWs.filter((ws: Workspace) => ws.status === "active").length;
  const pausedCount = allWs.filter((ws: Workspace) => ws.status === "paused").length;
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

  const { data: trashedWorkspaces } = useQuery({
    queryKey: ["workspaces-trash"],
    queryFn: () => api.workspaces.trash(),
  });
  const { data: graceDaysResp } = useQuery({
    queryKey: ["workspaces-grace-days"],
    queryFn: () => api.workspaces.graceDays(),
    staleTime: 60 * 60 * 1000,
  });
  const graceDays = graceDaysResp?.grace_days ?? 30;
  const [showTrash, setShowTrash] = useState(false);

  const restoreMutation = useMutation({
    mutationFn: (id: string) => api.workspaces.restore(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.invalidateQueries({ queryKey: ["workspaces-trash"] });
      toast.success(t("page.workspaces.restored"));
    },
    onError: (err: Error) => toast.error(t("page.workspaces.restore_failed"), err.message),
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
  function handleViewChange(next: string) {
    let nextView = parseWorkspaceView(next, false);
    const params = new URLSearchParams(searchParams);
    if (nextView === "workspaces") {
      params.delete("view");
    } else {
      params.set("view", nextView);
    }
    setSearchParams(params, { replace: true });
  }

  const isSaving = updateMutation.isPending;

  const filtered = allWs.filter((ws: Workspace) => {
    if (statusFilter === "active" && ws.status !== "active") return false;
    if (statusFilter === "paused" && ws.status !== "paused") return false;
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      ws.name.toLowerCase().includes(q) ||
      (ws.description || "").toLowerCase().includes(q) ||
      (ws.category || "").toLowerCase().includes(q)
    );
  });

  if (isLoading
  ) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", gap: 12, color: "#a8a29e" }}>
        <LoadingSpinner size={20} />
        <span style={{ fontSize: 14 }}>{t("page.workspaces.loading")}</span>
      </div>
    );
  }

  const viewTabs = [
    { key: "workspaces", label: t("page.workspaces.tab_workspaces") },
    { key: "goals", label: t("nav.goals"), badge: t("page.workspaces.beta") },
    { key: "office", label: t("page.workspaces.tab_office"), badge: t("page.workspaces.beta") },
  ];

  const isOffice = view === "office";
  const isGoals = view === "goals";
  const isMarketplace =
    false;

  const filterOptions = [
    { key: "all", label: t("page.workspaces.filter_all"), count: allWs.length },
    { key: "active", label: t("page.workspaces.filter_active"), count: activeCount },
    { key: "paused", label: t("page.workspaces.filter_paused"), count: pausedCount },
  ];

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "clamp(0.5rem, 2.5vw, 1rem)", overflow: "hidden", position: "relative", zIndex: 10 }}>
      <PageHeader
        title={t("page.workspaces.title")}
        subtitle={isOffice ? t("page.workspaces.office_view") : isGoals ? t("page.workspaces.goals_across") :
          t("page.workspaces.focused_operating_rooms_for_agents_knowledge_tasks_cha")}
        tabs={(
          <TabSwitcher
            tabs={viewTabs}
            value={view}
            onChange={handleViewChange}
            wrap
          />
        )}
        toolbar={!isOffice && !isGoals && !isMarketplace ? (
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.workspaces.search_placeholder")}
            className="w-full sm:w-64"
          />
        ) : undefined}
        actions={!isOffice && !isGoals && !isMarketplace ? (
          <Dropdown
            align="right"
            trigger={<PageHeaderAddButton label={t("page.workspaces.add_workspace")} caret />}
            items={[
              { key: "create", label: t("page.workspaces.create_workspace"), icon: <IconPlus size={14} /> },
              { key: "import", label: t("page.workspaces.import_workspace"), icon: <IconUpload size={14} /> },
            ]}
            onSelect={(key) => {
              if (key === "create") openCreate();
              if (key === "import") setShowImportModal(true);
            }}
          />
        ) : undefined}
      />

      {isOffice ? (
        <div style={{ flex: 1, overflow: "hidden" }}>
          <Suspense fallback={<LoadingSpinner size={20} />}>
            <ManorOffice />
          </Suspense>
        </div>
      ) : isGoals ? (
        <div className="workspaces-goals-view" style={{ flex: 1, overflowY: "auto", padding: "0 clamp(0px, 2vw, 24px) 24px" }}>
          <Suspense fallback={<LoadingSpinner size={20} />}>
            {allWs.length === 0 ? (
              <EmptyState title={t("page.workspaces.no_workspaces")} description={t("page.workspaces.no_workspaces_goals_desc")} />
            ) : (
              <div className="workspaces-goals-list" style={{ display: "flex", flexDirection: "column", gap: 24 }}>
                {allWs.filter((ws: Workspace) => ws.status === "active").map((ws: Workspace) => (
                  <div className="workspaces-goals-workspace" key={ws.id}>
                    <div className="workspaces-goals-workspace-header" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                      <div className="workspaces-goals-workspace-icon" style={{
                        width: 28, height: 28, borderRadius: 8, background: "#fbfbfa",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 11, fontWeight: 800, color: "#57534e",
                      }}>
                        {ws.name.charAt(0).toUpperCase()}
                      </div>
                      <span className="workspaces-goals-workspace-title" style={{ fontSize: 16, fontWeight: 700, color: "#1c1917" }}>{ws.name}</span>
                      <Chip variant="teal" size="sm">{ws.status}</Chip>
                    </div>
                    <WorkspaceGoalGraph workspaceId={ws.id} />
                  </div>
                ))}
              </div>
            )}
          </Suspense>
        </div>
      ) : (
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
          {editing && (
            <div
              style={{
                border: "1px solid rgba(28, 25, 23, 0.07)",
                borderRadius: 16,
                background: "linear-gradient(135deg, rgba(250,250,249,0.92), rgba(245,245,244,0.58))",
                padding: 12,
                display: "grid",
                gap: 12,
              }}
            >
              <WorkspaceCreatorSummary workspace={editing} />
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12, paddingTop: 10, borderTop: "1px solid rgba(28, 25, 23, 0.06)" }}>
                {metadataStat(t("page.workspace_detail.field_created"), formatDate(editing.created_at))}
                {metadataStat(t("page.workspace_detail.field_updated"), formatDate(editing.updated_at))}
              </div>
            </div>
          )}
          {updateMutation.isError && <p className="text-red-600 text-sm">{(updateMutation.error as Error).message}</p>}
        </div>
      </Modal>

      <ConfirmDialog open={!!confirmDelete} onClose={() => setConfirmDelete(null)} onConfirm={() => { if (confirmDelete) deleteMutation.mutate(confirmDelete); }} title={t("page.workspaces.delete_workspace")} message={t("page.workspaces.delete_workspace_msg").replace("{days}", String(graceDays))} confirmLabel={t("page.workspaces.move_to_trash")} danger />
      <ConfirmDialog open={!!confirmDraftDelete} onClose={() => setConfirmDraftDelete(null)} onConfirm={() => { if (confirmDraftDelete) deleteDraftMutation.mutate(confirmDraftDelete); }} title={t("page.workspaces.delete_draft")} message={t("page.workspaces.delete_draft_msg")} confirmLabel={t("action.delete")} danger />
      <ImportWorkspaceDialog
        open={showImportModal}
        onClose={() => setShowImportModal(false)}
        onImported={(workspaceId) => {
          queryClient.invalidateQueries({ queryKey: ["workspaces"] });
          navigate(`/workspaces/${workspaceId}`);
        }}
      />

      {/* Scrollable content */}
      <div style={{ flex: 1, overflowY: "auto", padding: "4px 0 24px" }}>
        <WorkspaceIntroPanel />

        {/* In-progress drafts */}
        {activeDrafts && activeDrafts.length > 0 && (
          <div style={{ marginBottom: 18 }}>
            <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: "#a8a29e", marginBottom: 8, paddingLeft: 4 }}>
              {t("page.workspaces.drafts")}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {activeDrafts.slice(0, 5).map((d) => {
                const name = (d.fields?.name as string) || t("page.workspaces.untitled_draft");
                return (
                  <ItemCard
                    key={d.id}
                    icon={
                      <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                      </svg>
                    }
                    title={name}
                    subtitle={d.status === "ready" ? t("page.workspaces.ready_to_create") : t("page.workspaces.drafting")}
                    accent="teal"
                    onClick={() => navigate(`/workspaces/new?draft=${d.id}`)}
                    actions={
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <StatusBadge type={d.status === "ready" ? "success" : "info"} dot pulse={d.status === "active"}>
                          {d.status === "ready" ? t("page.workspaces.ready") : t("page.workspaces.draft")}
                        </StatusBadge>
                        <button
                          onClick={(e) => { e.stopPropagation(); setConfirmDraftDelete(d.id); }}
                          style={{ width: 28, height: 28, borderRadius: 8, border: "none", background: "transparent", color: "#a8a29e", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}
                          onMouseEnter={(e) => { e.currentTarget.style.background = "#f8f0ef"; e.currentTarget.style.color = "#c14a44"; }}
                          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "#a8a29e"; }}
                        >
                          <IconTrash size={13} />
                        </button>
                      </div>
                    }
                  />
                );
              })}
            </div>
          </div>
        )}

        <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 14, flexWrap: "wrap", margin: "0 0 12px" }}>
          <div>
            <div style={{ fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: "0.08em", color: "#a8a29e", marginBottom: 3 }}>
              {t("page.workspaces.current_workspaces")}
            </div>
            <div style={{ color: "#78716c", fontSize: 12 }}>
              {t("page.workspaces.open_a_workspace_to_manage_its_tasks_knowledge_a")}
            </div>
          </div>
          {hasWorkspaceActivity && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              <Chip variant="teal" size="sm">{activeCount} {t("page.workspaces.active")}</Chip>
              {totalTasks > 0 && <Chip variant="slate" size="sm">{activeTasks} / {totalTasks} {t("page.tasks.tasks")}</Chip>}
              {totalAgents > 0 && <Chip variant="blue" size="sm">{totalAgents} {t("page.agent_dashboard.agents_plural")}</Chip>}
              {totalGoals > 0 && <Chip variant="green" size="sm">{totalGoals} {t("page.workspaces.goals")}</Chip>}
            </div>
          )}
        </div>

        {/* Filter Pills */}
        <div style={{ marginBottom: 16 }}>
          <FilterPills options={filterOptions} value={statusFilter} onChange={setStatusFilter} />
        </div>

        {/* Workspace Cards Grid */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 300px), 1fr))", gap: 14 }}>
          {/* Workspace Cards */}
          {filtered.map((ws: Workspace) => {
            const presentation = getWorkspacePresentation(ws);
            const stats = (ws as any).stats || {};
            const taskCount = Number(stats.tasks || 0);
            const activeTaskCount = Number(stats.tasks_active || 0);
            const agentCount = Number(stats.agents || 0);
            const goalCount = Number(stats.goals || 0);
            const chatActionCount = workspaceChatActionCount(ws);
            const chatButtonLabel = chatActionCount > 0
              ? t("page.workspaces.review_workspace_chat")
              : t("page.workspaces.open_workspace_chat");
            const statusText = ws.status === "active"
              ? t("page.agents.live")
              : ws.status === "paused"
                ? t("page.workspaces.filter_paused")
                : ws.status;
            return (
              <GlassCard
                key={ws.id}
                onClick={() => navigate(`/workspaces/${ws.id}`)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  openEdit(ws);
                }}
                style={{
                  height: 252,
                  display: "flex",
                  flexDirection: "column",
                  justifyContent: "space-between",
                  overflow: "hidden",
                }}
                footer={
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(`/chat?workspace=${encodeURIComponent(ws.id)}`);
                      }}
                      aria-label={chatActionCount > 0
                        ? `${chatButtonLabel}: ${chatActionCount}`
                        : chatButtonLabel}
                      title={chatActionCount > 0
                        ? `${chatButtonLabel}: ${chatActionCount}`
                        : chatButtonLabel}
                      style={{
                        minWidth: 0,
                        flex: "1 1 auto",
                        height: 30,
                        borderRadius: 9,
                        border: "1px solid rgba(28,25,23,0.06)",
                        background: chatActionCount > 0 ? "#3f6f68" : "#4f7d75",
                        color: "#fff",
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        gap: 7,
                        padding: "0 10px",
                        fontSize: 12,
                        fontWeight: 800,
                        cursor: "pointer",
                        boxShadow: "0 8px 18px rgba(63,111,104,0.18)",
                      }}
                    >
                      <IconChat size={13} />
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {chatButtonLabel}
                      </span>
                      {chatActionCount > 0 && (
                        <span style={{
                          minWidth: 18,
                          height: 18,
                          borderRadius: 9,
                          padding: "0 5px",
                          background: "#fff",
                          color: "#3f6f68",
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          fontSize: 11,
                          fontWeight: 900,
                          lineHeight: 1,
                          flexShrink: 0,
                        }}>
                          {chatActionCount}
                        </span>
                      )}
                    </button>
                    <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                      <button
                        onClick={(e) => { e.stopPropagation(); togglePauseMutation.mutate(ws); }}
                        title={ws.status === "active" ? t("page.workspaces.pause") : t("page.workspaces.resume")}
                        style={{ width: 28, height: 28, borderRadius: 8, border: "1px solid rgba(28,25,23,0.055)", background: "#ffffff", color: "#78716c", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", transition: "all 0.15s" }}
                      >
                        {ws.status === "active" ? (
                          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}><rect x="6" y="4" width="4" height="16" rx="1" /><rect x="14" y="4" width="4" height="16" rx="1" /></svg>
                        ) : (
                          <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}><path strokeLinecap="round" strokeLinejoin="round" d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z" /></svg>
                        )}
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); openEdit(ws); }}
                        title={t("action.edit")}
                        style={{ width: 28, height: 28, borderRadius: 8, border: "1px solid rgba(28,25,23,0.055)", background: "#ffffff", color: "#78716c", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", transition: "all 0.15s" }}
                      >
                        <IconEdit size={12} />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setConfirmDelete(ws.id); }}
                        title={t("action.delete")}
                        style={{ width: 28, height: 28, borderRadius: 8, border: "1px solid rgba(28,25,23,0.055)", background: "#ffffff", color: "#78716c", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", transition: "all 0.15s" }}
                      >
                        <IconTrash size={12} />
                      </button>
                    </div>
                  </div>
                }
              >
                <div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 14 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                      <WorkspaceIconTile workspace={ws} size={40} iconSize={20} />
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 10, color: "#a8a29e", fontWeight: 850, textTransform: "uppercase", letterSpacing: "0.07em", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {presentation.label}
                        </div>
                        <StatusBadge type="gray" dot={ws.status === "active"} pulse={false}>
                          {statusText}
                        </StatusBadge>
                      </div>
                    </div>
                  </div>

                  <h3 style={{
                    fontSize: 16,
                    fontWeight: 850,
                    color: "#1c1917",
                    margin: "0 0 6px",
                    lineHeight: 1.25,
                    display: "-webkit-box",
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: "vertical" as const,
                    overflow: "hidden",
                  }}>
                    {ws.name}
                  </h3>

                  <p style={{
                    fontSize: 12, color: "#78716c", lineHeight: 1.55, margin: "0 0 12px",
                    display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" as const, overflow: "hidden",
                    minHeight: 37,
                  }}>
                    {ws.description || t("page.workspaces.no_description")}
                  </p>

                  <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                    {ws.category && <Chip variant="slate" size="sm">{ws.category}</Chip>}
                    {taskCount > 0 && (
                      <Chip variant="slate" size="sm">{activeTaskCount} / {taskCount} {t("page.tasks.tasks")}</Chip>
                    )}
                    {agentCount > 0 && (
                      <Chip variant="slate" size="sm">{agentCount} {t("page.agent_dashboard.agents_plural")}</Chip>
                    )}
                    {goalCount > 0 && (
                      <Chip variant="slate" size="sm">{goalCount} {t("nav.goals").toLowerCase()}</Chip>
                    )}
                  </div>
                </div>
              </GlassCard>
            );
          })}

          {/* Empty search state */}
          {filtered.length === 0 && search && (
            <div style={{ gridColumn: "1 / -1" }}>
              <EmptyState
                icon={
                  <svg style={{ width: 32, height: 32, color: "#d6d3d1" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
                  </svg>
                }
                title={t("page.workspaces.no_match_search")}
                description={t("page.workspaces.try_different_search")}
              />
            </div>
          )}
        </div>

        {/* Trash — soft-deleted workspaces in the grace window */}
        {trashedWorkspaces && trashedWorkspaces.length > 0 && (
          <div style={{ marginTop: 32, borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 16 }}>
            <button
              onClick={() => setShowTrash((v) => !v)}
              style={{
                display: "flex", alignItems: "center", gap: 8,
                background: "transparent", border: "none", cursor: "pointer",
                fontSize: 11, fontWeight: 700, textTransform: "uppercase",
                letterSpacing: "0.08em", color: "#a8a29e", padding: "4px 0",
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} style={{ transform: showTrash ? "rotate(90deg)" : "none", transition: "transform 0.15s" }}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
              {t("page.workspaces.trash")} ({trashedWorkspaces.length})
            </button>
            {showTrash && (
              <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 6 }}>
                {trashedWorkspaces.map((ws) => {
                  const days = daysUntilPurge(ws.deleted_at);
                  return (
                    <ItemCard
                      key={ws.id}
                      icon={<IconTrash size={14} />}
                      title={ws.name}
                      subtitle={t("page.workspaces.days_until_purge").replace("{days}", String(days))}
                      accent="red"
                      actions={
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={(e: any) => { e.stopPropagation(); restoreMutation.mutate(ws.id); }}
                          disabled={restoreMutation.isPending}
                        >
                          {t("page.workspaces.restore")}
                        </Button>
                      }
                    />
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
      </>
      )}
    </div>
  );
}
