import { useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import {
  formatInitiativeSchedule,
  initiativeStatusPresentation,
  initiativeTrigger,
} from "../../lib/agentInitiativeFormat";
import type {
  AgentInitiative,
  AgentInitiativeConversationListResponse,
} from "../../lib/agentInitiativeTypes";
import { formatDateLong } from "../../lib/format";
import { t } from "../../lib/i18n";
import { useIsCollaborator } from "../../lib/collaboratorSession";
import { useAfterStartup } from "../../lib/useAfterStartup";
import { closeDetail, openDetail } from "../../stores/detail";
import {
  IconArrowLeft,
  IconBell,
  IconChat,
  IconChevronRight,
  IconClock,
  IconRefresh,
  IconViewOptions,
} from "../icons";
import AnchoredPopover from "../ui/AnchoredPopover";
import Button from "../ui/Button";
import CompactCard from "../ui/CompactCard";
import EmptyState from "../ui/EmptyState";
import HoverMarqueeText from "../ui/HoverMarqueeText";
import IconTile from "../ui/IconTile";
import LoadingSpinner from "../ui/LoadingSpinner";
import StatusPill from "../ui/StatusPill";
import { AgentInitiativeDetailBody } from "./AgentInitiativePanel";

interface ConversationInitiativesButtonProps {
  conversationId?: string | null;
  fallbackTitle?: string;
  className?: string;
  openDirectly?: boolean;
  returnFocusTo?: HTMLElement | null;
  stopClickPropagation?: boolean;
}

interface ConversationInitiativesMenuItemProps {
  conversationId?: string | null;
  fallbackTitle?: string;
  onSelect?: () => void;
  returnFocusTo?: HTMLElement | null;
}

interface ConversationPresentation {
  id: string;
  title: string;
  scope: string;
}

function shortConversationId(conversationId: string): string {
  return conversationId.slice(0, 8);
}

function conversationTitle(
  response: AgentInitiativeConversationListResponse,
  fallbackTitle?: string,
): string {
  return response.conversation.title?.trim()
    || fallbackTitle?.trim()
    || t("page.agent_initiative.current_chat_untitled");
}

function conversationScopeLabel(scope: string): string {
  if (scope === "workspace_main") {
    return t("page.agent_initiative.current_chat_workspace_main");
  }
  if (scope === "workspace_thread") {
    return t("page.agent_initiative.current_chat_workspace_thread");
  }
  return t("page.agent_initiative.current_chat_personal");
}

export function conversationInitiativeItems(
  response?: AgentInitiativeConversationListResponse,
): AgentInitiative[] {
  return response?.items.filter((initiative) => initiative.status !== "deleted") || [];
}

export function useConversationInitiativesQuery(conversationId?: string | null) {
  // Agent initiatives are a company surface an external collaborator can't open.
  const collaborator = useIsCollaborator();
  const startupSettled = useAfterStartup();
  return useQuery({
    queryKey: ["agent-initiatives", "conversation", conversationId],
    queryFn: () => api.agentInitiatives.listForConversation(conversationId as string),
    enabled: Boolean(conversationId) && !collaborator && startupSettled,
    staleTime: 30_000,
  });
}

function openConversationInitiatives(
  conversationId: string,
  fallbackTitle?: string,
  returnFocusTo?: HTMLElement | null,
) {
  openDetail({
    key: `conversation-initiatives:${conversationId}`,
    icon: <IconTile size={44}><IconBell size={21} /></IconTile>,
    title: t("page.agent_initiative.current_chat_title"),
    subtitle: fallbackTitle,
    body: (
      <ConversationInitiativeList
        conversationId={conversationId}
        fallbackTitle={fallbackTitle}
        returnFocusTo={returnFocusTo}
      />
    ),
    width: 560,
    returnFocusTo,
  });
}

export function ConversationInitiativesMenuItem({
  conversationId,
  fallbackTitle,
  onSelect,
  returnFocusTo,
}: ConversationInitiativesMenuItemProps) {
  if (!conversationId) return null;

  const openInitiatives = () => {
    onSelect?.();
    openConversationInitiatives(conversationId, fallbackTitle, returnFocusTo);
  };

  return (
    <button
      type="button"
      className="chat-quick-access-row"
      onClick={openInitiatives}
      aria-label={t("page.agent_initiative.open_current_chat_initiatives")}
    >
      <span className="chat-quick-access-row-icon" aria-hidden="true">
        <IconBell size={18} />
      </span>
      <span className="chat-quick-access-row-copy">
        <strong>{t("page.agent_initiative.title")}</strong>
        <span>{t("page.agent_initiative.quick_access_description")}</span>
      </span>
      <IconChevronRight
        className="chat-quick-access-row-chevron"
        size={15}
        aria-hidden="true"
      />
    </button>
  );
}

function openInitiativeDetail(
  initiative: AgentInitiative,
  conversation: ConversationPresentation,
  returnFocusTo?: HTMLElement | null,
) {
  const presentation = initiativeStatusPresentation(initiative.status);
  openDetail({
    key: `conversation-initiative:${initiative.id}:${initiative.revision}`,
    icon: <IconTile size={44}><IconClock size={21} /></IconTile>,
    title: initiative.name,
    subtitle: `${conversation.title} · ${shortConversationId(conversation.id)}`,
    badges: (
      <StatusPill
        status={initiative.status}
        label={presentation.label}
        size="sm"
      />
    ),
    body: (
      <AgentInitiativeDetailBody
        initiative={initiative}
        conversation={{ id: conversation.id, title: conversation.title }}
      />
    ),
    primaryAction: {
      label: t("page.agent_initiative.open_current_chat"),
      icon: <IconChat size={16} />,
      onClick: closeDetail,
    },
    secondaryActions: [{
      label: t("page.agent_initiative.back_to_current_chat_list"),
      icon: <IconArrowLeft size={16} />,
      onClick: () => openConversationInitiatives(
        conversation.id,
        conversation.title,
        returnFocusTo,
      ),
    }],
    width: 600,
    returnFocusTo,
  });
}

function ConversationInitiativeCards({
  initiatives,
  conversation,
  onSelect,
  returnFocusTo,
}: {
  initiatives: AgentInitiative[];
  conversation: ConversationPresentation;
  onSelect?: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  return (
    <div className="conversation-initiative-list">
      {initiatives.map((initiative) => {
        const status = initiativeStatusPresentation(initiative.status);
        const schedule = formatInitiativeSchedule(initiativeTrigger(initiative));
        return (
          <CompactCard
            key={initiative.id}
            className="conversation-initiative-item"
            icon={<IconTile size={36}><IconClock size={17} /></IconTile>}
            title={<HoverMarqueeText text={initiative.name} />}
            subtitle={initiative.next_run_at
              ? `${schedule} · ${t("page.agent_initiative.next_short", {
                time: formatDateLong(initiative.next_run_at),
              })}`
              : schedule}
            meta={(
              <StatusPill
                status={initiative.status}
                label={status.label}
                size="sm"
              />
            )}
            onClick={() => {
              onSelect?.();
              openInitiativeDetail(initiative, conversation, returnFocusTo);
            }}
          />
        );
      })}
    </div>
  );
}

export function ConversationInitiativesQuickList({
  response,
  fallbackTitle,
  onSelect,
  returnFocusTo,
}: {
  response: AgentInitiativeConversationListResponse;
  fallbackTitle?: string;
  onSelect?: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const conversation: ConversationPresentation = {
    id: response.conversation.id,
    title: conversationTitle(response, fallbackTitle),
    scope: response.conversation.scope,
  };

  return (
    <ConversationInitiativeCards
      initiatives={conversationInitiativeItems(response)}
      conversation={conversation}
      onSelect={onSelect}
      returnFocusTo={returnFocusTo}
    />
  );
}

function ConversationInitiativeList({
  conversationId,
  fallbackTitle,
  returnFocusTo,
}: {
  conversationId: string;
  fallbackTitle?: string;
  returnFocusTo?: HTMLElement | null;
}) {
  const query = useConversationInitiativesQuery(conversationId);

  if (query.isLoading) {
    return (
      <div
        className="flex min-h-28 items-center justify-center gap-2 text-sm text-stone-500"
        role="status"
        aria-live="polite"
      >
        <LoadingSpinner size={20} />
        <span className="sr-only">{t("page.agent_initiative.current_chat_loading")}</span>
      </div>
    );
  }

  if (!query.data) {
    return (
      <div className="rounded-xl bg-red-50/70 p-4" role="alert">
        <p className="m-0 text-sm text-red-700">
          {translateApiError(
            query.error,
            t("page.agent_initiative.current_chat_load_failed"),
          )}
        </p>
        <Button
          variant="ghost"
          size="sm"
          className="mt-2"
          onClick={() => query.refetch()}
        >
          <IconRefresh size={14} />
          {t("page.agent_initiative.retry")}
        </Button>
      </div>
    );
  }

  const title = conversationTitle(query.data, fallbackTitle);
  const conversation: ConversationPresentation = {
    id: query.data.conversation.id,
    title,
    scope: query.data.conversation.scope,
  };
  const initiatives = conversationInitiativeItems(query.data);

  return (
    <div className="space-y-4">
      <CompactCard
        className="conversation-initiative-context"
        icon={<IconTile size={36}><IconChat size={17} /></IconTile>}
        title={<HoverMarqueeText text={title} />}
        subtitle={conversationScopeLabel(conversation.scope)}
        onClick={closeDetail}
        ariaLabel={t("page.agent_initiative.open_current_chat")}
      />

      {initiatives.length === 0 ? (
        <EmptyState
          compact
          className="conversation-initiative-empty"
          icon={<IconClock size={20} />}
          title={t("page.agent_initiative.current_chat_empty_title")}
          description={t("page.agent_initiative.current_chat_empty_description")}
          action={(
            <Button variant="primary" size="sm" onClick={closeDetail}>
              <IconChat size={14} />
              {t("page.agent_initiative.current_chat_empty_action")}
            </Button>
          )}
        />
      ) : (
        <ConversationInitiativeCards
          initiatives={initiatives}
          conversation={conversation}
          returnFocusTo={returnFocusTo}
        />
      )}
    </div>
  );
}

export default function ConversationInitiativesButton({
  conversationId,
  fallbackTitle,
  className = "",
  openDirectly = false,
  returnFocusTo,
  stopClickPropagation = false,
}: ConversationInitiativesButtonProps) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const initiativesQuery = useConversationInitiativesQuery(conversationId);
  const hasInitiatives = conversationInitiativeItems(initiativesQuery.data).length > 0;
  if (!conversationId || !hasInitiatives) return null;
  const ariaLabel = t(openDirectly
    ? "page.agent_initiative.open_current_chat_initiatives"
    : "page.agent_initiative.open_chat_controls");
  const trigger = (
    <button
      type="button"
      ref={triggerRef}
      className={`workspace-chat-stats-trigger conversation-initiative-trigger ${className}`.trim()}
      aria-label={ariaLabel}
      title={ariaLabel}
      data-testid="conversation-initiative-trigger"
      onClick={openDirectly
        ? (event) => {
          if (stopClickPropagation) event.stopPropagation();
          openConversationInitiatives(
            conversationId,
            fallbackTitle,
            returnFocusTo ?? triggerRef.current,
          );
        }
        : undefined}
    >
      <IconViewOptions size={18} />
    </button>
  );

  if (openDirectly) return trigger;

  return (
    <AnchoredPopover
      align="right"
      width={360}
      ariaLabel={ariaLabel}
      panelClassName="conversation-initiative-popover"
      trigger={trigger}
    >
      {({ close }) => (
        <div className="chat-quick-access-panel">
          <ConversationInitiativesMenuItem
            conversationId={conversationId}
            fallbackTitle={fallbackTitle}
            onSelect={close}
            returnFocusTo={returnFocusTo ?? triggerRef.current}
          />
        </div>
      )}
    </AnchoredPopover>
  );
}
