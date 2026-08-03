import type { AgentLearningCandidate, RuntimeEvidence } from "../lib/types";
import { relativeTime } from "../lib/format";
import { t } from "../lib/i18n";
import Button from "./ui/Button";
import Chip from "./ui/Chip";
import EmptyState from "./ui/EmptyState";
import GlassCard from "./ui/GlassCard";
import LoadingSpinner from "./ui/LoadingSpinner";
import StatusBadge from "./ui/StatusBadge";
import Toggle from "./ui/Toggle";

enum LearningCandidateStatus {
  Proposed = "proposed",
  Accepted = "accepted",
  Rejected = "rejected",
  Applied = "applied",
  Archived = "archived",
}

enum LearningCandidateType {
  Memory = "memory",
  Skill = "skill",
  Rule = "rule",
  ToolExperience = "tool_experience",
  AgentProfilePatch = "agent_profile_patch",
  ProfilePatch = "profile_patch",
}

enum RuntimeEvidenceType {
  ChatRun = "chat_run",
  TaskRun = "task_run",
  ToolSummary = "tool_summary",
  UserFeedback = "user_feedback",
  StrategistReview = "strategist_review",
  LearningApply = "learning_apply",
}

interface AgentLearningPanelProps {
  candidates: AgentLearningCandidate[];
  evidence: RuntimeEvidence[];
  enabled: boolean;
  isUpdating: boolean;
  isLoading: boolean;
  error?: unknown;
  workspaceNames: Record<string, string>;
  onToggleEnabled: () => void;
  onAdjustAgent: () => void;
}

function humanize(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) return t("page.agent_detail.learning_unknown");
  return text
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function candidateTypeLabel(value: string): string {
  switch (value as LearningCandidateType) {
    case LearningCandidateType.Memory:
      return t("page.agent_detail.learning_type_memory");
    case LearningCandidateType.Skill:
      return t("page.agent_detail.learning_type_skill");
    case LearningCandidateType.Rule:
      return t("page.agent_detail.learning_type_rule");
    case LearningCandidateType.ToolExperience:
      return t("page.agent_detail.learning_type_tool_experience");
    case LearningCandidateType.AgentProfilePatch:
    case LearningCandidateType.ProfilePatch:
      return t("page.agent_detail.learning_type_profile");
    default:
      return humanize(value);
  }
}

function evidenceTypeLabel(value: string): string {
  switch (value as RuntimeEvidenceType) {
    case RuntimeEvidenceType.ChatRun:
      return t("page.agent_detail.learning_evidence_chat");
    case RuntimeEvidenceType.TaskRun:
      return t("page.agent_detail.learning_evidence_task");
    case RuntimeEvidenceType.ToolSummary:
      return t("page.agent_detail.learning_evidence_tool");
    case RuntimeEvidenceType.UserFeedback:
      return t("page.agent_detail.learning_evidence_feedback");
    case RuntimeEvidenceType.StrategistReview:
      return t("page.agent_detail.learning_evidence_review");
    case RuntimeEvidenceType.LearningApply:
      return t("page.agent_detail.learning_evidence_apply");
    default:
      return humanize(value);
  }
}

function isVisibleLearning(value: string): boolean {
  switch (value as LearningCandidateStatus) {
    case LearningCandidateStatus.Proposed:
    case LearningCandidateStatus.Accepted:
    case LearningCandidateStatus.Applied:
      return true;
    case LearningCandidateStatus.Rejected:
    case LearningCandidateStatus.Archived:
      return false;
    default:
      return true;
  }
}

function evidenceStatusType(value: string): string {
  switch (value) {
    case "succeeded":
    case "completed":
      return "success";
    case "failed":
      return "danger";
    case "blocked":
    case "partial":
      return "warning";
    default:
      return "inactive";
  }
}

function candidatePreview(candidate: AgentLearningCandidate): string {
  const payload = candidate.payload || {};
  let preview = "";
  switch (candidate.candidate_type as LearningCandidateType) {
    case LearningCandidateType.Memory:
    case LearningCandidateType.Rule:
      preview = String(payload.content || "");
      break;
    case LearningCandidateType.Skill:
      preview = String(payload.seed_prompt || "");
      break;
    case LearningCandidateType.AgentProfilePatch:
    case LearningCandidateType.ProfilePatch:
      preview = String(payload.profile_update || payload.content || "");
      if (!preview && Array.isArray(payload.target_files)) {
        preview = `${t("page.agent_detail.learning_adjusts")}: ${payload.target_files.join(" · ")}`;
      }
      break;
    case LearningCandidateType.ToolExperience:
      preview = Array.isArray(payload.meaningful_tools)
        ? payload.meaningful_tools.join(" → ")
        : "";
      break;
    default:
      preview = String(payload.content || payload.seed_prompt || payload.profile_update || "");
  }
  return preview.trim().slice(0, 360);
}

function workspaceLabel(workspaceId: string | null | undefined, names: Record<string, string>): string {
  if (!workspaceId) return t("page.agent_detail.learning_agent_scope");
  return names[workspaceId] || t("page.agent_detail.learning_workspace_scope");
}

function CandidateRow({
  candidate,
  workspaceNames,
}: {
  candidate: AgentLearningCandidate;
  workspaceNames: Record<string, string>;
}) {
  const preview = candidatePreview(candidate);
  return (
    <article
      style={{
        padding: "14px 0",
        borderBottom: "1px solid rgba(28,25,23,0.06)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <Chip variant="slate" size="sm">{candidateTypeLabel(candidate.candidate_type)}</Chip>
          <Chip variant="slate" size="sm">
            {Math.round((candidate.confidence || 0) * 100)}% {t("page.agent_detail.learning_confidence")}
          </Chip>
        </div>
        <span className="mono" style={{ color: "var(--text-faint)", fontSize: 11 }}>
          {relativeTime(candidate.applied_at || candidate.updated_at || candidate.created_at)}
        </span>
      </div>
      <h4 style={{ margin: "10px 0 0", color: "var(--text-strong)", fontSize: 14, fontWeight: 750, lineHeight: 1.4 }}>
        {candidate.title}
      </h4>
      {preview && (
        <div style={{ marginTop: 10, padding: "10px 12px", borderRadius: "var(--radius-control)", background: "var(--surface-muted)", color: "var(--text-default)", fontSize: 12, lineHeight: 1.55, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {preview}
        </div>
      )}
      <div style={{ marginTop: 10, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
          {workspaceLabel(candidate.workspace_id, workspaceNames)}
        </span>
      </div>
    </article>
  );
}

function CandidateSection({
  title,
  description,
  candidates,
  emptyTitle,
  emptyDescription,
  workspaceNames,
}: {
  title: string;
  description: string;
  candidates: AgentLearningCandidate[];
  emptyTitle: string;
  emptyDescription: string;
  workspaceNames: Record<string, string>;
}) {
  return (
    <GlassCard hoverable={false} style={{ padding: 20 }}>
      <div style={{ marginBottom: candidates.length ? 2 : 14 }}>
        <h3 style={{ margin: 0, color: "var(--text-strong)", fontSize: 14, fontWeight: 750 }}>{title}</h3>
        <p style={{ margin: "5px 0 0", color: "var(--text-muted)", fontSize: 12.5, lineHeight: 1.5 }}>{description}</p>
      </div>
      {candidates.length ? (
        <div>
          {candidates.map((candidate) => (
            <CandidateRow
              key={candidate.id}
              candidate={candidate}
              workspaceNames={workspaceNames}
            />
          ))}
        </div>
      ) : (
        <EmptyState title={emptyTitle} description={emptyDescription} />
      )}
    </GlassCard>
  );
}

export default function AgentLearningPanel({
  candidates,
  evidence,
  enabled,
  isUpdating,
  isLoading,
  error,
  workspaceNames,
  onToggleEnabled,
  onAdjustAgent,
}: AgentLearningPanelProps) {
  if (isLoading) {
    return <div style={{ display: "grid", minHeight: 260, placeItems: "center" }}><LoadingSpinner /></div>;
  }

  if (error) {
    return (
      <GlassCard hoverable={false} style={{ padding: 20 }}>
        <EmptyState
          title={t("page.agent_detail.learning_load_failed")}
          description={t("page.agent_detail.learning_load_failed_desc")}
        />
      </GlassCard>
    );
  }

  const learnedItems = candidates.filter((candidate) => isVisibleLearning(candidate.status));
  const learningSources = new Set(
    learnedItems.map((candidate) => candidate.workspace_id || "agent"),
  ).size;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <GlassCard hoverable={false} style={{ padding: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
          <div style={{ minWidth: 0, maxWidth: 760 }}>
            <h3 style={{ margin: 0, color: "var(--text-strong)", fontSize: 15, fontWeight: 800 }}>
              {t("page.agent_detail.learning_what_it_learned")}
            </h3>
            <p style={{ margin: "6px 0 0", color: "var(--text-muted)", fontSize: 13, lineHeight: 1.6 }}>
              {t("page.agent_detail.learning_explainer")}
            </p>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <Button size="sm" variant="outline" onClick={onAdjustAgent}>
              {t("page.agent_detail.learning_adjust_agent")}
            </Button>
            <div style={{ display: "flex", alignItems: "center", gap: 9, minHeight: 32 }}>
              <span style={{ color: "var(--text-default)", fontSize: 12.5, fontWeight: 650 }}>
                {enabled ? t("page.agent_detail.learning_on") : t("page.agent_detail.learning_paused")}
              </span>
              <Toggle
                checked={enabled}
                onChange={onToggleEnabled}
                disabled={isUpdating}
                aria-label={t("page.agent_detail.runtime_learning")}
              />
            </div>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 140px), 1fr))", gap: 10, marginTop: 16 }}>
          {[
            [t("page.agent_detail.learning_items_count"), learnedItems.length],
            [t("page.agent_detail.learning_sources_count"), learningSources],
            [t("page.agent_detail.learning_evidence_count"), evidence.length],
          ].map(([label, value]) => (
            <div key={String(label)} style={{ padding: "10px 12px", borderRadius: "var(--radius-control)", background: "var(--surface-muted)" }}>
              <div style={{ color: "var(--text-faint)", fontSize: 10, fontWeight: 700, letterSpacing: "0.03em", textTransform: "uppercase" }}>{label}</div>
              <div className="mono" style={{ marginTop: 4, color: "var(--text-strong)", fontSize: 18, fontWeight: 750 }}>{value}</div>
            </div>
          ))}
        </div>
      </GlassCard>

      <CandidateSection
        title={t("page.agent_detail.learning_items")}
        description={t("page.agent_detail.learning_items_desc")}
        candidates={learnedItems}
        emptyTitle={t("page.agent_detail.learning_no_items")}
        emptyDescription={t("page.agent_detail.learning_no_items_desc")}
        workspaceNames={workspaceNames}
      />

      <GlassCard hoverable={false} style={{ padding: 20 }}>
        <div style={{ marginBottom: evidence.length ? 4 : 14 }}>
          <h3 style={{ margin: 0, color: "var(--text-strong)", fontSize: 14, fontWeight: 750 }}>
            {t("page.agent_detail.learning_recent_evidence")}
          </h3>
          <p style={{ margin: "5px 0 0", color: "var(--text-muted)", fontSize: 12.5, lineHeight: 1.5 }}>
            {t("page.agent_detail.learning_recent_evidence_desc")}
          </p>
        </div>
        {evidence.length ? (
          <div>
            {evidence.map((item) => {
              const metrics = [
                item.metrics?.total_tokens ? `${item.metrics.total_tokens.toLocaleString()} ${t("page.agent_detail.learning_tokens")}` : "",
                item.metrics?.rounds ? `${item.metrics.rounds} ${t("page.agent_detail.learning_rounds")}` : "",
                item.metrics?.tool_call_count ? `${item.metrics.tool_call_count} ${t("page.agent_detail.learning_tools")}` : "",
              ].filter(Boolean).join(" · ");
              return (
                <article key={item.id} style={{ padding: "13px 0", borderBottom: "1px solid rgba(28,25,23,0.06)" }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <StatusBadge type={evidenceStatusType(item.status)} dot>{humanize(item.status)}</StatusBadge>
                      <Chip variant="slate" size="sm">{evidenceTypeLabel(item.evidence_type)}</Chip>
                      <Chip variant="slate" size="sm">{workspaceLabel(item.workspace_id, workspaceNames)}</Chip>
                    </div>
                    <span className="mono" style={{ color: "var(--text-faint)", fontSize: 11 }}>{relativeTime(item.created_at)}</span>
                  </div>
                  <p style={{ margin: "8px 0 0", color: "var(--text-default)", fontSize: 12.5, fontWeight: 600, lineHeight: 1.5 }}>{item.summary}</p>
                  {metrics && <p className="mono" style={{ margin: "5px 0 0", color: "var(--text-faint)", fontSize: 11 }}>{metrics}</p>}
                </article>
              );
            })}
          </div>
        ) : (
          <EmptyState
            title={t("page.agent_detail.learning_no_evidence")}
            description={t("page.agent_detail.learning_no_evidence_desc")}
          />
        )}
      </GlassCard>
    </div>
  );
}
