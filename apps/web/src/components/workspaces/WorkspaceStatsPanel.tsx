import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type WorkspaceStatDefinition,
  type WorkspaceStatLibraryEntry,
} from "../../lib/api";
import { relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import {
  formatWorkspaceStatValue,
  workspaceStatAttentionPresentation,
} from "../../lib/workspaceStats";
import { useToastStore } from "../../stores/toast";
import { IconTrendingUp } from "../icons";
import Button from "../ui/Button";
import Chip from "../ui/Chip";
import CompactCard from "../ui/CompactCard";
import ConfirmDialog from "../ui/ConfirmDialog";
import EmptyState from "../ui/EmptyState";
import GlassCard from "../ui/GlassCard";
import IconTile from "../ui/IconTile";
import Input from "../ui/Input";
import LoadingSpinner from "../ui/LoadingSpinner";
import Modal from "../ui/Modal";
import RadioCard from "../ui/RadioCard";
import Select from "../ui/Select";
import StatusBadge from "../ui/StatusBadge";
import TabSwitcher from "../ui/TabSwitcher";
import Textarea from "../ui/Textarea";

interface WorkspaceStatsPanelProps {
  workspaceId: string;
  canManage: boolean;
  context?: "overview" | "goals";
}

type AddMode = "library" | "custom";

const WINDOW_OPTIONS = [
  "latest",
  "lifetime",
  "rolling_24h",
  "rolling_7d",
  "rolling_30d",
  "calendar_week",
  "calendar_month",
].map((value) => ({ value, label: value.replace(/_/g, " ") }));

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : t("page.workspace_stats.unknown_error");
}

export default function WorkspaceStatsPanel({ workspaceId, canManage, context = "overview" }: WorkspaceStatsPanelProps) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [addOpen, setAddOpen] = useState(false);
  const [addMode, setAddMode] = useState<AddMode>("library");
  const [librarySearch, setLibrarySearch] = useState("");
  const [selectedLibraryKey, setSelectedLibraryKey] = useState("");
  const [selectedConnectionId, setSelectedConnectionId] = useState("");
  const [selectedStat, setSelectedStat] = useState<WorkspaceStatDefinition | null>(null);
  const [statToDelete, setStatToDelete] = useState<WorkspaceStatDefinition | null>(null);
  const [newValue, setNewValue] = useState("");
  const [valueNote, setValueNote] = useState("");
  const [custom, setCustom] = useState({
    name: "",
    key: "",
    description: "",
    value_type: "number" as WorkspaceStatDefinition["value_type"],
    unit: "",
    window: "latest",
    initial_value: "",
  });

  const statsQuery = useQuery({
    queryKey: ["workspace-stats", workspaceId],
    queryFn: () => api.workspaces.stats.list(workspaceId),
    enabled: Boolean(workspaceId),
  });
  const libraryQuery = useQuery({
    queryKey: ["workspace-stats-library", workspaceId],
    queryFn: () => api.workspaces.stats.library(),
    enabled: addOpen,
    staleTime: 30_000,
    refetchOnMount: "always",
  });
  const historyQuery = useQuery({
    queryKey: ["workspace-stat-observations", workspaceId, selectedStat?.id],
    queryFn: () => api.workspaces.stats.observations(workspaceId, selectedStat!.id, 20),
    enabled: Boolean(selectedStat),
  });

  const stats = statsQuery.data?.items || [];
  const installedKeys = useMemo(
    () => new Set(stats.flatMap((stat) => [stat.key, stat.library_key || ""])),
    [stats],
  );
  const libraryEntries = useMemo(() => {
    const query = librarySearch.trim().toLowerCase();
    return (libraryQuery.data?.items || []).filter((entry) => (
      !query
      || entry.name.toLowerCase().includes(query)
      || entry.description.toLowerCase().includes(query)
      || entry.category.toLowerCase().includes(query)
    ));
  }, [libraryQuery.data?.items, librarySearch]);
  const selectedLibrary = (libraryQuery.data?.items || []).find(
    (entry) => entry.key === selectedLibraryKey,
  );
  const selectedConnections = selectedLibrary?.available_connections || [];
  const effectiveConnectionId = selectedConnectionId
    || (selectedConnections.length === 1 ? selectedConnections[0].id : "");
  const heading = context === "goals"
    ? t("page.workspace_stats.measurement_sources")
    : t("page.workspace_stats.title");
  const description = context === "goals"
    ? t("page.workspace_stats.measurement_sources_description")
    : t("page.workspace_stats.description");

  const refreshStats = () => queryClient.invalidateQueries({ queryKey: ["workspace-stats", workspaceId] });

  const createMutation = useMutation({
    mutationFn: async () => {
      if (addMode === "library") {
        if (!selectedLibrary) throw new Error(t("page.workspace_stats.choose_stat"));
        if (selectedLibrary.collector_type === "integration" && !effectiveConnectionId) {
          throw new Error(t("page.workspace_stats.choose_connection"));
        }
        let stat = await api.workspaces.stats.create(workspaceId, {
          library_key: selectedLibrary.key,
          collector_config: effectiveConnectionId
            ? { connection_id: effectiveConnectionId }
            : undefined,
        });
        let collectionError = "";
        if (stat.collector_type !== "manual") {
          try {
            stat = (await api.workspaces.stats.collect(workspaceId, stat.id)).stat;
          } catch (error) {
            collectionError = errorMessage(error);
          }
        }
        return { stat, collectionError };
      }
      if (!custom.name.trim() || !custom.key.trim()) {
        throw new Error(t("page.workspace_stats.name_key_required"));
      }
      let stat = await api.workspaces.stats.create(workspaceId, {
        name: custom.name.trim(),
        key: custom.key.trim().toLowerCase(),
        description: custom.description.trim() || undefined,
        value_type: custom.value_type,
        unit: custom.unit.trim() || undefined,
        window: custom.window,
        collector_type: "manual",
        goal_eligible: true,
      });
      if (custom.initial_value.trim()) {
        const value = Number(custom.initial_value);
        if (!Number.isFinite(value)) throw new Error(t("page.workspace_stats.valid_number_required"));
        stat = (await api.workspaces.stats.record(workspaceId, stat.id, { value })).stat;
      }
      return { stat, collectionError: "" };
    },
    onSuccess: ({ stat, collectionError }) => {
      void refreshStats();
      setAddOpen(false);
      setSelectedLibraryKey("");
      setSelectedConnectionId("");
      setCustom({ name: "", key: "", description: "", value_type: "number", unit: "", window: "latest", initial_value: "" });
      if (collectionError) {
        toast.warning(t("page.workspace_stats.added_needs_attention"), collectionError);
      } else {
        toast.success(t("page.workspace_stats.added"));
      }
      setSelectedStat(stat);
    },
    onError: (error) => toast.error(t("page.workspace_stats.add_failed"), errorMessage(error)),
  });

  const collectMutation = useMutation({
    mutationFn: (stat: WorkspaceStatDefinition) => api.workspaces.stats.collect(workspaceId, stat.id),
    onSuccess: ({ stat }) => {
      void refreshStats();
      void queryClient.invalidateQueries({ queryKey: ["workspace-goals", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-stat-observations", workspaceId, stat.id] });
      setSelectedStat(stat);
      toast.success(t("page.workspace_stats.collected"));
    },
    onError: (error) => {
      void refreshStats();
      toast.error(t("page.workspace_stats.collect_failed"), errorMessage(error));
    },
  });

  const recordMutation = useMutation({
    mutationFn: async () => {
      if (!selectedStat) throw new Error(t("page.workspace_stats.choose_stat"));
      const value = Number(newValue);
      if (!newValue.trim() || !Number.isFinite(value)) throw new Error(t("page.workspace_stats.valid_number_required"));
      return api.workspaces.stats.record(workspaceId, selectedStat.id, {
        value,
        note: valueNote.trim() || undefined,
      });
    },
    onSuccess: ({ stat }) => {
      setNewValue("");
      setValueNote("");
      setSelectedStat(stat);
      void refreshStats();
      void queryClient.invalidateQueries({ queryKey: ["workspace-goals", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-stat-observations", workspaceId, stat.id] });
      toast.success(t("page.workspace_stats.value_recorded"));
    },
    onError: (error) => toast.error(t("page.workspace_stats.record_failed"), errorMessage(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: (stat: WorkspaceStatDefinition) => api.workspaces.stats.delete(workspaceId, stat.id),
    onSuccess: () => {
      setStatToDelete(null);
      setSelectedStat(null);
      setNewValue("");
      setValueNote("");
      void refreshStats();
      void queryClient.invalidateQueries({ queryKey: ["workspace-goals", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-goals-graph", workspaceId] });
      toast.success(t("page.workspace_stats.removed"));
    },
    onError: (error) => toast.error(t("page.workspace_stats.remove_failed"), errorMessage(error)),
  });

  return (
    <GlassCard
      id={context === "overview" ? "workspace-stats" : "workspace-goal-measurement-sources"}
      hoverable={false}
      className={`workspace-overview-card workspace-overview-stats workspace-overview-stats--${context}`}
    >
      <div className="workspace-stats-header" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, marginBottom: stats.length ? 14 : 0 }}>
        <div style={{ minWidth: 0 }}>
          <div className="workspace-overview-section-heading" style={{ fontSize: 12, fontWeight: 800, color: "var(--text-strong)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            {heading}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5, marginTop: 3 }}>
            {description}
          </div>
        </div>
        {canManage && (
          <Button variant="outline" size="sm" onClick={() => setAddOpen(true)} style={{ flexShrink: 0, whiteSpace: "nowrap" }}>
            {t("page.workspace_stats.add")}
          </Button>
        )}
      </div>

      {statsQuery.isLoading ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 28 }}><LoadingSpinner size={20} /></div>
      ) : statsQuery.isError ? (
        <EmptyState
          title={t("page.workspace_stats.load_failed")}
          description={errorMessage(statsQuery.error)}
          action={<Button variant="outline" size="sm" onClick={() => void statsQuery.refetch()}>{t("page.workspace_stats.try_again")}</Button>}
        />
      ) : stats.length === 0 ? (
        <EmptyState
          title={t("page.workspace_stats.empty")}
          description={t("page.workspace_stats.empty_description")}
          action={canManage ? <Button size="sm" onClick={() => setAddOpen(true)}>{t("page.workspace_stats.add_first")}</Button> : undefined}
        />
      ) : (
        <div className="workspace-stats-grid">
          {stats.map((stat) => {
            const attentionStatus = workspaceStatAttentionPresentation(stat.freshness_status);
            const timing = stat.current_value_updated_at
              ? `${t("page.workspace_stats.updated")} ${relativeTime(stat.current_value_updated_at)}`
              : t("page.workspace_stats.waiting_for_value");
            return (
              <CompactCard
                key={stat.id}
                className="workspace-stat-card"
                icon={<IconTile size={36}><IconTrendingUp size={17} /></IconTile>}
                title={(
                  <span className="workspace-stat-card-title">
                    <span>{stat.name}</span>
                    <strong className="mono">{formatWorkspaceStatValue(stat)}</strong>
                  </span>
                )}
                subtitle={`${stat.window.replace(/_/g, " ")} · ${stat.collector_type.replace(/_/g, " ")} · ${timing}`}
                meta={attentionStatus ? (
                  <StatusBadge type={attentionStatus.type} dot>{attentionStatus.label}</StatusBadge>
                ) : undefined}
                onClick={() => setSelectedStat(stat)}
              />
            );
          })}
        </div>
      )}

      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title={t("page.workspace_stats.add")}
        maxWidth="680px"
        footer={(
          <>
            <Button variant="outline" onClick={() => setAddOpen(false)}>{t("action.cancel")}</Button>
            <Button
              loading={createMutation.isPending}
              disabled={addMode === "library"
                ? !selectedLibrary
                  || (selectedLibrary.collector_type === "integration" && !effectiveConnectionId)
                : !custom.name.trim() || !custom.key.trim()}
              onClick={() => createMutation.mutate()}
            >
              {t("page.workspace_stats.add")}
            </Button>
          </>
        )}
      >
        <TabSwitcher
          tabs={[
            { key: "library", label: t("page.workspace_stats.library") },
            { key: "custom", label: t("page.workspace_stats.custom") },
          ]}
          value={addMode}
          onChange={(value) => setAddMode(value as AddMode)}
          size="sm"
          ariaLabel={t("page.workspace_stats.add_mode")}
        />

        {addMode === "library" ? (
          <div style={{ marginTop: 16 }}>
            <Input
              value={librarySearch}
              onChange={(event) => setLibrarySearch(event.target.value)}
              placeholder={t("page.workspace_stats.search_library")}
              ariaLabel={t("page.workspace_stats.search_library")}
            />
            {libraryEntries.length ? (
              <div role="radiogroup" style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 10, maxHeight: 360, overflowY: "auto" }}>
                {libraryEntries.map((entry: WorkspaceStatLibraryEntry) => {
                  const installed = installedKeys.has(entry.key);
                  return (
                    <RadioCard
                      key={entry.key}
                      selected={selectedLibraryKey === entry.key}
                      onSelect={() => {
                        setSelectedLibraryKey(entry.key);
                        setSelectedConnectionId(
                          entry.available_connections.length === 1
                            ? entry.available_connections[0].id
                            : "",
                        );
                      }}
                      disabled={installed}
                      ariaLabel={entry.name}
                      title={installed ? t("page.workspace_stats.already_added") : undefined}
                    >
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                          <span style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>{entry.name}</span>
                          <Chip variant="slate" size="sm">{installed ? t("page.workspace_stats.added_label") : entry.category}</Chip>
                        </div>
                        <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.45, marginTop: 2 }}>{entry.description}</div>
                      </div>
                    </RadioCard>
                  );
                })}
              </div>
            ) : (
              <div style={{ padding: "24px 12px", textAlign: "center", color: "var(--text-muted)", fontSize: 12 }}>
                {t("page.workspace_stats.no_matching_stats")}
              </div>
            )}
            {selectedLibrary?.collector_type === "integration" && (
              <div style={{ marginTop: 12 }}>
                <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">
                  {t("page.workspace_stats.integration_connection")}
                </label>
                <Select
                  value={effectiveConnectionId}
                  onChange={setSelectedConnectionId}
                  options={selectedConnections.map((connection) => ({
                    value: connection.id,
                    label: connection.label,
                  }))}
                  placeholder={t("page.workspace_stats.choose_connection")}
                  filterable={selectedConnections.length > 6}
                  ariaLabel={t("page.workspace_stats.integration_connection")}
                  style={{ width: "100%" }}
                />
                <p style={{ margin: "6px 2px 0", color: "var(--text-muted)", fontSize: 11, lineHeight: 1.45 }}>
                  {t("page.workspace_stats.integration_connection_hint")}
                </p>
              </div>
            )}
            <p style={{ margin: "10px 2px 0", color: "var(--text-faint)", fontSize: 11, lineHeight: 1.45 }}>
              {t("page.workspace_stats.integration_stats_hint")}
            </p>
          </div>
        ) : (
          <div style={{ display: "grid", gap: 12, marginTop: 16 }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))", gap: 10 }}>
              <Input
                label={t("page.workspace_stats.name")}
                value={custom.name}
                onChange={(event) => setCustom({ ...custom, name: event.target.value })}
                placeholder={t("page.workspace_stats.name_placeholder")}
              />
              <Input
                label={t("page.workspace_stats.key")}
                hint={t("page.workspace_stats.key_hint")}
                value={custom.key}
                onChange={(event) => setCustom({ ...custom, key: event.target.value })}
                placeholder="sales.qualified_leads"
              />
            </div>
            <Textarea
              label={t("page.workspace_stats.description_label")}
              value={custom.description}
              onChange={(event) => setCustom({ ...custom, description: event.target.value })}
              rows={2}
            />
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 150px), 1fr))", gap: 10 }}>
              <div>
                <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">{t("page.workspace_stats.value_type")}</label>
                <Select
                  value={custom.value_type}
                  onChange={(value) => setCustom({ ...custom, value_type: value as WorkspaceStatDefinition["value_type"] })}
                  options={["number", "percent", "currency", "duration"]}
                  ariaLabel={t("page.workspace_stats.value_type")}
                  style={{ width: "100%" }}
                />
              </div>
              <Input
                label={t("page.workspace_stats.unit")}
                value={custom.unit}
                onChange={(event) => setCustom({ ...custom, unit: event.target.value })}
                placeholder="leads"
              />
              <div>
                <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">{t("page.workspace_stats.window")}</label>
                <Select
                  value={custom.window}
                  onChange={(value) => setCustom({ ...custom, window: value })}
                  options={WINDOW_OPTIONS}
                  ariaLabel={t("page.workspace_stats.window")}
                  style={{ width: "100%" }}
                />
              </div>
              <Input
                label={t("page.workspace_stats.initial_value")}
                value={custom.initial_value}
                onChange={(event) => setCustom({ ...custom, initial_value: event.target.value })}
                type="number"
                step="any"
              />
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={Boolean(selectedStat)}
        onClose={() => setSelectedStat(null)}
        title={selectedStat?.name || t("page.workspace_stats.details")}
        maxWidth="640px"
        footer={(
          <>
            <Button variant="outline" onClick={() => setSelectedStat(null)}>{t("action.close")}</Button>
            {canManage && selectedStat && (
              <Button
                variant="danger"
                onClick={() => {
                  deleteMutation.reset();
                  setStatToDelete(selectedStat);
                  setSelectedStat(null);
                }}
              >
                {t("page.workspace_stats.remove")}
              </Button>
            )}
          </>
        )}
      >
        {selectedStat && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 14, flexWrap: "wrap" }}>
              <div>
                <div style={{ fontSize: 34, fontWeight: 750, color: "var(--text-strong)", lineHeight: 1.1 }}>{formatWorkspaceStatValue(selectedStat)}</div>
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 5 }}>{selectedStat.description}</div>
              </div>
              {canManage && selectedStat.collector_type !== "manual" && (
                <Button
                  variant="outline"
                  size="sm"
                  loading={collectMutation.isPending}
                  onClick={() => collectMutation.mutate(selectedStat)}
                >
                  {t("page.workspace_stats.collect_now")}
                </Button>
              )}
            </div>

            <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
              <Chip variant="slate" size="sm">{selectedStat.key}</Chip>
              <Chip variant="slate" size="sm">{selectedStat.window.replace(/_/g, " ")}</Chip>
              <Chip variant="slate" size="sm">{selectedStat.collector_type.replace(/_/g, " ")}</Chip>
              {selectedStat.collection_cadence && <Chip variant="teal" size="sm">{selectedStat.collection_cadence}</Chip>}
              {selectedStat.goal_eligible && <Chip variant="teal" size="sm">{t("page.workspace_stats.goal_eligible")}</Chip>}
            </div>

            {selectedStat.last_collection_error && (
              <div style={{ padding: "10px 12px", borderRadius: 10, border: "1px solid var(--border-subtle)", background: "var(--surface-muted)", color: "var(--text-default)", fontSize: 12, lineHeight: 1.5 }}>
                <strong>{t("page.workspace_stats.collection_error")}</strong> {selectedStat.last_collection_error}
              </div>
            )}

            {canManage && (
              <div style={{ paddingTop: 14, borderTop: "1px solid var(--border-subtle)" }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-strong)", marginBottom: 8 }}>{t("page.workspace_stats.record_value")}</div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 160px), 1fr))", gap: 8, alignItems: "end" }}>
                  <Input value={newValue} onChange={(event) => setNewValue(event.target.value)} type="number" step="any" placeholder="0" ariaLabel={t("page.workspace_stats.value")} />
                  <Input value={valueNote} onChange={(event) => setValueNote(event.target.value)} placeholder={t("page.workspace_stats.optional_note")} ariaLabel={t("page.workspace_stats.optional_note")} />
                  <Button loading={recordMutation.isPending} onClick={() => recordMutation.mutate()}>{t("action.save")}</Button>
                </div>
              </div>
            )}

            <div style={{ paddingTop: 14, borderTop: "1px solid var(--border-subtle)" }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-strong)", marginBottom: 8 }}>{t("page.workspace_stats.history")}</div>
              {historyQuery.isLoading ? (
                <div style={{ padding: 18, display: "flex", justifyContent: "center" }}><LoadingSpinner size={18} /></div>
              ) : (historyQuery.data?.items || []).length === 0 ? (
                <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("page.workspace_stats.no_history")}</div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  {(historyQuery.data?.items || []).map((observation) => (
                    <div key={observation.id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border-subtle)" }}>
                      <div>
                        <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>{formatWorkspaceStatValue(selectedStat, observation.value)}</div>
                        <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>{observation.source.replace(/_/g, " ")}</div>
                      </div>
                      <div style={{ fontSize: 11, color: "var(--text-muted)", textAlign: "right" }}>{relativeTime(observation.observed_at)}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={Boolean(statToDelete)}
        onClose={() => {
          if (!deleteMutation.isPending) {
            deleteMutation.reset();
            setStatToDelete(null);
          }
        }}
        onConfirm={() => {
          if (statToDelete) deleteMutation.mutate(statToDelete);
        }}
        title={t("page.workspace_stats.remove_title")}
        message={t("page.workspace_stats.remove_confirmation").replace("{name}", statToDelete?.name || "")}
        confirmLabel={t("page.workspace_stats.remove")}
        cancelLabel={t("action.cancel")}
        danger
        loading={deleteMutation.isPending}
        closeOnConfirm={false}
        error={deleteMutation.isError ? errorMessage(deleteMutation.error) : undefined}
      />
    </GlassCard>
  );
}
