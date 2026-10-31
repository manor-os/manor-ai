import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Checkbox from "../ui/Checkbox";
import Toggle from "../ui/Toggle";
import AvatarStack from "../ui/AvatarStack";
import UserAvatar from "../ui/UserAvatar";
import {
  api,
  type ConnectionKind,
  type ConnectionShareMember,
  type ConnectionShareState,
  type ConnectionShareSummary,
} from "../../lib/api";
import { INTEGRATION_CATALOG_QUERY_KEY } from "../../lib/integrationCatalog";
import { t } from "../../lib/i18n";
import { useToastStore } from "../../stores/toast";
import { getPreferredTimeZone } from "../../lib/format";

export interface ShareableConnection {
  id: string;
  kind: ConnectionKind;
  label: string;
}

function memberName(member: ConnectionShareMember): string {
  return member.display_name || member.email || member.user_id;
}

export function hasConnectionShares(summary?: ConnectionShareSummary | null): boolean {
  return Boolean(summary && (summary.entity || summary.user_count > 0));
}

export function ShareBadge({ summary }: { summary?: ConnectionShareSummary | null }) {
  if (!summary || !hasConnectionShares(summary)) return null;
  if (summary.entity || !summary.people?.length) {
    const label = summary.entity
      ? t("page.integrations.sharing_badge_everyone")
      : t("page.integrations.sharing_badge_people").replace("{count}", String(summary.user_count));
    return <span className="shrink-0 text-[11px] text-[var(--text-muted)]">{label}</span>;
  }
  const names = summary.people.map((person) => person.display_name || person.user_id);
  const extra = summary.user_count - names.length;
  const label = `${t("page.integrations.sharing_badge_people").replace(
    "{count}", String(summary.user_count),
  )}: ${names.join(", ")}${extra > 0 ? ` +${extra}` : ""}`;
  return (
    <AvatarStack
      people={summary.people.map((person) => ({
        id: person.user_id,
        name: person.display_name || person.user_id,
        avatarUrl: person.avatar_url,
      }))}
      total={summary.user_count}
      label={label}
    />
  );
}

function MemberLabel({ member }: { member: ConnectionShareMember }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-2">
      <UserAvatar name={memberName(member)} avatarUrl={member.avatar_url} size={22} />
      <span className="min-w-0 truncate">{memberName(member)}</span>
    </span>
  );
}

function LoadError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
      <span>{t("page.integrations.sharing_load_error")}</span>
      <Button variant="ghost" size="sm" onClick={onRetry}>
        {t("page.integrations.sharing_retry")}
      </Button>
    </div>
  );
}

export default function ShareConnectionDialog({
  open,
  onClose,
  connection,
}: {
  open: boolean;
  onClose: () => void;
  connection: ShareableConnection;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const stateKey = ["connection-grants", connection.kind, connection.id];
  const shareState = useQuery({
    queryKey: stateKey,
    queryFn: () => api.integrations.getConnectionShares(connection.kind, connection.id),
    enabled: open,
    // Members, roles and usage change outside this dialog; refetch on open.
    staleTime: 0,
  });
  const usage = useQuery({
    queryKey: ["connection-usage", connection.kind, connection.id],
    queryFn: () => api.integrations.listConnectionUsage(connection.kind, connection.id),
    enabled: open,
    staleTime: 0,
  });
  const applyState = (state: ConnectionShareState) => {
    queryClient.setQueryData(stateKey, state);
    queryClient.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
  };
  const onError = (error: any) =>
    toast.error(t("page.integrations.sharing_error"), error?.message || "");
  const shareWithPeople = useMutation({
    mutationFn: (userIds: string[]) =>
      api.integrations.shareConnectionWithUsers(connection.kind, connection.id, userIds),
    onSuccess: (state) => {
      setSelected(new Set());
      applyState(state);
    },
    onError,
  });
  const setEveryone = useMutation({
    mutationFn: (enabled: boolean) =>
      api.integrations.setConnectionEntityShare(connection.kind, connection.id, enabled),
    onSuccess: applyState,
    onError,
  });
  const revoke = useMutation({
    mutationFn: (grantId: string) =>
      api.integrations.deleteConnectionGrant(connection.kind, connection.id, grantId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: stateKey });
      queryClient.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
    },
    onError,
  });
  const state = shareState.data;
  const everyone = Boolean(state?.entity_grant_id);
  const uses = usage.data ?? [];
  const candidates = state?.candidates ?? [];
  // Members may have left or been shared with since they were ticked.
  const currentSelection = candidates
    .filter((member) => selected.has(member.user_id))
    .map((member) => member.user_id);
  const submitSelection = () => {
    if (currentSelection.length === 0) {
      setSelected(new Set());
      return;
    }
    if (currentSelection.length !== selected.size) setSelected(new Set(currentSelection));
    shareWithPeople.mutate(currentSelection);
  };
  const toggleSelected = (userId: string, checked: boolean) =>
    setSelected((previous) => {
      const next = new Set(previous);
      if (checked) next.add(userId);
      else next.delete(userId);
      return next;
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("page.integrations.sharing_title").replace("{name}", connection.label)}
      maxWidth="520px"
      footer={
        <Button variant="outline" onClick={onClose}>
          {t("page.integrations.sharing_done")}
        </Button>
      }
    >
      <div className="flex flex-col gap-4 text-sm">
        <p className="m-0 text-xs leading-5 text-[var(--text-muted)]">
          {t("page.integrations.sharing_full_use_note")}
        </p>

        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="font-medium">{t("page.integrations.sharing_everyone")}</div>
            <div className="text-xs text-[var(--text-muted)]">
              {t("page.integrations.sharing_everyone_hint")}
            </div>
          </div>
          <Toggle
            checked={everyone}
            disabled={!state || setEveryone.isPending}
            onChange={() => setEveryone.mutate(!everyone)}
            aria-label={t("page.integrations.sharing_everyone")}
          />
        </div>

        {shareState.isError && (
          <LoadError onRetry={() => shareState.refetch()} />
        )}

        <section className="flex flex-col gap-2">
          <div className="font-medium">{t("page.integrations.sharing_people")}</div>
          {state && candidates.length === 0 ? (
            <p className="m-0 text-xs text-[var(--text-muted)]">
              {t("page.integrations.sharing_no_candidates")}
            </p>
          ) : (
            <div className="flex max-h-48 flex-col gap-1 overflow-y-auto">
              {candidates.map((member) => (
                <Checkbox
                  key={member.user_id}
                  size="sm"
                  checked={selected.has(member.user_id)}
                  onChange={(checked) => toggleSelected(member.user_id, checked)}
                  label={<MemberLabel member={member} />}
                />
              ))}
            </div>
          )}
          {candidates.length > 0 && (
            <div>
              <Button
                variant="primary"
                size="sm"
                disabled={currentSelection.length === 0}
                loading={shareWithPeople.isPending}
                onClick={submitSelection}
              >
                {t("page.integrations.sharing_add_people")}
              </Button>
            </div>
          )}
        </section>

        {state && state.users.length > 0 && (
          <section className="flex flex-col gap-1">
            <div className="font-medium">{t("page.integrations.sharing_shared_with")}</div>
            {state.users.map((share) => (
              <div key={share.grant_id} className="flex items-center gap-2">
                <span className="flex min-w-0 flex-1">
                  <MemberLabel member={share} />
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(share.grant_id)}
                >
                  {t("page.integrations.sharing_remove")}
                </Button>
              </div>
            ))}
          </section>
        )}

        <section className="flex flex-col gap-1">
          <div className="font-medium">{t("page.integrations.sharing_usage")}</div>
          {usage.isError ? (
            <LoadError onRetry={() => usage.refetch()} />
          ) : uses.length === 0 ? (
            <p className="m-0 text-xs text-[var(--text-muted)]">
              {t("page.integrations.sharing_usage_empty")}
            </p>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs">
              {uses.map((use, index) => (
                <li key={`${use.created_at}-${index}`} className="flex min-w-0 gap-2">
                  <span className="min-w-0 truncate">
                    {use.actor_display_name || use.actor_user_id}
                  </span>
                  <span className="min-w-0 truncate text-[var(--text-muted)]">{`${use.server_key} · ${use.tool_name}`}</span>
                  <span className="mono ml-auto shrink-0 text-[var(--text-muted)]">
                    {new Date(use.created_at).toLocaleString(undefined, { timeZone: getPreferredTimeZone() })}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Modal>
  );
}
