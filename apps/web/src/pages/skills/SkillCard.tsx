import React, { useState } from "react";
import {
  IconEdit,
  IconTrash,
  IconPlay,
  IconKey,
  IconWarning,
  IconCheckCircle,
  IconInfo,
} from "../../components/icons";
import Card from "../../components/ui/Card";
import CompactCard from "../../components/ui/CompactCard";
import Button from "../../components/ui/Button";
import Chip from "../../components/ui/Chip";
import StatusBadge from "../../components/ui/StatusBadge";
import SkillIcon from "../../components/ui/SkillIcon";
import { openDetail, closeDetail } from "../../stores/detail";
import {
  formatCategory,
  getSkillDescription,
  skillNeedsCredentials,
} from "./skillTypes";
import { t } from "../../lib/i18n";
export { SkillIcon };

/** `/slash` trigger used to invoke the skill (Codex / Manus style). */
function skillTrigger(name: string): string {
  const slug = (name || "")
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `/${slug || "skill"}`;
}

type BindingContext = Record<string, any>;

function skillBindingContexts(skill: any): BindingContext[] {
  const contexts = skill?.bindings;
  return Array.isArray(contexts)
    ? contexts.filter((item: any) => item && typeof item === "object")
    : [];
}

function detailedBindingLabel(context: BindingContext): string {
  const parts = [
    context.agent_name ? `Agent ${context.agent_name}` : null,
    context.workspace_name ? `Workspace ${context.workspace_name}` : null,
    context.automation_name || context.automation_id
      ? `Automation ${context.automation_name || context.automation_id}`
      : null,
  ].filter(Boolean);
  return parts.join(" · ")
    || String(context.source || context.binding_type || "binding").replace(/_/g, " ");
}

function bindingSourceLabel(context: BindingContext): string {
  const source = String(context.source || context.binding_type || "binding").replace(/_/g, " ");
  const match = context.match?.type
    ? String(context.match.type).replace(/_/g, " ")
    : "";
  return match ? `${source} · ${match}` : source;
}

/* ------------------------------------------------------------------ */
/*  SectionHeader                                                       */
/* ------------------------------------------------------------------ */

export function SectionHeader({
  title,
  count,
}: {
  title: string;
  count?: number;
  /** @deprecated no longer rendered — kept for call-site compatibility */
  accent?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginBottom: 16,
        marginTop: 8,
      }}
    >
      <span
        style={{
          fontSize: 13,
          fontWeight: 800,
          color: "#44403c",
          letterSpacing: "-0.01em",
        }}
      >
        {title}
      </span>
      {count !== undefined && (
        <span
          style={{
            fontSize: 11,
            fontWeight: 700,
            color: "#a8a29e",
            background: "#f5f5f4",
            padding: "2px 8px",
            borderRadius: 999,
          }}
        >
          {count}
        </span>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  ActionBtn                                                           */
/* ------------------------------------------------------------------ */

export function ActionBtn({
  id,
  color,
  danger,
  title,
  onClick,
  disabled,
  children,
}: {
  id: string;
  color: string;
  danger?: boolean;
  title: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  const [isH, setIsH] = useState(false);
  const borderColor = danger
    ? isH
      ? "#ddafac"
      : "#f5f5f4"
    : isH
      ? color
      : "#f5f5f4";
  return (
    <button
      onMouseEnter={() => setIsH(true)}
      onMouseLeave={() => setIsH(false)}
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        width: 32,
        height: 32,
        borderRadius: "50%",
        background: isH ? "#fff" : "#fafaf9",
        border: `1px solid ${borderColor}`,
        color: isH ? color : "#a8a29e",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        cursor: disabled ? "not-allowed" : "pointer",
        transition: "all 0.2s",
        flexShrink: 0,
        boxShadow: isH ? `0 0 0 3px ${color}22` : "none",
        transform: isH ? "scale(1.08)" : "none",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {children}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/*  SkillCard                                                           */
/* ------------------------------------------------------------------ */

export interface SkillCardProps {
  key?: React.Key;
  skill: any;
  isAgentView?: boolean;
  selectedAgentId?: string;
  onInvoke: (skill: any) => void;
  onEdit: (skill: any) => void;
  onCredential: (skill: any) => void;
  onDetails: (skill: any) => void;
  onDelete: (skillId: string) => void;
  onUnbind: (params: { agentId: string; skillId: string }) => void;
  isDeletePending?: boolean;
}

export function SkillCard({
  skill,
  isAgentView,
  selectedAgentId,
  onInvoke,
  onEdit,
  onCredential,
  onDetails,
  onDelete,
  onUnbind,
  isDeletePending,
}: SkillCardProps) {
  const id = isAgentView ? `agent-${skill.id}` : skill.id;
  const needsCreds = skillNeedsCredentials(skill);
  const credsMissing = needsCreds && !skill.credentials_configured;
  const description = getSkillDescription(skill);
  const trigger = skillTrigger(skill.name);
  const bindingContexts = skillBindingContexts(skill);

  return (
    <CompactCard
      icon={<SkillIcon skill={skill} size={34} />}
      title={skill.name}
      subtitle={
        <>
          <span className="mono" style={{ color: "var(--accent)", fontWeight: 600 }}>
            {trigger}
          </span>
          {description ? (
            <span style={{ color: "var(--text-faint)" }}>{"  ·  "}</span>
          ) : null}
          {description || (skill.category ? formatCategory(skill.category) : "")}
        </>
      }
      meta={
        needsCreds ? (
          <span
            title={
              credsMissing
                ? t("page.skill_card.credentials_needed")
                : t("page.skill_card.credentials_configured")
            }
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: "currentColor",
            }}
          />
        ) : undefined
      }
      metaTone={needsCreds && !credsMissing ? "connected" : "muted"}
      onClick={() =>
        openDetail({
          icon: <SkillIcon skill={skill} size={48} />,
          title: skill.name,
          subtitle: [
            skill.version ? `${t("page.skill_card.v")}${skill.version}` : null,
            skill.category ? formatCategory(skill.category) : null,
          ]
            .filter(Boolean)
            .join(" · "),
          badges: (
            <>
              <Chip variant="slate" size="sm">
                <span className="mono">{trigger}</span>
              </Chip>
              {needsCreds && (
                <StatusBadge type={credsMissing ? "warning" : "success"}>
                  {credsMissing ? (
                    <IconWarning size={11} />
                  ) : (
                    <IconCheckCircle size={11} />
                  )}
                  {credsMissing
                    ? t("page.skill_card.credentials_needed")
                    : t("page.skill_card.credentials_configured")}
                </StatusBadge>
              )}
              {(skill.tags as string[] | undefined)
                ?.slice(0, 4)
                .map((tag) => (
                  <Chip key={tag} variant="slate" size="sm">
                    {tag}
                  </Chip>
                ))}
            </>
          ),
          body: (
            <div style={{ display: "grid", gap: 14 }}>
              <p style={{ margin: 0, color: "#44403c" }}>{description}</p>
              {bindingContexts.length ? (
                <div style={{ display: "grid", gap: 8 }}>
                  <div
                    style={{
                      fontSize: 10,
                      fontWeight: 800,
                      color: "#a8a29e",
                      letterSpacing: 0.8,
                      textTransform: "uppercase",
                    }}
                  >
                    Binding provenance
                  </div>
                  {bindingContexts.map((context, index) => (
                    <div
                      key={`${context.binding_id || context.automation_id || "binding"}-${index}`}
                      style={{
                        display: "grid",
                        gap: 3,
                        padding: "8px 0",
                        borderTop: index === 0 ? "1px solid #e7e5e4" : "none",
                      }}
                    >
                      <div style={{ fontSize: 12, fontWeight: 700, color: "#292524" }}>
                        {detailedBindingLabel(context)}
                      </div>
                      <div style={{ fontSize: 11, color: "#78716c" }}>
                        {bindingSourceLabel(context)}
                        {context.service_key ? ` · ${context.service_key}` : ""}
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ),
          primaryAction: {
            label: t("page.skill_card.test_invoke"),
            icon: <IconPlay size={15} />,
            onClick: () => {
              closeDetail();
              onInvoke(skill);
            },
          },
          secondaryActions: [
            ...(needsCreds
              ? [
                  {
                    label: t("page.skill_card.configure_credentials"),
                    icon: <IconKey size={16} />,
                    onClick: () => {
                      closeDetail();
                      onCredential(skill);
                    },
                  },
                ]
              : []),
            {
              label: t("page.skill_card.view_usage_and_examples"),
              icon: <IconInfo size={16} />,
              onClick: () => {
                closeDetail();
                onDetails(skill);
              },
            },
            ...(!isAgentView
              ? [
                  {
                    label: t("action.edit"),
                    icon: <IconEdit size={16} />,
                    onClick: () => {
                      closeDetail();
                      onEdit(skill);
                    },
                  },
                ]
              : []),
          ],
          dangerAction: isAgentView
            ? selectedAgentId
              ? {
                  label: t("page.skill_card.unbind_from_agent"),
                  icon: <IconTrash size={16} />,
                  onClick: () => {
                    closeDetail();
                    onUnbind({ agentId: selectedAgentId, skillId: skill.id });
                  },
                }
              : undefined
            : {
                label: t("action.delete"),
                icon: <IconTrash size={16} />,
                disabled: isDeletePending,
                onClick: () => {
                  closeDetail();
                  onDelete(skill.id);
                },
              },
        })
      }
    />
  );
}
