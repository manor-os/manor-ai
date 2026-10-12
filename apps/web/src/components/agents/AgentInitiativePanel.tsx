import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError, type AgentDeploymentResponse } from "../../lib/api";
import { deploymentQueryOptions, useCatalogScope } from "../../lib/catalogQueries";
import { formatDateLong } from "../../lib/format";
import {
  decisionModeLabel,
  formatInitiativeSchedule,
  initiativeCondition,
  initiativeItems,
  initiativeStatusPresentation,
  initiativeTrigger,
} from "../../lib/agentInitiativeFormat";
import type {
  AgentInitiative,
  AgentInitiativeCreateRequest,
} from "../../lib/agentInitiativeTypes";
import type { Conversation } from "../../lib/types";
import { t } from "../../lib/i18n";
import { invalidateAgentInitiativeQueries } from "../../lib/agentInitiativeInvalidation";
import { closeDetail, openDetail } from "../../stores/detail";
import { useToastStore } from "../../stores/toast";
import AgentInitiativeForm, {
  type AgentInitiativeConversationOption,
  type AgentInitiativeFormScope,
} from "./AgentInitiativeForm";
import AgentInitiativeRunHistory from "./AgentInitiativeRunHistory";
import Button from "../ui/Button";
import CompactCard from "../ui/CompactCard";
import ConfirmDialog from "../ui/ConfirmDialog";
import EmptyState from "../ui/EmptyState";
import IconTile from "../ui/IconTile";
import LoadingSpinner from "../ui/LoadingSpinner";
import Select from "../ui/Select";
import StatusPill from "../ui/StatusPill";
import {
  IconBell,
  IconClock,
  IconEdit,
  IconPause,
  IconPlay,
  IconPlus,
  IconTrash,
} from "../icons";

type InitiativeActionKind = "run" | "mute" | "unmute" | "pause" | "resume" | "delete";

interface PendingAction {
  kind: InitiativeActionKind;
  initiative: AgentInitiative;
}

function scopeLabel(deployment: AgentDeploymentResponse): string {
  if (deployment.workspace_id) {
    return t("page.agent_initiative.workspace_scope", {
      name: deployment.workspace_name || deployment.name || t("page.agent_initiative.workspace"),
    });
  }
  const owner = deployment.owner_display_name || deployment.owner_name || deployment.name;
  return owner
    ? t("page.agent_initiative.personal_scope_named", { name: owner })
    : t("page.agent_initiative.personal_scope");
}

function deploymentUnavailableReason(deployment: AgentDeploymentResponse): string | null {
  if (deployment.status !== "active") return t("page.agent_initiative.subscription_inactive");
  if (!deployment.workspace_id && !deployment.owner_user_id) {
    return t("page.agent_initiative.ownerless_personal_disabled");
  }
  if (deployment.can_manage === false) return t("page.agent_initiative.no_manage_permission");
  return null;
}

function actionCopy(action: PendingAction): { title: string; message: string; confirm: string; danger?: boolean } {
  const name = action.initiative.name;
  if (action.kind === "mute") {
    return {
      title: t("page.agent_initiative.confirm_mute_title"),
      message: t("page.agent_initiative.confirm_mute_message", { name }),
      confirm: t("page.agent_initiative.mute"),
    };
  }
  if (action.kind === "unmute") {
    return {
      title: t("page.agent_initiative.confirm_unmute_title"),
      message: t("page.agent_initiative.confirm_unmute_message", { name }),
      confirm: t("page.agent_initiative.unmute"),
    };
  }
  if (action.kind === "pause") {
    return {
      title: t("page.agent_initiative.confirm_pause_title"),
      message: t("page.agent_initiative.confirm_pause_message", { name }),
      confirm: t("page.agent_initiative.pause"),
    };
  }
  if (action.kind === "resume") {
    return {
      title: t("page.agent_initiative.confirm_resume_title"),
      message: t("page.agent_initiative.confirm_resume_message", { name }),
      confirm: t("page.agent_initiative.resume"),
    };
  }
  if (action.kind === "delete") {
    return {
      title: t("page.agent_initiative.confirm_delete_title"),
      message: t("page.agent_initiative.confirm_delete_message", { name }),
      confirm: t("action.delete"),
      danger: true,
    };
  }
  return {
    title: t("page.agent_initiative.confirm_run_title"),
    message: action.initiative.status === "paused"
      ? t("page.agent_initiative.confirm_run_paused_message", { name })
      : action.initiative.status === "muted"
        ? t("page.agent_initiative.confirm_run_muted_message", { name })
        : t("page.agent_initiative.confirm_run_message", { name }),
    confirm: t("page.agent_initiative.run_now"),
  };
}

function initiativeDestination(initiative: AgentInitiative): string {
  const surface = initiative.delivery_policy?.surface;
  if (surface?.kind === "workspace_chat") return t("page.agent_initiative.workspace_main_chat");
  if (surface?.kind === "conversation") return t("page.agent_initiative.personal_agent_chat");
  const notification = initiative.delivery_policy?.notification;
  if (notification?.enabled) return t("page.agent_initiative.notification_only");
  return t("page.agent_initiative.destination_unavailable");
}

function DetailField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[10px] font-semibold uppercase tracking-wide text-stone-400">{label}</dt>
      <dd className="mt-1 break-words whitespace-pre-wrap text-sm leading-relaxed text-stone-700">{children}</dd>
    </div>
  );
}

export function AgentInitiativeDetailBody({
  initiative,
  conversation,
}: {
  initiative: AgentInitiative;
  conversation?: { id: string; title: string };
}) {
  const condition = initiativeCondition(initiative);
  const notification = initiative.delivery_policy?.notification;
  const quietHours = initiative.interruption_policy?.quiet_hours;
  return (
    <div className="space-y-6">
      <dl className="space-y-4">
        <DetailField label={t("page.agent_initiative.schedule")}>
          {formatInitiativeSchedule(initiativeTrigger(initiative))}
        </DetailField>
        <DetailField label={t("page.agent_initiative.instruction")}>
          {initiative.instruction}
        </DetailField>
        <DetailField label={t("page.agent_initiative.condition")}>
          {condition?.semantic_condition || t("page.agent_initiative.no_condition")}
        </DetailField>
        <DetailField label={t("page.agent_initiative.tell_me_when")}>
          {decisionModeLabel(initiative.decision_policy?.mode)}
        </DetailField>
        <DetailField label={t("page.agent_initiative.chat_destination")}>
          {conversation ? (
            <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span>{conversation.title}</span>
              <span className="font-mono text-[10px] tabular-nums text-stone-400">
                {conversation.id.slice(0, 8)}
              </span>
            </span>
          ) : initiativeDestination(initiative)}
        </DetailField>
        <DetailField label={t("page.agent_initiative.external_notification")}>
          {notification?.enabled
            ? (notification.channels?.length
              ? notification.channels.join(", ")
              : t("page.agent_initiative.default_channels"))
            : t("page.agent_initiative.off")}
        </DetailField>
        <DetailField label={t("page.agent_initiative.quiet_hours")}>
          {quietHours?.mode === "custom"
            ? t("page.agent_initiative.custom_quiet_hours", {
              from: quietHours.start || quietHours.from || "--",
              to: quietHours.end || quietHours.to || "--",
              timezone: quietHours.timezone,
            })
            : quietHours?.mode === "none"
              ? t("page.agent_initiative.quiet_hours_none_description")
              : t("page.agent_initiative.quiet_hours_inherited")}
        </DetailField>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <DetailField label={t("page.agent_initiative.next_run")}>
            <span className="font-mono text-xs tabular-nums">{formatDateLong(initiative.next_run_at || undefined)}</span>
          </DetailField>
          <DetailField label={t("page.agent_initiative.last_run")}>
            <span className="font-mono text-xs tabular-nums">
              {formatDateLong(initiative.last_checked_at || initiative.last_run_at || undefined)}
            </span>
          </DetailField>
        </div>
        <DetailField label={t("page.agent_initiative.last_notification")}>
          <span className="font-mono text-xs tabular-nums">
            {formatDateLong(initiative.last_notified_at || initiative.last_delivery_at || undefined)}
          </span>
        </DetailField>
        {initiative.last_silent_reason ? (
          <DetailField label={t("page.agent_initiative.last_silent_reason")}>
            {initiative.last_silent_reason}
          </DetailField>
        ) : null}
        {initiative.last_error ? (
          <DetailField label={t("page.agent_initiative.last_error")}>
            <span className="text-red-700">{initiative.last_error}</span>
          </DetailField>
        ) : null}
      </dl>
      <AgentInitiativeRunHistory initiativeId={initiative.id} />
    </div>
  );
}

function createIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `initiative-run-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function AgentInitiativePanel({ agentId }: { agentId: string }) {
  const queryClient = useQueryClient();
  const catalogScope = useCatalogScope();
  const toast = useToastStore();
  const [selectedSubscriptionId, setSelectedSubscriptionId] = useState("");
  const [formTarget, setFormTarget] = useState<AgentInitiative | "create" | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);

  const deploymentsQuery = useQuery({
    ...deploymentQueryOptions(catalogScope, agentId),
    enabled: !!catalogScope && !!agentId,
  });
  const deployments = deploymentsQuery.data || [];
  const selectableDeployments = useMemo(
    () => deployments.filter((deployment) => !deploymentUnavailableReason(deployment)),
    [deployments],
  );
  const selectableKey = selectableDeployments.map((deployment) => deployment.id).join(":");

  useEffect(() => {
    setSelectedSubscriptionId((current) => {
      if (selectableDeployments.some((deployment) => deployment.id === current)) return current;
      return selectableDeployments.length === 1 ? selectableDeployments[0].id : "";
    });
  }, [selectableKey]);

  const selectedDeployment = selectableDeployments.find(
    (deployment) => deployment.id === selectedSubscriptionId,
  );
  const canCreate = !!selectedDeployment && !selectedDeployment.workspace_id;
  const initiativesQuery = useQuery({
    queryKey: ["agent-initiatives", selectedSubscriptionId],
    queryFn: () => api.agentInitiatives.list(selectedSubscriptionId),
    enabled: !!selectedSubscriptionId,
  });
  const initiatives = initiativeItems(initiativesQuery.data).filter(
    (initiative) => initiative.status !== "deleted",
  );

  const conversationsQuery = useQuery({
    queryKey: [
      "conversations",
      "agent-initiative",
      selectedDeployment?.workspace_id || "personal",
    ],
    queryFn: () => api.chat.listConversations(selectedDeployment?.workspace_id || undefined),
    enabled: !!selectedDeployment && formTarget !== null,
  });
  const conversationOptions = useMemo<AgentInitiativeConversationOption[]>(() => {
    if (!selectedDeployment) return [];
    const matches = (conversationsQuery.data || [])
      .filter((conversation) => {
        if (conversation.status && conversation.status !== "active") return false;
        if (selectedDeployment.workspace_id) {
          return conversation.workspace_id === selectedDeployment.workspace_id
            && (!conversation.scope
              || ["workspace_main", "workspace_thread"].includes(conversation.scope));
        }
        if (conversation.workspace_id) return false;
        if (conversation.agent_subscription_id) {
          return conversation.agent_subscription_id === selectedDeployment.id;
        }
        return conversation.agent_id === agentId;
      })
      .map((conversation) => ({
        id: conversation.id,
        label: conversation.title || (selectedDeployment.workspace_id
          ? t("page.agent_initiative.untitled_workspace_conversation")
          : t("page.agent_initiative.untitled_conversation")),
      }));
    const defaultConversationId = selectedDeployment.default_conversation_id || selectedDeployment.conversation_id;
    if (defaultConversationId && !matches.some((conversation) => conversation.id === defaultConversationId)) {
      matches.unshift({
        id: defaultConversationId,
        label: t("page.agent_initiative.default_agent_chat"),
      });
    }
    return matches;
  }, [agentId, conversationsQuery.data, selectedDeployment]);

  const invalidateInitiative = async (initiativeId?: string) => {
    await invalidateAgentInitiativeQueries(queryClient, initiativeId);
  };

  const saveMutation = useMutation({
    mutationFn: async ({
      target,
      request,
    }: {
      target: AgentInitiative | null;
      request: AgentInitiativeCreateRequest;
    }) => {
      if (target) {
        return api.agentInitiatives.update(target.id, {
          expected_revision: target.revision,
          patch: request,
        });
      }
      if (!selectedSubscriptionId) throw new Error(t("page.agent_initiative.choose_instance_first"));
      if (selectedDeployment?.workspace_id) throw new Error(t("page.agent_initiative.workspace_automation_description"));
      return api.agentInitiatives.create(selectedSubscriptionId, request);
    },
    onSuccess: async (saved) => {
      setFormTarget(null);
      await invalidateInitiative(saved.id);
    },
  });

  const actionMutation = useMutation({
    mutationFn: async (action: PendingAction) => {
      const revision = { expected_revision: action.initiative.revision };
      if (action.kind === "run") {
        return api.agentInitiatives.run(
          action.initiative.id,
          createIdempotencyKey(),
          action.initiative.status === "paused",
        );
      }
      if (action.kind === "mute") return api.agentInitiatives.mute(action.initiative.id, revision);
      if (action.kind === "unmute") return api.agentInitiatives.unmute(action.initiative.id, revision);
      if (action.kind === "pause") return api.agentInitiatives.pause(action.initiative.id, revision);
      if (action.kind === "resume") return api.agentInitiatives.resume(action.initiative.id, revision);
      return api.agentInitiatives.delete(action.initiative.id, revision);
    },
    onSuccess: async (_response, action) => {
      setPendingAction(null);
      await invalidateInitiative(action.initiative.id);
      if (action.kind === "run") {
        toast.info(
          t("page.agent_initiative.run_queued_title"),
          t("page.agent_initiative.run_queued_message"),
        );
      }
    },
  });

  const editInitiative = (initiative: AgentInitiative) => {
    closeDetail();
    saveMutation.reset();
    setFormTarget(initiative);
  };

  const requestAction = (kind: InitiativeActionKind, initiative: AgentInitiative) => {
    closeDetail();
    actionMutation.reset();
    setPendingAction({ kind, initiative });
  };

  const openInitiative = (initiative: AgentInitiative) => {
    const presentation = initiativeStatusPresentation(initiative.status);
    const secondaryActions = [
      {
        label: t("action.edit"),
        icon: <IconEdit size={16} />,
        onClick: () => editInitiative(initiative),
      },
    ];
    if (initiative.status === "muted") {
      secondaryActions.push({
        label: t("page.agent_initiative.unmute"),
        icon: <IconBell size={16} />,
        onClick: () => requestAction("unmute", initiative),
      });
    } else if (initiative.status === "active") {
      secondaryActions.push({
        label: t("page.agent_initiative.mute"),
        icon: <IconBell size={16} />,
        onClick: () => requestAction("mute", initiative),
      });
    }
    if (initiative.status === "paused" || initiative.status === "needs_setup" || initiative.status === "error") {
      secondaryActions.push({
        label: t("page.agent_initiative.resume"),
        icon: <IconPlay size={16} />,
        onClick: () => requestAction("resume", initiative),
      });
    } else if (["active", "muted"].includes(initiative.status)) {
      secondaryActions.push({
        label: t("page.agent_initiative.pause"),
        icon: <IconPause size={16} />,
        onClick: () => requestAction("pause", initiative),
      });
    }
    openDetail({
      key: `agent-initiative:${initiative.id}:${initiative.revision}`,
      icon: <IconTile size={44}><IconClock size={21} /></IconTile>,
      title: initiative.name,
      subtitle: selectedDeployment ? scopeLabel(selectedDeployment) : undefined,
      badges: (
        <StatusPill
          status={initiative.status}
          label={presentation.label}
          size="sm"
        />
      ),
      body: <AgentInitiativeDetailBody initiative={initiative} />,
      primaryAction: ["active", "muted", "paused"].includes(initiative.status)
        ? {
          label: t("page.agent_initiative.run_now"),
          icon: <IconPlay size={16} />,
          onClick: () => requestAction("run", initiative),
        }
        : undefined,
      secondaryActions,
      dangerAction: {
        label: t("action.delete"),
        icon: <IconTrash size={16} />,
        onClick: () => requestAction("delete", initiative),
      },
      width: 600,
    });
  };

  const unavailableDeployments = deployments.filter(deploymentUnavailableReason);
  const selectedScope = selectedDeployment
    ? ({
      id: selectedDeployment.id,
      label: scopeLabel(selectedDeployment),
      workspaceId: selectedDeployment.workspace_id,
    } satisfies AgentInitiativeFormScope)
    : null;
  const confirmation = pendingAction ? actionCopy(pendingAction) : null;

  return (
    <section className="space-y-5" aria-labelledby="agent-initiative-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="agent-initiative-heading" className="m-0 text-base font-bold text-stone-900">
            {t("page.agent_initiative.title")}
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-stone-500">
            {t(selectedDeployment?.workspace_id
              ? "page.agent_initiative.workspace_automation_description"
              : "page.agent_initiative.description")}
          </p>
        </div>
        {canCreate ? (
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              saveMutation.reset();
              setFormTarget("create");
            }}
          >
            <IconPlus size={15} />
            {t("page.agent_initiative.create")}
          </Button>
        ) : null}
      </div>

      {deploymentsQuery.isLoading ? (
        <div className="flex min-h-40 items-center justify-center" aria-live="polite">
          <LoadingSpinner size={24} />
        </div>
      ) : deploymentsQuery.error ? (
        <div className="rounded-xl bg-red-50/70 p-4" role="alert">
          <p className="m-0 text-sm text-red-700">
            {translateApiError(deploymentsQuery.error, t("page.agent_initiative.instances_load_failed"))}
          </p>
          <Button variant="ghost" size="sm" className="mt-2" onClick={() => deploymentsQuery.refetch()}>
            {t("page.agent_initiative.retry")}
          </Button>
        </div>
      ) : deployments.length === 0 ? (
        <EmptyState
          icon={<IconClock size={22} />}
          title={t("page.agent_initiative.no_instances")}
          description={t("page.agent_initiative.no_instances_description")}
        />
      ) : (
        <>
          <div className="rounded-xl bg-stone-100/70 p-4">
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-stone-500">
              {t("page.agent_initiative.agent_instance")}
            </label>
            <Select
              value={selectedSubscriptionId}
              onChange={setSelectedSubscriptionId}
              options={selectableDeployments.map((deployment) => ({
                value: deployment.id,
                label: scopeLabel(deployment),
              }))}
              placeholder={t("page.agent_initiative.choose_instance")}
              disabled={selectableDeployments.length === 0}
              ariaLabel={t("page.agent_initiative.agent_instance")}
              filterable={selectableDeployments.length > 6}
            />
            {selectableDeployments.length > 1 && !selectedSubscriptionId ? (
              <p className="mt-2 text-xs leading-relaxed text-stone-500">
                {t("page.agent_initiative.choose_instance_description")}
              </p>
            ) : null}
            {unavailableDeployments.length ? (
              <div className="mt-3 space-y-1" role="status">
                {unavailableDeployments.map((deployment) => (
                  <p key={deployment.id} className="m-0 text-xs leading-relaxed text-amber-700">
                    {scopeLabel(deployment)} · {deploymentUnavailableReason(deployment)}
                  </p>
                ))}
              </div>
            ) : null}
          </div>

          {selectableDeployments.length === 0 ? (
            <EmptyState
              icon={<IconClock size={22} />}
              title={t("page.agent_initiative.no_manageable_instances")}
              description={t("page.agent_initiative.no_manageable_instances_description")}
            />
          ) : !selectedDeployment ? (
            <EmptyState
              icon={<IconClock size={22} />}
              title={t("page.agent_initiative.choose_instance_first")}
              description={t("page.agent_initiative.choose_instance_description")}
            />
          ) : initiativesQuery.isLoading ? (
            <div className="flex min-h-40 items-center justify-center" aria-live="polite">
              <LoadingSpinner size={24} />
            </div>
          ) : initiativesQuery.error ? (
            <div className="rounded-xl bg-red-50/70 p-4" role="alert">
              <p className="m-0 text-sm text-red-700">
                {translateApiError(initiativesQuery.error, t("page.agent_initiative.list_load_failed"))}
              </p>
              <Button variant="ghost" size="sm" className="mt-2" onClick={() => initiativesQuery.refetch()}>
                {t("page.agent_initiative.retry")}
              </Button>
            </div>
          ) : initiatives.length === 0 ? (
            <EmptyState
              icon={<IconClock size={22} />}
              title={t("page.agent_initiative.empty_title")}
              description={t(selectedDeployment.workspace_id
                ? "page.agent_initiative.workspace_automation_description"
                : "page.agent_initiative.empty_description")}
              action={canCreate ? (
                <Button variant="primary" size="sm" onClick={() => setFormTarget("create")}>
                  <IconPlus size={15} />
                  {t("page.agent_initiative.create_first")}
                </Button>
              ) : undefined}
            />
          ) : (
            <div className="grid gap-3 lg:grid-cols-2">
              {initiatives.map((initiative) => {
                const presentation = initiativeStatusPresentation(initiative.status);
                const schedule = formatInitiativeSchedule(initiativeTrigger(initiative));
                return (
                  <CompactCard
                    key={initiative.id}
                    icon={<IconTile size={36}><IconClock size={17} /></IconTile>}
                    title={initiative.name}
                    subtitle={initiative.next_run_at
                      ? `${schedule} · ${t("page.agent_initiative.next_short", { time: formatDateLong(initiative.next_run_at) })}`
                      : schedule}
                    meta={(
                      <StatusPill
                        status={initiative.status}
                        label={presentation.label}
                        size="sm"
                      />
                    )}
                    action={["active", "muted", "paused"].includes(initiative.status) ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => requestAction("run", initiative)}
                        disabled={actionMutation.isPending}
                        ariaLabel={t("page.agent_initiative.run_named", { name: initiative.name })}
                        title={t("page.agent_initiative.run_now")}
                      >
                        <IconPlay size={14} />
                      </Button>
                    ) : undefined}
                    onClick={() => openInitiative(initiative)}
                  />
                );
              })}
            </div>
          )}
        </>
      )}

      {selectedScope && formTarget && (formTarget !== "create" || canCreate) ? (
        <AgentInitiativeForm
          key={`${selectedScope.id}:${formTarget === "create" ? "create" : formTarget.id}`}
          open
          initiative={formTarget === "create" ? null : formTarget}
          scope={selectedScope}
          conversations={conversationOptions}
          conversationsLoading={conversationsQuery.isLoading}
          saving={saveMutation.isPending}
          error={saveMutation.error}
          onClose={() => {
            if (!saveMutation.isPending) setFormTarget(null);
          }}
          onSubmit={async (request: AgentInitiativeCreateRequest) => {
            await saveMutation.mutateAsync({
              target: formTarget === "create" ? null : formTarget,
              request,
            });
          }}
        />
      ) : null}

      <ConfirmDialog
        open={!!pendingAction && !!confirmation}
        onClose={() => {
          if (!actionMutation.isPending) setPendingAction(null);
        }}
        onConfirm={() => {
          if (pendingAction) actionMutation.mutate(pendingAction);
        }}
        title={confirmation?.title || ""}
        message={confirmation?.message || ""}
        confirmLabel={confirmation?.confirm}
        cancelLabel={t("action.cancel")}
        danger={confirmation?.danger}
        loading={actionMutation.isPending}
        closeOnConfirm={false}
        error={actionMutation.error
          ? translateApiError(actionMutation.error, t("page.agent_initiative.action_failed"))
          : undefined}
      />
    </section>
  );
}
