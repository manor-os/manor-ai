import { invalidateCatalogQueries } from "../lib/catalogInvalidation";
import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { agentListQueryOptions, skillListQueryOptions, useCatalogScope } from "../lib/catalogQueries";
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
  IconInfo,
  IconPlus,
  IconRefresh,
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
import Input from "../components/ui/Input";
import { useNavigate } from "react-router-dom";
import { SkillPane } from "./skills/SkillPane";
import { SkillDetailsModal } from "./skills/SkillDetailsModal";
import { CredentialModal } from "./skills/CredentialModal";
import { InvokeModal } from "./skills/InvokeModal";
import { SkillFormModal } from "./skills/SkillFormModal";
import { ImportSkillsDialog } from "./skills/ImportSkillsDialog";

function agentDisplayName(agent: any): string {
  return agent?.name || agent?.agent_name || agent?.agentName || agent?.id || "";
}

function skillDisplayName(skill: any): string {
  return skill?.display_name || skill?.name || skill?.id || "";
}


/* ------------------------------------------------------------------ */
/*  Main component                                                      */
/* ------------------------------------------------------------------ */

type SkillLibrarySource = "all" | "company" | "market" | "manor" | "agent";

/** `embedded`: shown inside Settings › AI, so the header stays in the panel. */
export default function Skills({ embedded = false }: { embedded?: boolean } = {}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const catalogScope = useCatalogScope();
  const toast = useToastStore();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);

  const [tab, setTab] = useState<MainTab>("my");
  const [librarySource, setLibrarySource] = useState<SkillLibrarySource>("all");
  const scope: MyScope = librarySource === "agent" ? "agent" : "entity";
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null);
  const [teachDraft, setTeachDraft] = useState("");
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
    ...skillListQueryOptions(catalogScope),
    enabled: privateApiEnabled && !!catalogScope,
  });


  const { data: agents = [], isLoading: agentsLoading } = useQuery({
    ...agentListQueryOptions(catalogScope),
    enabled: privateApiEnabled && !!catalogScope && scope === "agent",
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
      invalidateCatalogQueries(queryClient, "skills");
      invalidateCatalogQueries(queryClient, "agents");
      toast.success(t("page.skills.skill_deleted"));
    },
  });

  const bindMutation = useMutation({
    mutationFn: ({ agentId, skillId }: { agentId: string; skillId: string }) =>
      api.skills.bindSkill(agentId, skillId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills", "agent"] });
      invalidateCatalogQueries(queryClient, "agents");
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
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
      invalidateCatalogQueries(queryClient, "agents");
      invalidateCatalogQueries(queryClient, "skills", { queryKey: ["skills", "list"] });
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

  const skillsBySource: Record<Exclude<SkillLibrarySource, "agent">, any[]> = {
    all: allSkills as any[],
    company: entitySkills.filter((skill: any) => !skill?.config?.marketplace_id),
    market: entitySkills.filter((skill: any) => !!skill?.config?.marketplace_id),
    manor: (allSkills as any[]).filter((skill: any) => !skill.entity_id),
  };
  const filteredLibrarySkills = (skillsBySource[librarySource === "agent" ? "all" : librarySource]).filter(filterSkill);
  const selectedSkill = filteredLibrarySkills.find((skill: any) => skill.id === selectedSkillId) ?? filteredLibrarySkills[0] ?? null;

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
        padding: 0,
        overflow: "hidden",
        position: "relative",
        zIndex: 10,
      }}
    >
      {/* ── Header ── */}
      <PageHeader
        title={t("nav.skills")}
        subtitle={embedded ? undefined :
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
            trigger={<PageHeaderAddButton label={t("page.skills.add_skill")} caret className="skills-add-button" />}
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
          <form
            className="skills-teach"
            onSubmit={(event) => {
              event.preventDefault();
              const ask = teachDraft.trim();
              if (ask) navigate(`/chat?new=chat&ask=${encodeURIComponent(t("page.skills.teach_prompt", { ask }))}`);
            }}
          >
            <Input
              value={teachDraft}
              onChange={(event) => setTeachDraft(event.target.value)}
              placeholder={t("page.skills.teach_placeholder")}
              ariaLabel={t("page.skills.teach_placeholder")}
            />
          </form>
          <div style={{ marginBottom: 16, flexShrink: 0 }}>
            <TabSwitcher
              appearance="minimal"
              size="sm"
              ariaLabel={t("page.skills.manage_by")}
              tabs={[
                { key: "all", label: t("page.skills.source_all") },
                { key: "company", label: t("page.skills.source_company") },
                ...(skillsBySource.market.length > 0 ? [{ key: "market", label: t("page.skills.source_market") }] : []),
                ...(skillsBySource.manor.length > 0 ? [{ key: "manor", label: t("page.skills.source_manor") }] : []),
                { key: "agent", label: t("page.skills.agent_skills") },
              ]}
              value={librarySource}
              onChange={(key) => setLibrarySource(key as SkillLibrarySource)}
            />
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
                      color: "var(--text-faint)",
                    }}
                  >
                    <LoadingSpinner size={20} />
                    <span style={{ fontSize: 14 }}>
                      <span className="sr-only">{t("page.skills.loading_skills")}</span>
                    </span>
                  </div>
                </div>
              ) : (
                filteredLibrarySkills.length === 0 ? (
                  <div style={{ minHeight: 220, display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
                    <EmptyState
                      icon={<IconSkill size={32} style={{ color: "var(--text-faint)" }} />}
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
                ) : (
                  <div className="agents-master-detail">
                    <nav className="agents-master-list" aria-label={t("nav.skills")}>
                      {filteredLibrarySkills.map((skill: any) => (
                        <button
                          key={skill.id}
                          type="button"
                          className={`agents-master-row${selectedSkill?.id === skill.id ? " is-active" : ""}`}
                          aria-current={selectedSkill?.id === skill.id ? "true" : undefined}
                          onClick={() => setSelectedSkillId(skill.id)}
                        >
                          <SkillIcon skill={skill} size={22} />
                          <span>{skill.name}</span>
                        </button>
                      ))}
                    </nav>
                    <div className="agents-master-detail-pane" style={{ overflowY: "auto" }}>
                      {selectedSkill && (
                        <SkillPane
                          key={selectedSkill.id}
                          skill={selectedSkill}
                          editable={!!selectedSkill.entity_id}
                          onInvoke={skillCardProps.onInvoke}
                          onEdit={skillCardProps.onEdit}
                          onDetails={skillCardProps.onDetails}
                          onCredential={skillCardProps.onCredential}
                          onDelete={skillCardProps.onDelete}
                        />
                      )}
                    </div>
                  </div>
                )
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
                  border: "0.5px solid rgba(29, 29, 31,0.06)",
                  borderRadius: 16,
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
                  gap: 14,
                  alignItems: "end",
                  boxShadow: "0 1px 2px rgba(29, 29, 31,0.02)",
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <label
                    style={{
                      fontSize: 12,
                      fontWeight: 600,
                      color: "var(--text-muted)",
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
                      background: "var(--surface-panel)",
                      borderColor: "rgba(29, 29, 31,0.08)",
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
                      color: "var(--text-faint)",
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
                        color: "var(--text-muted)",
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
                          background: "var(--surface-panel)",
                          borderColor: "rgba(29, 29, 31,0.08)",
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
                      color: "var(--text-faint)",
                    }}
                  >
                    <LoadingSpinner size={14} />
                    <span style={{ fontSize: 12 }}>
                      <span className="sr-only">{t("page.skills.loading_agents")}</span>
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
                        style={{ width: 32, height: 32, color: "var(--text-faint)" }}
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
                        color: "var(--text-faint)",
                      }}
                    >
                      <LoadingSpinner size={20} />
                      <span style={{ fontSize: 14 }}>
                        <span className="sr-only">{t("page.skills.loading_agent_skills")}</span>
                      </span>
                    </div>
                  </div>
                ) : (agentBoundSkills as any[]).length === 0 ? (
                  <EmptyState
                    icon={
                      <svg
                        style={{ width: 32, height: 32, color: "var(--text-faint)" }}
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
          invalidateCatalogQueries(queryClient, "skills")
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
