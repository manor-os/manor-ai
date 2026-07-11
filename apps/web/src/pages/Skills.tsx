import React, { useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { useToastStore } from "../stores/toast";
import { useAuthStore } from "../stores/auth";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import TabSwitcher from "../components/ui/TabSwitcher";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Select from "../components/ui/Select";
import SmartToolbar from "../components/ui/SmartToolbar";
import CompactCard from "../components/ui/CompactCard";
import Chip from "../components/ui/Chip";
import Dropdown from "../components/ui/Dropdown";
import {
  IconCheck,
  IconClose,
  IconInfo,
  IconPlus,
  IconSkill,
  IconUpload,
} from "../components/icons";
import { openDetail, closeDetail } from "../stores/detail";

import {
  MainTab,
  MyScope,
  CATEGORIES,
  THEME_COLORS,
  formatCategory,
  getFallbackColor,
  getSkillDescription,
  getSkillTheme,
} from "./skills/skillTypes";
import { SkillCard, SectionHeader, SkillIcon } from "./skills/SkillCard";
import { CredentialModal } from "./skills/CredentialModal";
import { InvokeModal } from "./skills/InvokeModal";
import { SkillFormModal } from "./skills/SkillFormModal";
import { ImportSkillsDialog } from "./skills/ImportSkillsDialog";

function skillExamples(skill: any): any[] {
  const examples = skill?.examples ?? skill?.config?.examples ?? [];
  return Array.isArray(examples) ? examples : [];
}

function skillExampleScenarios(skill: any): string[] {
  const scenarios =
    skill?.example_scenarios ?? skill?.config?.example_scenarios ?? [];
  return Array.isArray(scenarios) ? scenarios : [];
}

function skillUsageSummary(skill: any): string {
  return skill?.usage_summary ?? skill?.config?.usage_summary ?? "";
}

function skillExtraPaths(skill: any): string[] {
  const paths =
    skill?.extra_file_paths ?? skill?.config?.extra_file_paths ?? [];
  return Array.isArray(paths) ? paths : [];
}

function agentDisplayName(agent: any): string {
  return agent?.name || agent?.agent_name || agent?.agentName || agent?.id || "";
}

function skillDisplayName(skill: any): string {
  return skill?.display_name || skill?.name || skill?.id || "";
}


/* ------------------------------------------------------------------ */
/*  Main component                                                      */
/* ------------------------------------------------------------------ */

export default function Skills() {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);

  const [tab, setTab] = useState<MainTab>("my");
  const [scope, setScope] = useState<MyScope>("entity");
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [search, setSearch] = useState("");

  // Modals
  const [showImportModal, setShowImportModal] = useState(false);
  const [showSkillModal, setShowSkillModal] = useState(false);
  const [editingSkill, setEditingSkill] = useState<any | null>(null);
  const [invokeSkill, setInvokeSkill] = useState<any | null>(null);
  const [credentialSkill, setCredentialSkill] = useState<any | null>(null);
  const [detailSkill, setDetailSkill] = useState<any | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [unbindTarget, setUnbindTarget] = useState<{
    agentId: string;
    skillId: string;
  } | null>(null);
  const [bindSkillId, setBindSkillId] = useState("");


  /* ── Data fetching ── */

  const { data: allSkills = [], isLoading: skillsLoading } = useQuery({
    queryKey: ["skills", "my"],
    queryFn: () => api.skills.list(),
    enabled: privateApiEnabled,
  });


  const { data: agents = [], isLoading: agentsLoading } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
    enabled: privateApiEnabled && scope === "agent",
  });

  const { data: agentBoundSkills = [], isLoading: agentSkillsLoading } =
    useQuery({
      queryKey: ["skills", "agent", selectedAgentId],
      queryFn: () => api.skills.listAgentBindings(selectedAgentId),
      enabled: privateApiEnabled && scope === "agent" && !!selectedAgentId,
    });

  const { data: agentAvailableSkills = [] } = useQuery({
    queryKey: ["skills", "agent-available", selectedAgentId],
    queryFn: () => api.skills.listAgentAvailable(selectedAgentId),
    enabled: privateApiEnabled && scope === "agent" && !!selectedAgentId,
  });

  /* ── Mutations ── */

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.skills.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills"] });
      toast.success(t("page.skills.skill_deleted"));
    },
  });

  const bindMutation = useMutation({
    mutationFn: ({ agentId, skillId }: { agentId: string; skillId: string }) =>
      api.skills.bindSkill(agentId, skillId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills", "agent"] });
      queryClient.invalidateQueries({
        queryKey: ["skills", "agent-available"],
      });
      setBindSkillId("");
      toast.success(t("page.skills.skill_bound"));
    },
  });

  const unbindMutation = useMutation({
    mutationFn: ({ agentId, skillId }: { agentId: string; skillId: string }) =>
      api.skills.unbindSkill(agentId, skillId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills", "agent"] });
      queryClient.invalidateQueries({
        queryKey: ["skills", "agent-available"],
      });
      toast.success(t("page.skills.skill_unbound"));
    },
  });


  /* ── Derived data ── */

  const entitySkills = (allSkills as any[]).filter((s: any) => !!s.entity_id);


  const filterSkill = (s: any) =>
    !search ||
    s.name?.toLowerCase().includes(search.toLowerCase()) ||
    getSkillDescription(s).toLowerCase().includes(search.toLowerCase());

  const filteredEntitySkills = entitySkills.filter(filterSkill);

  const selectedAgent = (agents as any[]).find(
    (a: any) => (a.id || a.agent_id) === selectedAgentId,
  );

  const tabs = [
    {
      key: "my",
      label: t("page.skills.my_skills"),
      count: entitySkills.length,
    },
  ];

  /* ── SkillCard callbacks ── */

  const skillCardProps = {
    onInvoke: (skill: any) => setInvokeSkill(skill),
    onDetails: (skill: any) => setDetailSkill(skill),
    onEdit: (skill: any) => {
      setEditingSkill(skill);
      setShowSkillModal(true);
    },
    onCredential: (skill: any) => setCredentialSkill(skill),
    onDelete: (skillId: string) => setDeleteTarget(skillId),
    onUnbind: (params: { agentId: string; skillId: string }) =>
      setUnbindTarget(params),
    isDeletePending: deleteMutation.isPending,
  };

  /* ================================================================ */
  /*  Render                                                           */
  /* ================================================================ */

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
      {/* ── Header ── */}
      <PageHeader
        title={t("nav.skills")}
        subtitle={
          tab === "my"
            ? scope === "entity"
              ? t("page.skills.subtitle_entity")
              : t("page.skills.subtitle_agent")
            : (
            ""
            )
        }
        tabs={(
          <TabSwitcher
            tabs={tabs}
            value={tab}
            onChange={(k) => setTab(k as MainTab)}
          />
        )}
        toolbar={tab === "my" ? (
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.skills.search_placeholder")}
            className="w-full sm:w-64"
          />
        ) : undefined}
        actions={tab === "my" && scope === "entity" ? (
          <Dropdown
            align="right"
            trigger={<PageHeaderAddButton label={t("page.skills.add_skill")} caret />}
            items={[
              { key: "create", label: t("page.skills.create_skill"), icon: <IconPlus size={14} /> },
              { key: "import", label: t("page.skills.import_skills"), icon: <IconUpload size={14} /> },
            ]}
            onSelect={(key) => {
              if (key === "create") {
                setEditingSkill(null);
                setShowSkillModal(true);
              } else if (key === "import") {
                setShowImportModal(true);
              }
            }}
          />
        ) : undefined}
      />

      {/* ════════════════════════════════════════════════════════════ */}
      {/*  MY SKILLS TAB                                              */}
      {/* ════════════════════════════════════════════════════════════ */}
      {tab === "my" && (
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
          }}
        >
          {/* Info banner */}
          <div
            style={{
              marginBottom: 14,
              flexShrink: 0,
              padding: "14px 18px",
              background:
                "linear-gradient(135deg, rgba(28,25,23,0.06), rgba(28,25,23,0.02))",
              border: "1px solid rgba(28,25,23,0.18)",
              borderRadius: 14,
              display: "flex",
              alignItems: "flex-start",
              gap: 12,
            }}
          >
            <svg
              style={{
                width: 18,
                height: 18,
                color: "#57534e",
                flexShrink: 0,
                marginTop: 1,
              }}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
            <div>
              <p
                style={{
                  margin: 0,
                  fontSize: 13,
                  fontWeight: 700,
                  color: "#292524",
                }}
              >
                {t("page.skills.entity_subagent_binding")}
              </p>
              <p style={{ margin: "3px 0 0", fontSize: 12, color: "#78716c" }}>
                {t("page.skills.entity_subagent_desc")}
              </p>
            </div>
          </div>

          {/* Scope switcher */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              marginBottom: 16,
              flexShrink: 0,
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 600, color: "#78716c" }}>
              {t("page.skills.manage_by")}:
            </span>
            {(["entity", "agent"] as MyScope[]).map((s) => (
              <button
                key={s}
                onClick={() => setScope(s)}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "8px 18px",
                  borderRadius: 11,
                  border: "1px solid transparent",
                  background: scope === s ? "#1c1917" : "rgba(255,255,255,0.7)",
                  color: scope === s ? "#fff" : "#57534e",
                  fontSize: 12,
                  fontWeight: 700,
                  cursor: "pointer",
                  transition: "all 0.2s",
                  boxShadow:
                    scope === s ? "0 4px 12px rgba(28,25,23,0.3)" : "none",
                }}
              >
                {s === "entity" ? (
                  <svg
                    style={{ width: 13, height: 13 }}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"
                    />
                  </svg>
                ) : (
                  <svg
                    style={{ width: 13, height: 13 }}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                    />
                  </svg>
                )}
                {s === "entity"
                  ? t("page.skills.entity_skills")
                  : t("page.skills.agent_skills")}
              </button>
            ))}
          </div>

          {/* ── ENTITY SCOPE ── */}
          {scope === "entity" && (
            <div style={{ flex: 1, overflowY: "auto", padding: "4px 2px" }}>
              {skillsLoading ? (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    padding: "64px 0",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      color: "#a8a29e",
                    }}
                  >
                    <LoadingSpinner size={20} />
                    <span style={{ fontSize: 14 }}>
                      {t("page.skills.loading_skills")}
                    </span>
                  </div>
                </div>
              ) : (
                <div>
                  <SectionHeader
                    title={t("page.skills.entity_skills")}
                    count={entitySkills.length}
                    accent="#6d6fb2"
                  />
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns:
                        "repeat(auto-fill, minmax(min(100%, 240px), 1fr))",
                      gap: 18,
                    }}
                  >
                    {filteredEntitySkills.map((skill: any) => (
                      <SkillCard
                        key={skill.id}
                        skill={skill}
                        {...skillCardProps}
                      />
                    ))}

                    {filteredEntitySkills.length === 0 && (
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
                          icon={<IconSkill size={32} style={{ color: "#d6d3d1" }} />}
                          title={
                            entitySkills.length === 0
                              ? t("page.skills.no_entity_skills")
                              : t("page.skills.no_matching_entity_skills")
                          }
                          description={
                            entitySkills.length === 0
                              ? t("page.skills.no_entity_skills_desc")
                              : t("page.skills.no_matching_entity_skills_desc")
                          }
                        />
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ── AGENT SCOPE ── */}
          {scope === "agent" && (
            <div
              style={{
                flex: 1,
                display: "flex",
                flexDirection: "column",
                minHeight: 0,
              }}
            >
              {/* Agent selector + bind panel */}
              <div
                style={{
                  marginBottom: 16,
                  flexShrink: 0,
                  padding: "14px 16px",
                  background: "rgba(255,255,255,0.72)",
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 16,
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
                  gap: 14,
                  alignItems: "end",
                  boxShadow: "0 1px 2px rgba(28,25,23,0.02)",
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <label
                    style={{
                      fontSize: 12,
                      fontWeight: 600,
                      color: "#78716c",
                      display: "block",
                      marginBottom: 6,
                    }}
                  >
                    {t("page.skills.select_subagent")}
                  </label>
                  <Select
                    value={selectedAgentId}
                    onChange={(value) => {
                      setSelectedAgentId(value);
                      setBindSkillId("");
                    }}
                    options={(agents as any[]).map((agent: any) => ({
                      value: agent.id || agent.agent_id,
                      label: agentDisplayName(agent),
                    }))}
                    placeholder={
                      agentsLoading
                        ? t("page.skills.loading_agents")
                        : t("page.skills.choose_agent")
                    }
                    filterable
                    dropdownMinWidth={320}
                    buttonStyle={{
                      minHeight: 40,
                      background: "#fff",
                      borderColor: "rgba(28,25,23,0.08)",
                    }}
                    openButtonStyle={{
                      borderColor: "#4f7d75",
                    }}
                  />
                </div>

                {!selectedAgentId && !agentsLoading && (
                  <div
                    style={{
                      minHeight: 40,
                      display: "flex",
                      alignItems: "center",
                      color: "#a8a29e",
                      fontSize: 13,
                      lineHeight: 1.5,
                    }}
                  >
                    {t("page.skills.select_agent_desc")}
                  </div>
                )}

                {selectedAgentId && (
                  <div style={{ minWidth: 0 }}>
                    <label
                      style={{
                        fontSize: 12,
                        fontWeight: 600,
                        color: "#78716c",
                        display: "block",
                        marginBottom: 6,
                      }}
                    >
                      {t("page.skills.bind_entity_skill")}
                    </label>
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "minmax(220px, 1fr) auto",
                        gap: 8,
                        alignItems: "center",
                      }}
                    >
                      <Select
                        value={bindSkillId}
                        onChange={setBindSkillId}
                        options={(agentAvailableSkills as any[]).map((skill: any) => ({
                          value: skill.id,
                          label: skillDisplayName(skill),
                        }))}
                        placeholder={
                          (agentAvailableSkills as any[]).length === 0
                            ? t("page.skills.no_entity_skills_bind")
                            : t("page.skills.select_skill_bind")
                        }
                        filterable
                        dropdownMinWidth={320}
                        buttonStyle={{
                          minHeight: 40,
                          background: "#fff",
                          borderColor: "rgba(28,25,23,0.08)",
                        }}
                        openButtonStyle={{
                          borderColor: "#4f7d75",
                        }}
                      />
                      <Button
                        variant="primary"
                        size="sm"
                        disabled={!bindSkillId || bindMutation.isPending}
                        onClick={() =>
                          bindSkillId &&
                          bindMutation.mutate({
                            agentId: selectedAgentId,
                            skillId: bindSkillId,
                          })
                        }
                      >
                        {t("page.skills.bind")}
                      </Button>
                    </div>
                  </div>
                )}

                {agentsLoading && (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      color: "#a8a29e",
                    }}
                  >
                    <LoadingSpinner size={14} />
                    <span style={{ fontSize: 12 }}>
                      {t("page.skills.loading_agents")}
                    </span>
                  </div>
                )}
              </div>

              {/* Agent skills grid */}
              <div style={{ flex: 1, overflowY: "auto", padding: "4px 2px" }}>
                {!selectedAgentId ? (
                  <EmptyState
                    icon={
                      <svg
                        style={{ width: 32, height: 32, color: "#d6d3d1" }}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth={1}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                        />
                      </svg>
                    }
                    title={t("page.skills.select_agent")}
                    description={t("page.skills.select_agent_desc")}
                  />
                ) : agentSkillsLoading ? (
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      padding: "64px 0",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 12,
                        color: "#a8a29e",
                      }}
                    >
                      <LoadingSpinner size={20} />
                      <span style={{ fontSize: 14 }}>
                        {t("page.skills.loading_agent_skills")}
                      </span>
                    </div>
                  </div>
                ) : (agentBoundSkills as any[]).length === 0 ? (
                  <EmptyState
                    icon={
                      <svg
                        style={{ width: 32, height: 32, color: "#d6d3d1" }}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth={1}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M12 18v-5.25m0 0a6.01 6.01 0 001.5-.189m-1.5.189a6.01 6.01 0 01-1.5-.189m3.75 7.478a12.06 12.06 0 01-4.5 0m3.75 2.383a14.406 14.406 0 01-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 10-7.517 0c.85.493 1.509 1.333 1.509 2.316V18"
                        />
                      </svg>
                    }
                    title={t("page.skills.no_skills_bound")}
                    description={`${selectedAgent?.name || t("page.skills.this_agent")} ${t("page.skills.no_skills_bound_desc")}`}
                  />
                ) : (
                  <>
                    <SectionHeader
                      title={`${t("page.skills.skills_bound_to")} ${selectedAgent?.name || selectedAgent?.agentName || t("page.skills.agent")}`}
                      count={(agentBoundSkills as any[]).length}
                      accent="#6d6fb2"
                    />
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns:
                          "repeat(auto-fill, minmax(min(100%, 240px), 1fr))",
                        gap: 18,
                      }}
                    >
                      {(agentBoundSkills as any[]).map((skill: any) => (
                        <SkillCard
                          key={skill.id}
                          skill={skill}
                          isAgentView
                          selectedAgentId={selectedAgentId}
                          {...skillCardProps}
                        />
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      )}


      {/* ════════════════════════════════════════════════════════════ */}
      {/*  Dialogs                                                    */}
      {/* ════════════════════════════════════════════════════════════ */}

      <ImportSkillsDialog
        open={showImportModal}
        onClose={() => setShowImportModal(false)}
        onImported={() =>
          queryClient.invalidateQueries({ queryKey: ["skills"] })
        }
      />

      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget);
          setDeleteTarget(null);
        }}
        title={t("page.skills.delete_skill")}
        message={t("page.skills.delete_skill_message")}
        confirmLabel={t("action.delete")}
        danger
      />

      <SkillDetailsModal
        skill={detailSkill}
        onClose={() => setDetailSkill(null)}
      />

      <ConfirmDialog
        open={!!unbindTarget}
        onClose={() => setUnbindTarget(null)}
        onConfirm={() => {
          if (unbindTarget) unbindMutation.mutate(unbindTarget);
          setUnbindTarget(null);
        }}
        title={t("page.skills.unbind_skill")}
        message={t("page.skills.unbind_skill_message")}
        confirmLabel={t("page.skills.unbind")}
        danger
      />

      <SkillFormModal
        skill={editingSkill}
        open={showSkillModal}
        onClose={() => {
          setShowSkillModal(false);
          setEditingSkill(null);
        }}
      />

      <InvokeModal
        skill={invokeSkill}
        open={!!invokeSkill}
        onClose={() => setInvokeSkill(null)}
      />

      <CredentialModal
        skill={credentialSkill}
        open={!!credentialSkill}
        onClose={() => setCredentialSkill(null)}
      />
    </div>
  );
}

function SkillDetailsModal({
  skill,
  onClose,
  onImport,
  importing,
  subscribed,
}: {
  skill: any | null;
  onClose: () => void;
  onImport?: () => void;
  importing?: boolean;
  subscribed?: boolean;
}) {
  if (!skill) return null;

  const examples = skillExamples(skill);
  const scenarios = skillExampleScenarios(skill);
  const usage = skillUsageSummary(skill);
  const paths = skillExtraPaths(skill);
  const title =
    skill.display_name || skill.name || skill.skill_name || t("page.skills.skill");
  const description = getSkillDescription(skill);

  return createPortal(
    <div
      className="manor-dialog-overlay"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 20000,
        background: "var(--modal-overlay-bg)",
        backdropFilter: "blur(5px)",
        WebkitBackdropFilter: "blur(5px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
      }}
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="manor-dialog skill-details-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(760px, 100%)",
          maxHeight: "86vh",
          overflow: "hidden",
          background: "var(--modal-bg)",
          backdropFilter: "blur(20px) saturate(1.08)",
          WebkitBackdropFilter: "blur(20px) saturate(1.08)",
          borderRadius: 18,
          boxShadow: "var(--modal-shadow)",
          border: "1px solid var(--modal-border)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          style={{
            padding: "18px 20px",
            borderBottom: "1px solid var(--modal-border)",
            display: "flex",
            justifyContent: "space-between",
            gap: 16,
          }}
        >
          <div style={{ minWidth: 0 }}>
            <p
              style={{
                margin: "0 0 4px",
                fontSize: 11,
                fontWeight: 800,
                color: "var(--text-muted)",
                textTransform: "uppercase",
                letterSpacing: "0.08em",
              }}
            >
              {t("page.skills.skill_details")}
            </p>
            <h2
              style={{
                margin: 0,
                fontSize: 20,
                lineHeight: 1.25,
                color: "var(--text-strong)",
                wordBreak: "break-word",
              }}
            >
              {title}
            </h2>
          </div>
          <button
            onClick={onClose}
            title={t("page.flows.close")}
            style={{
              width: 34,
              height: 34,
              borderRadius: "50%",
              border: "1px solid var(--modal-border)",
              background: "var(--modal-muted-bg)",
              color: "var(--text-muted)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              flexShrink: 0,
            }}
          >
            <IconClose size={16} />
          </button>
        </div>

        <div style={{ overflowY: "auto", padding: 20 }}>
          {description && (
            <p
              style={{ margin: "0 0 16px", color: "var(--text-muted)", lineHeight: 1.6 }}
            >
              {description}
            </p>
          )}

          {usage && (
            <DetailSection title={t("page.skills.how_to_use")}>
              <p style={{ margin: 0, color: "var(--text-default)", lineHeight: 1.6 }}>
                {usage}
              </p>
            </DetailSection>
          )}

          {scenarios.length > 0 && (
            <DetailSection title={t("page.skills.example_scenarios")}>
              <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-default)" }}>
                {scenarios.map((scenario) => (
                  <li
                    key={scenario}
                    style={{ marginBottom: 6, lineHeight: 1.5 }}
                  >
                    {scenario}
                  </li>
                ))}
              </ul>
            </DetailSection>
          )}

          {examples.length > 0 && (
            <DetailSection title={t("page.skills.examples_folder")}>
              <div style={{ display: "grid", gap: 10 }}>
                {examples.map((example: any) => (
                  <div
                    key={example.path || example.title}
                    style={{
                      border: "1px solid var(--modal-border)",
                      borderRadius: 10,
                      overflow: "hidden",
                      background: "var(--modal-muted-bg)",
                    }}
                  >
                    <div
                      style={{
                        padding: "9px 11px",
                        borderBottom: "1px solid var(--modal-border)",
                        background: "var(--modal-sunken-bg)",
                        display: "flex",
                        justifyContent: "space-between",
                        gap: 12,
                      }}
                    >
                      <strong style={{ fontSize: 13, color: "var(--text-strong)" }}>
                        {example.title || example.path}
                      </strong>
                      {example.path && (
                        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
                          {example.path}
                        </span>
                      )}
                    </div>
                    <pre
                      style={{
                        margin: 0,
                        padding: 12,
                        maxHeight: 220,
                        overflow: "auto",
                        whiteSpace: "pre-wrap",
                        fontSize: 12,
                        lineHeight: 1.55,
                        color: "var(--text-default)",
                        fontFamily:
                          "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                      }}
                    >
                      {(example.content || "").slice(0, 3200)}
                    </pre>
                  </div>
                ))}
              </div>
            </DetailSection>
          )}

          {paths.length > 0 && (
            <DetailSection title={t("page.skills.bundled_files")}>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {paths.slice(0, 24).map((path) => (
                  <span
                    key={path}
                    style={{
                      fontSize: 11,
                      color: "var(--text-muted)",
                      background: "var(--modal-muted-bg)",
                      border: "1px solid var(--modal-border)",
                      borderRadius: 7,
                      padding: "3px 7px",
                    }}
                  >
                    {path}
                  </span>
                ))}
              </div>
            </DetailSection>
          )}
        </div>

        <div
          style={{
            padding: "14px 20px",
            borderTop: "1px solid var(--modal-border)",
            display: "flex",
            justifyContent: "flex-end",
            gap: 10,
          }}
        >
          <Button variant="outline" size="sm" onClick={onClose}>
            {t("page.flows.close")}
          </Button>
          {onImport && (subscribed ? (
            <Button
              variant="outline"
              size="sm"
              disabled
              style={{
                color: "#44895f",
                borderColor: "#bbf7d0",
                background: "var(--accent-soft)",
              }}
            >
              <IconCheck size={12} />
              {t("page.skills.subscribed")}
            </Button>
          ) : (
            <Button
              variant="primary"
              size="sm"
              onClick={onImport}
              loading={importing}
            >
              <IconPlus size={12} />
              {t("page.skills.subscribe")}
            </Button>
          ))}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function DetailSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section style={{ marginBottom: 18 }}>
      <h3
        style={{
          margin: "0 0 8px",
          fontSize: 13,
          color: "#1c1917",
          fontWeight: 800,
        }}
      >
        {title}
      </h3>
      {children}
    </section>
  );
}
