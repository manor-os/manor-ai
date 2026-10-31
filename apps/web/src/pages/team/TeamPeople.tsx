import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Modal from "../../components/ui/Modal";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import LoadingSpinner from "../../components/ui/LoadingSpinner";
import EmptyState from "../../components/ui/EmptyState";
import Button from "../../components/ui/Button";
import Input from "../../components/ui/Input";
import FilterPills from "../../components/ui/FilterPills";
import Dropdown from "../../components/ui/Dropdown";
import CompactCard from "../../components/ui/CompactCard";
import { PageHeaderAddButton } from "../../components/ui/PageHeader";
import { openDetail, closeDetail } from "../../stores/detail";
import type { TeamActivityItem, TeamUsageMember } from "../../lib/types";
import {
  IconBuilding,
  IconChecklist,
  IconClock,
  IconCreditCard,
  IconEdit,
  IconGlobe,
  IconKey,
  IconMail,
  IconPhone,
  IconRefresh,
  IconShield,
  IconTag,
  IconTrash,
  IconTrendingUp,
  IconUser,
  IconUserPlus,
  IconWrench,
} from "../../components/icons";
import { usePermission } from "../../lib/permissions";
import { userAvatarImage } from "../../lib/userAvatar";
import {
  Kind,
  KindFilter,
  KIND_BADGE,
  KIND_FILTERS,
  STATUS_MAP,
  StaffForm,
  emptyStaffForm,
  toForm,
  formToPayload,
  staffStatusLabel,
  summarizePermissions,
} from "./shared";
import { getPreferredTimeZone } from "../../lib/format";

export default function TeamPeople() {
  const queryClient = useQueryClient();
  const can = usePermission();
  const { teamSearch: search = "", setTeamControls } = (useOutletContext() || {}) as {
    teamSearch?: string;
    setTeamControls?: (controls: { toolbar?: ReactNode; actions?: ReactNode }) => void;
  };
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");

  const [inviteOpen, setInviteOpen] = useState(false);
  const [vendorOpen, setVendorOpen] = useState(false);
  const [editStaff, setEditStaff] = useState<any | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmTransfer, setConfirmTransfer] = useState<{ id: string; name: string } | null>(null);
  const [editForm, setEditForm] = useState<StaffForm>(emptyStaffForm);
  const [createdAccount, setCreatedAccount] = useState<{
    name: string;
    email: string;
    password: string;
    mode: "created" | "reset";
  } | null>(null);
  const [deliveredInvite, setDeliveredInvite] = useState<{
    email: string;
    invite_url: string;
    email_sent: boolean;
  } | null>(null);

  const { data: staffList, isLoading } = useQuery({
    queryKey: ["staff"],
    queryFn: () => api.staff.list(),
  });

  const { data: roles = [] } = useQuery({
    queryKey: ["staff-roles"],
    queryFn: () => api.staff.roles.list(),
  });

  const canViewTeamUsage =
    can("admin.audit") || can("admin.billing") || can("users.manage");
  const canTransferKnowledge = can("docs.transfer_departed");

  const { data: teamUsage, isLoading: teamUsageLoading, error: teamUsageError } = useQuery({
    queryKey: ["team-usage", 30],
    queryFn: () => api.usage.team({ days: 30, activity_limit: 80 }),
    enabled: canViewTeamUsage,
    retry: false,
  });

  const roleNameById = useMemo(() => {
    const map = new Map<string, string>();
    roles.forEach((role: any) => {
      if (role?.id && role?.name) map.set(role.id, role.name);
    });
    return map;
  }, [roles]);

  const usageByStaffId = useMemo(() => {
    const map = new Map<string, TeamUsageMember>();
    (teamUsage?.members || []).forEach((member) => {
      map.set(member.staff_id, member);
    });
    return map;
  }, [teamUsage]);

  const updateStaffMutation = useMutation({
    mutationFn: ({ id, data, initialRole }: { id: string; data: StaffForm; initialRole: string }) => {
      const payload = formToPayload(data);
      // The role is an authority change: only send it when it was changed here,
      // never as a side effect of editing a phone number.
      if (data.role === initialRole) delete payload.role;
      return api.staff.update(id, payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["staff"] });
      queryClient.invalidateQueries({ queryKey: ["team-usage"] });
      setEditStaff(null);
      setVendorOpen(false);
      setEditForm(emptyStaffForm);
    },
  });

  const createVendorMutation = useMutation({
    mutationFn: (data: StaffForm) =>
      api.staff.create({ ...formToPayload(data), kind: data.kind }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["staff"] });
      queryClient.invalidateQueries({ queryKey: ["team-usage"] });
      setVendorOpen(false);
      setEditForm(emptyStaffForm);
    },
  });

  const deleteStaffMutation = useMutation({
    mutationFn: (id: string) => api.staff.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["staff"] });
      queryClient.invalidateQueries({ queryKey: ["team-usage"] });
      setConfirmDelete(null);
    },
  });

  const transferKnowledgeMutation = useMutation({
    mutationFn: (id: string) => api.staff.transferKnowledgeToEntity(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents-browse"] });
      setConfirmTransfer(null);
    },
  });

  const createAccountMutation = useMutation({
    mutationFn: ({ staffId }: { staffId: string }) =>
      api.staff.createAccount(staffId),
    onSuccess: (
      data: {
        staff_id: string;
        user_id: string;
        email: string;
        password: string;
      },
      vars: { staffId: string },
    ) => {
      queryClient.invalidateQueries({ queryKey: ["staff"] });
      queryClient.invalidateQueries({ queryKey: ["team-usage"] });
      const member = staff.find((s: any) => s.id === vars.staffId);
      setCreatedAccount({
        name: member?.name || data.email,
        email: data.email,
        password: data.password,
        mode: "created",
      });
    },
  });

  const resetPasswordMutation = useMutation({
    mutationFn: ({ staffId }: { staffId: string }) =>
      api.staff.resetAccountPassword(staffId),
    onSuccess: (
      data: {
        staff_id: string;
        user_id: string;
        email: string;
        password: string;
      },
      vars: { staffId: string },
    ) => {
      queryClient.invalidateQueries({ queryKey: ["team-usage"] });
      const member = staff.find((s: any) => s.id === vars.staffId);
      setCreatedAccount({
        name: member?.name || data.email,
        email: data.email,
        password: data.password,
        mode: "reset",
      });
    },
  });

  const resendInviteMutation = useMutation({
    mutationFn: ({ staffId }: { staffId: string }) =>
      api.staff.resendInvite(staffId),
    onSuccess: (data: { email: string; invite_url: string; email_sent: boolean }) => {
      queryClient.invalidateQueries({ queryKey: ["staff"] });
      queryClient.invalidateQueries({ queryKey: ["team-usage"] });
      setDeliveredInvite(data);
    },
  });

  const staff = staffList || [];

  const filteredStaff = staff.filter((s: any) => {
    const k = (s.kind as Kind) || "employee";
    if (kindFilter !== "all" && k !== kindFilter) return false;
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      (s.name || "").toLowerCase().includes(q) ||
      (s.email || "").toLowerCase().includes(q) ||
      (s.title || "").toLowerCase().includes(q) ||
      (s.company_name || "").toLowerCase().includes(q)
    );
  });

  function openEdit(s: any) {
    setEditStaff(s);
    setEditForm(toForm(s));
  }

  const openAddVendor = useCallback(() => {
    setEditForm({ ...emptyStaffForm, kind: "vendor", role: "" });
    setVendorOpen(true);
  }, []);

  const canInvite = can("users.invite");
  const canDelete = can("users.manage");

  const openStaffDetail = useCallback((s: any) => {
    const view = staffViewModel(s, roleNameById);
    const usageMember = usageByStaffId.get(s.id);
    const canResendInvite =
      canInvite &&
      !view.hasLinkedAccount &&
      Boolean(s.email) &&
      (view.hasUnlinkedUserAccount || view.normalizedStatus === "invited");
    const accountLabel = view.hasLinkedAccount
      ? t("page.team_people.reset_password")
      : view.hasUnlinkedUserAccount
        ? canInvite
          ? t("page.team_people.resend_invite")
          : t("page.team_people.awaiting_invite")
        : t("page.team_people.create_account");
    const accountDisabled =
      view.hasLinkedAccount
        ? resetPasswordMutation.isPending
        : view.hasUnlinkedUserAccount
          ? !canResendInvite || resendInviteMutation.isPending
          : createAccountMutation.isPending;

    const runAccountAction = () => {
      closeDetail();
      if (view.hasLinkedAccount) {
        resetPasswordMutation.mutate({ staffId: s.id });
      } else if (view.hasUnlinkedUserAccount) {
        resendInviteMutation.mutate({ staffId: s.id });
      } else {
        createAccountMutation.mutate({ staffId: s.id });
      }
    };

    const detailActions: TeamDetailAction[] = [];
    if (canDelete) {
      detailActions.push({
        label: t("action.edit"),
        icon: <IconEdit size={16} />,
        onClick: () => {
          closeDetail();
          openEdit(s);
        },
      });
    }
    if (
      canTransferKnowledge
      && s.user_id
      && String(s.status || "").toLowerCase() !== "active"
    ) {
      detailActions.push({
        label: t("page.team_people.transfer_knowledge"),
        icon: <IconBuilding size={16} />,
        disabled: transferKnowledgeMutation.isPending,
        onClick: () => {
          closeDetail();
          transferKnowledgeMutation.reset();
          setConfirmTransfer({ id: s.id, name: view.displayName });
        },
      });
    }
    if (s.email && (canDelete || view.hasUnlinkedUserAccount)) {
      detailActions.push({
        label: accountLabel,
        icon: view.hasLinkedAccount
          ? <IconRefresh size={16} />
          : view.hasUnlinkedUserAccount
            ? <IconMail size={16} />
            : <IconUserPlus size={16} />,
        disabled: accountDisabled,
        onClick: runAccountAction,
      });
    }
    if (canResendInvite && !view.hasUnlinkedUserAccount) {
      detailActions.push({
        label: t("page.team_people.resend_invite"),
        icon: <IconMail size={16} />,
        disabled: resendInviteMutation.isPending,
        onClick: () => {
          closeDetail();
          resendInviteMutation.mutate({ staffId: s.id });
        },
      });
    }
    if (canDelete) {
      detailActions.push({
        label: t("page.team_people.remove"),
        icon: <IconTrash size={16} />,
        onClick: () => {
          closeDetail();
          setConfirmDelete(s.id);
        },
      });
    }

    openDetail({
      key: `staff-${s.id}`,
      icon: (
        <StaffAvatar
          avatarUrl={view.avatarUrl}
          name={view.displayName}
          kind={view.kind}
          size={48}
          iconSize={24}
        />
      ),
      title: view.displayName,
      subtitle: <span className="team-detail-subtitle-line">{view.subtitle || s.email || s.phone || t("page.team_people.na_dash")}</span>,
      badges: (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, rowGap: 6 }}>
          <StaffMetaItem icon={<IconUser size={13} />}>
            {view.statusLabel}
          </StaffMetaItem>
          <StaffMetaItem icon={<StaffKindIcon kind={view.kind} size={13} />}>
            {view.kindCfg.label}
          </StaffMetaItem>
          {view.rate ? (
            <StaffMetaItem icon={<IconCreditCard size={13} />}>
              {view.rate}
            </StaffMetaItem>
          ) : (
            view.roleLabel && (
              <StaffMetaItem icon={<IconShield size={13} />}>
                <span style={{ textTransform: "capitalize" }}>{view.roleLabel}</span>
              </StaffMetaItem>
            )
          )}
          {view.hasLinkedAccount && (
            <StaffMetaItem icon={<IconKey size={13} />}>
              {t("page.team_people.has_account")}
            </StaffMetaItem>
          )}
          {!view.hasLinkedAccount && view.hasUnlinkedUserAccount && (
            <StaffMetaItem icon={<IconMail size={13} />}>
              {t("page.team_people.awaiting_invite")}
            </StaffMetaItem>
          )}
        </div>
      ),
      body: (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {(s.email || s.phone) && (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {s.email && (
                <StaffInfoRow icon={<IconMail size={14} />}>
                  {s.email}
                </StaffInfoRow>
              )}
              {s.phone && (
                <StaffInfoRow icon={<IconPhone size={14} />}>
                  {s.phone}
                </StaffInfoRow>
              )}
            </div>
          )}
          {view.cats.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  color: "#1c1917",
                  fontSize: 11,
                  fontWeight: 800,
                  textTransform: "uppercase",
                }}
              >
                <IconTag size={13} />
                {view.isVendorLike
                  ? t("page.team_people.service_categories")
                  : t("page.team_people.skills")}
              </div>
              <div style={{ color: "#1c1917", fontSize: 12, lineHeight: 1.45 }}>
                {view.cats.join(", ")}
              </div>
            </div>
          )}
          {(s.title || view.orgLabel) && (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {s.title && (
                <StaffInfoRow icon={<IconTag size={14} />} muted>
                  {s.title}
                </StaffInfoRow>
              )}
              {view.orgLabel && (
                <StaffInfoRow icon={<IconBuilding size={14} />} muted>
                  {view.orgLabel}
                </StaffInfoRow>
              )}
            </div>
          )}
          {canViewTeamUsage && (
            <TeamUsagePanel
              member={usageMember}
              loading={teamUsageLoading}
              error={teamUsageError as Error | null}
            />
          )}
        </div>
      ),
      actions: detailActions.length ? <TeamDetailActions actions={detailActions} /> : undefined,
    });
  }, [
    canDelete,
    canInvite,
    canTransferKnowledge,
    canViewTeamUsage,
    createAccountMutation,
    resendInviteMutation,
    resetPasswordMutation,
    transferKnowledgeMutation,
    roleNameById,
    usageByStaffId,
  ]);

  const addMenuItems = useMemo(() => {
    const items: { key: string; label: string }[] = [];
    if (canInvite) items.push({ key: "invite", label: t("page.team_people.invite_employee") });
    if (canInvite) items.push({ key: "vendor", label: t("page.team_people.add_vendor") });
    return items;
  }, [canInvite]);

  const handleAddSelect = useCallback(
    (key: string) => {
      if (key === "invite") setInviteOpen(true);
      else if (key === "vendor") openAddVendor();
    },
    [openAddVendor],
  );

  useEffect(() => {
    if (!setTeamControls) return;
    setTeamControls({
      toolbar: (
        <FilterPills
          options={KIND_FILTERS.map((k) => ({ key: k.key, label: k.label }))}
          value={kindFilter}
          onChange={(k) => setKindFilter(k as KindFilter)}
        />
      ),
      actions: addMenuItems.length > 0 ? (
        <Dropdown
          align="right"
          trigger={
            <PageHeaderAddButton label={t("page.team_people.add_member")} caret />
          }
          items={addMenuItems}
          onSelect={handleAddSelect}
        />
      ) : null,
    });
    return () => setTeamControls({});
  }, [addMenuItems, handleAddSelect, kindFilter, setTeamControls]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full gap-3 text-stone-400">
        <LoadingSpinner size={20} />
        <span className="text-sm">{t("page.team_people.loading_team")}</span>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto pb-2">
        <ConfirmDialog
          open={!!confirmDelete}
          onClose={() => {
            deleteStaffMutation.reset();
            setConfirmDelete(null);
          }}
          onConfirm={() => {
            if (confirmDelete) deleteStaffMutation.mutate(confirmDelete);
          }}
          title={t("page.team_people.remove")}
          message={t("page.team_people.remove_confirm")}
          confirmLabel={
            deleteStaffMutation.isPending ? t("page.team_people.removing") : t("page.team_people.remove")
          }
          loading={deleteStaffMutation.isPending}
          closeOnConfirm={false}
          error={
            deleteStaffMutation.isError
              ? (deleteStaffMutation.error as Error).message
              : undefined
          }
          danger
        />

        <ConfirmDialog
          open={!!confirmTransfer}
          onClose={() => {
            transferKnowledgeMutation.reset();
            setConfirmTransfer(null);
          }}
          onConfirm={() => {
            if (confirmTransfer) transferKnowledgeMutation.mutate(confirmTransfer.id);
          }}
          title={t("page.team_people.transfer_knowledge_title")}
          message={t("page.team_people.transfer_knowledge_confirm", {
            name: confirmTransfer?.name || "",
          })}
          confirmLabel={
            transferKnowledgeMutation.isPending
              ? t("page.team_people.transferring_knowledge")
              : t("page.team_people.transfer_knowledge")
          }
          loading={transferKnowledgeMutation.isPending}
          closeOnConfirm={false}
          error={
            transferKnowledgeMutation.isError
              ? (transferKnowledgeMutation.error as Error).message
              : undefined
          }
        />

        {filteredStaff.length > 0 ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))",
              gap: 24,
              alignItems: "start",
            }}
          >
            {filteredStaff.map((s: any) => {
              const view = staffViewModel(s, roleNameById);
              return (
                <CompactCard
                  key={s.id}
                  icon={
                    <StaffAvatar
                      avatarUrl={view.avatarUrl}
                      name={view.displayName}
                      kind={view.kind}
                      size={34}
                      iconSize={18}
                    />
                  }
                  title={view.displayName}
                  subtitle={
                    <span style={{ color: "#1c1917" }}>
                      {view.cardSubtitle || view.subtitle || s.email || s.phone || t("page.team_people.na_dash")}
                    </span>
                  }
                  meta={<StaffCardStatus label={view.statusLabel} />}
                  onClick={() => openStaffDetail(s)}
                />
              );
            })}
          </div>
        ) : (
          <EmptyState
            icon={<EmptyUserIcon />}
            title={
              search || kindFilter !== "all"
                ? t("page.team_people.no_people_match")
                : t("page.team_people.no_team_members")
            }
            description={
              search || kindFilter !== "all"
                ? t("page.team_people.try_adjusting")
                : t("page.team_people.no_team_members_desc")
            }
          />
        )}
      </div>

      <InviteEmployeeModal
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        roles={roles}
      />

      {createdAccount && (
        <StaffAccountCreatedModal
          name={createdAccount.name}
          email={createdAccount.email}
          password={createdAccount.password}
          mode={createdAccount.mode}
          onClose={() => setCreatedAccount(null)}
        />
      )}

      {deliveredInvite && (
        <InviteDeliveryModal
          email={deliveredInvite.email}
          inviteUrl={deliveredInvite.invite_url}
          emailSent={deliveredInvite.email_sent}
          onClose={() => setDeliveredInvite(null)}
        />
      )}

      <StaffEditModal
        open={vendorOpen || !!editStaff}
        title={editStaff ? t("page.team_people.edit_person") : t("page.team_people.add_vendor")}
        form={editForm}
        setForm={setEditForm}
        onClose={() => {
          setEditStaff(null);
          setVendorOpen(false);
          setEditForm(emptyStaffForm);
        }}
        onSave={() => {
          if (editStaff)
            updateStaffMutation.mutate({
              id: editStaff.id,
              data: editForm,
              initialRole: toForm(editStaff).role,
            });
          else createVendorMutation.mutate(editForm);
        }}
        isSaving={
          updateStaffMutation.isPending || createVendorMutation.isPending
        }
        error={
          (updateStaffMutation.error as Error)?.message ||
          (createVendorMutation.error as Error)?.message
        }
        lockKind={!editStaff}
      />
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────
//   Invite Employee modal
// ──────────────────────────────────────────────────────────────────────────

function InviteEmployeeModal({
  open,
  onClose,
  roles,
}: {
  open: boolean;
  onClose: () => void;
  roles: any[];
}) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [roleId, setRoleId] = useState<string>("");
  const [createdInvite, setCreatedInvite] = useState<{
    email: string;
    invite_url: string;
    email_sent: boolean;
  } | null>(null);
  const [copied, setCopied] = useState(false);

  const selectedRole = roles.find((r) => r.id === roleId);

  const inviteMutation = useMutation({
    mutationFn: () =>
      api.staff.invite({
        email,
        role_id: roleId || null,
        name: name || undefined,
        workspace_ids: [],
    }),
    onSuccess: (data: { email: string; invite_url: string; email_sent: boolean }) => {
      queryClient.invalidateQueries({ queryKey: ["staff"] });
      queryClient.invalidateQueries({ queryKey: ["team-usage"] });
      setCreatedInvite(data);
      setCopied(false);
    },
  });

  function reset() {
    setEmail("");
    setName("");
    setRoleId("");
    setCreatedInvite(null);
    setCopied(false);
  }

  useMemoDefault(roles, roleId, setRoleId);

  return (
    <Modal
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title={t("page.team_people.invite_employee")}
      footer={
        createdInvite ? (
          <Button
            variant="primary"
            onClick={() => {
              reset();
              onClose();
            }}
          >
            {t("page.team_people.done")}
          </Button>
        ) : (
          <>
            <Button
              variant="outline"
              onClick={() => {
                reset();
                onClose();
              }}
            >
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              disabled={!email || !roleId || inviteMutation.isPending}
              onClick={() => inviteMutation.mutate()}
            >
              {inviteMutation.isPending ? t("page.team_people.sending") : t("page.team_people.send_invite")}
            </Button>
          </>
        )
      }
    >
      {createdInvite ? (
        <div className="space-y-4">
          <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">
            <div className="font-bold text-emerald-900">
              {createdInvite.email_sent
                ? t("page.team_people.invite_email_sent")
                : t("page.team_people.invite_created_email_failed")}
            </div>
            <div className="mt-1 break-words">
              {createdInvite.email}
            </div>
          </div>
          <div>
            <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">
              {t("page.team_people.invite_link")}
            </label>
            <div className="flex gap-2">
              <input
                className="manor-input"
                readOnly
                value={createdInvite.invite_url}
                onFocus={(e) => e.currentTarget.select()}
                style={{ minWidth: 0 }}
              />
              <Button
                variant="outline"
                onClick={async () => {
                  await navigator.clipboard?.writeText(createdInvite.invite_url);
                  setCopied(true);
                }}
                style={{ whiteSpace: "nowrap" }}
              >
                {copied ? t("page.team_people.invite_link_copied") : t("page.team_people.copy_invite_link")}
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <Input
            label={t("page.team_people.email")}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={t("page.team_people.jane_company_com")}
          />
          <Input
            label={t("page.team_people.name_optional")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("page.team_people.jane_doe")}
          />
          <div>
            <label className="block text-sm font-medium text-stone-600 mb-2">
              {t("page.team_people.role")}
            </label>
            <div className="flex gap-1.5 flex-wrap">
              {roles.map((r: any) => {
                const sel = roleId === r.id;
                return (
                  <button
                    key={r.id}
                    type="button"
                    onClick={() => setRoleId(r.id)}
                    className={[
                      "px-3 py-1.5 rounded-lg text-xs font-semibold border cursor-pointer capitalize",
                      sel
                        ? "bg-manor-50 border-manor-700/30 text-manor-700"
                        : "bg-white border-stone-200 text-stone-500 hover:text-stone-700",
                    ].join(" ")}
                  >
                    {r.name}
                  </button>
                );
              })}
            </div>
          </div>

          {selectedRole && (
            <div className="bg-manor-50/60 border border-manor-700/15 rounded-xl p-3">
              <div className="text-[11px] font-bold uppercase tracking-widest text-manor-700 mb-1.5">
                <span className="capitalize">{selectedRole.name}</span> {t("page.team_people.can")}
              </div>
              <div className="text-xs text-stone-600 leading-relaxed">
                {summarizePermissions(selectedRole.permissions || [])}
              </div>
            </div>
          )}

          {inviteMutation.isError && (
            <p className="text-red-600 text-sm">
              {(inviteMutation.error as Error).message}
            </p>
          )}

          <div className="text-[11px] text-stone-400 text-center">
            {t("page.team_people.invite_note")}
          </div>
        </div>
      )}
    </Modal>
  );
}

function useMemoDefault(
  roles: any[],
  roleId: string,
  setRoleId: (id: string) => void,
) {
  useMemo(() => {
    if (!roleId && roles.length) {
      const def =
        roles.find((r) => r.is_default) ||
        roles.find((r) => r.name === "member") ||
        roles[0];
      if (def) setRoleId(def.id);
    }
  }, [roles, roleId, setRoleId]);
}

function InviteDeliveryModal({
  email,
  inviteUrl,
  emailSent,
  onClose,
}: {
  email: string;
  inviteUrl: string;
  emailSent: boolean;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <Modal
      open
      onClose={onClose}
      title={t("page.team_people.invite_resent")}
      footer={
        <Button variant="primary" onClick={onClose}>
          {t("page.team_people.done")}
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">
          <div className="font-bold text-emerald-900">
            {emailSent
              ? t("page.team_people.invite_email_sent")
              : t("page.team_people.invite_created_email_failed")}
          </div>
          <div className="mt-1 break-words">{email}</div>
        </div>

        <div>
          <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">
            {t("page.team_people.invite_link")}
          </label>
          <div className="flex gap-2">
            <input
              className="manor-input"
              readOnly
              value={inviteUrl}
              onFocus={(e) => e.currentTarget.select()}
              style={{ minWidth: 0 }}
            />
            <Button
              variant="outline"
              onClick={async () => {
                await navigator.clipboard?.writeText(inviteUrl);
                setCopied(true);
              }}
              style={{ whiteSpace: "nowrap" }}
            >
              {copied ? t("page.team_people.invite_link_copied") : t("page.team_people.copy_invite_link")}
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

// ──────────────────────────────────────────────────────────────────────────
//   Staff Edit / Add-Vendor modal
// ──────────────────────────────────────────────────────────────────────────

interface EditModalProps {
  open: boolean;
  title: string;
  form: StaffForm;
  setForm: (f: StaffForm) => void;
  onClose: () => void;
  onSave: () => void;
  isSaving: boolean;
  error?: string;
  lockKind: boolean;
}

function StaffEditModal({
  open,
  title,
  form,
  setForm,
  onClose,
  onSave,
  isSaving,
  error,
  lockKind,
}: EditModalProps) {
  const isVendorLike = form.kind === "vendor" || form.kind === "contractor";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      className="team-edit-dialog"
      footer={
        <>
          <Button variant="outline" className="team-edit-cancel" onClick={onClose}>
            {t("action.cancel")}
          </Button>
          <Button
            variant="primary"
            className="team-edit-save"
            disabled={!form.name || isSaving}
            onClick={onSave}
          >
            {isSaving ? t("page.team_people.saving") : t("action.save")}
          </Button>
        </>
      }
    >
      <div className="team-edit-form">
        {!lockKind && (
          <div>
            <label className="team-edit-label">
              {t("page.team_people.kind")}
            </label>
            <div className="team-edit-kind-row">
              {(
                Object.entries(KIND_BADGE) as [
                  Kind,
                  (typeof KIND_BADGE)[Kind],
                ][]
              ).map(([key, cfg]) => {
                const selected = form.kind === key;
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setForm({ ...form, kind: key })}
                    className={[
                      "team-edit-kind-button",
                      selected ? "is-selected" : "",
                    ].join(" ")}
                  >
                    <StaffKindIcon kind={key} size={14} />
                    <span>{cfg.label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <Input
          label={form.kind === "vendor" ? t("page.team_people.contact_name") : t("page.team_people.name")}
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder={form.kind === "vendor" ? t("page.team_people.primary_contact") : t("page.team_people.full_name")}
        />
        <Input
          label={t("page.team_people.email")}
          type="email"
          value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })}
          placeholder={
            form.kind === "vendor"
              ? "billing@company.com"
              : "person@example.com"
          }
        />
        <Input
          label={t("page.team_people.phone")}
          value={form.phone}
          onChange={(e) => setForm({ ...form, phone: e.target.value })}
          placeholder="+1 (555) 000-0000"
        />

        {isVendorLike && (
          <div className="team-edit-section">
            <div className="team-edit-section-title">
              {t("page.team_people.commercial")}
            </div>
            {form.kind === "vendor" && (
              <>
                <Input
                  label={t("page.team_people.company_name")}
                  value={form.company_name}
                  onChange={(e) =>
                    setForm({ ...form, company_name: e.target.value })
                  }
                  placeholder={t("page.team_people.e_g_joe_s_plumbing_pty_ltd")}
                />
                <Input
                  label={t("page.team_people.tax_id")}
                  value={form.tax_id}
                  onChange={(e) => setForm({ ...form, tax_id: e.target.value })}
                  placeholder={t("page.team_people.abn_ein_vat")}
                />
              </>
            )}
            <div>
              <label className="team-edit-label">
                {t("page.team_people.service_categories")}
              </label>
              <input
                value={form.service_categories}
                onChange={(e) =>
                  setForm({ ...form, service_categories: e.target.value })
                }
                className="manor-input"
                placeholder={t("page.team_people.plumbing_electrical_hvac_comma_separated")}
              />
            </div>
            <div className="grid grid-cols-[2fr_1fr] gap-2">
              <Input
                label={t("page.team_people.rate")}
                type="number"
                value={form.billing_rate}
                onChange={(e) =>
                  setForm({ ...form, billing_rate: e.target.value })
                }
                placeholder="120.00"
              />
              <div>
                <label className="team-edit-label">
                  {t("page.team_people.currency")}
                </label>
                <select
                  value={form.billing_currency}
                  onChange={(e) =>
                    setForm({ ...form, billing_currency: e.target.value })
                  }
                  className="manor-input"
                >
                  <option value="">—</option>
                  <option value="USD">{t("page.team_people.usd")}</option>
                  <option value="AUD">{t("page.team_people.aud")}</option>
                  <option value="EUR">{t("page.team_people.eur")}</option>
                  <option value="GBP">{t("page.team_people.gbp")}</option>
                  <option value="CNY">{t("page.team_people.cny")}</option>
                </select>
              </div>
            </div>
            <Input
              label={t("page.team_people.payment_terms")}
              value={form.payment_terms}
              onChange={(e) =>
                setForm({ ...form, payment_terms: e.target.value })
              }
              placeholder={t("page.team_people.e_g_net_30_on_completion")}
            />
            <div>
              <label className="team-edit-label">
                {t("page.team_people.payment_method")}
              </label>
              <select
                value={form.preferred_payment_method}
                onChange={(e) =>
                  setForm({ ...form, preferred_payment_method: e.target.value })
                }
                className="manor-input"
              >
                <option value="">—</option>
                <option value="bank_transfer">{t("page.team_people.bank_transfer")}</option>
                <option value="stripe">{t("page.team_people.stripe_card")}</option>
                <option value="check">{t("page.team_people.check")}</option>
                <option value="cash">{t("page.team_people.cash")}</option>
              </select>
            </div>
          </div>
        )}

        {form.kind === "employee" && (
          <>
            <Input
              label={t("page.team_people.title")}
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder={t("page.team_people.e_g_software_engineer")}
            />
            <div>
              <label className="team-edit-label">
                {t("page.team_people.role")}
              </label>
              <select
                value={form.role}
                onChange={(e) => setForm({ ...form, role: e.target.value })}
                className="manor-input"
              >
                <option value="viewer">{t("page.users.role_viewer")}</option>
                <option value="member">{t("page.users.role_member")}</option>
                <option value="admin">{t("page.users.role_admin")}</option>
                <option value="owner">{t("page.users.role_owner")}</option>
              </select>
            </div>
          </>
        )}

        <div>
          <label className="team-edit-label">
            {t("page.team_people.skills")}
          </label>
          <input
            value={form.skills}
            onChange={(e) => setForm({ ...form, skills: e.target.value })}
            className="manor-input"
            placeholder={t("page.team_people.comma_separated")}
          />
        </div>

        <Input
          label={t("page.team_people.address")}
          value={form.address}
          onChange={(e) => setForm({ ...form, address: e.target.value })}
          placeholder={t("page.login.optional")}
        />
        {form.kind === "vendor" && (
          <Input
            label={t("page.team_people.website")}
            value={form.website}
            onChange={(e) => setForm({ ...form, website: e.target.value })}
            placeholder={t("page.team_people.https_example_com")}
          />
        )}
        <div>
          <label className="team-edit-label">
            {t("page.team_people.notes")}
          </label>
          <textarea
            value={form.notes}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
            className="manor-input"
            rows={2}
            placeholder={t("page.login.optional")}
          />
        </div>

        {error && <p className="text-red-600 text-sm">{error}</p>}
      </div>
    </Modal>
  );
}

// ──────────────────────────────────────────────────────────────────────────
//   Staff Account Created modal
// ──────────────────────────────────────────────────────────────────────────

function StaffAccountCreatedModal({
  name,
  email,
  password,
  mode,
  onClose,
}: {
  name: string;
  email: string;
  password: string;
  mode: "created" | "reset";
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [pwVisible, setPwVisible] = useState(false);
  const isReset = mode === "reset";

  function copyAll() {
    navigator.clipboard.writeText(`Email: ${email}\nPassword: ${password}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={isReset ? t("page.team_people.password_reset") : t("page.team_people.account_created")}
      footer={
        <Button variant="primary" onClick={onClose}>
          {t("page.team_people.done")}
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="bg-manor-50 border border-manor-200 rounded-xl p-4 text-sm text-manor-800">
          {isReset ? (
            <>
              {t("page.team_people.password_reset_for")} <strong>{name}</strong>. {t("page.team_people.credentials_note")}
            </>
          ) : (
            <>
              {t("page.team_people.account_created_for")} <strong>{name}</strong>. {t("page.team_people.credentials_note")}
            </>
          )}
        </div>

        <div className="space-y-2">
          <div>
            <label className="block text-xs font-semibold text-stone-500 mb-1 uppercase tracking-wide">
              {t("page.team_people.email")}
            </label>
            <div className="manor-input bg-stone-50 select-all">
              <span className="text-sm">{email}</span>
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-stone-500 mb-1 uppercase tracking-wide">
              {t("page.team_people.password")}
            </label>
            <div className="flex gap-2 items-center">
              <div
                className="manor-input bg-stone-50 flex-1 select-all"
                style={{ letterSpacing: pwVisible ? "normal" : 2 }}
              >
                <span className="text-sm font-mono">
                  {pwVisible ? password : "•".repeat(password.length)}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setPwVisible((v) => !v)}
                className="text-stone-400 hover:text-stone-600 text-xs px-2 py-1 border border-stone-200 rounded-lg"
              >
                  {pwVisible ? t("page.team_people.hide") : t("page.team_people.show")}
              </button>
            </div>
          </div>
        </div>

        <button
          type="button"
          onClick={copyAll}
          className="w-full py-2 rounded-xl border border-stone-200 text-sm text-stone-600 hover:bg-stone-50 flex items-center justify-center gap-2"
        >
          {copied ? t("page.team_people.copied") : t("page.team_people.copy_credentials")}
        </button>
      </div>
    </Modal>
  );
}

// ──────────────────────────────────────────────────────────────────────────
//   Team usage / activity
// ──────────────────────────────────────────────────────────────────────────

function TeamUsagePanel({
  member,
  loading,
  error,
}: {
  member?: TeamUsageMember;
  loading: boolean;
  error: Error | null;
}) {
  const usage = member?.usage;
  const activity = member?.activity;
  const recent = activity?.recent || [];

  return (
    <div className="team-detail-section">
      <div className="team-detail-section-head">
        <div className="team-detail-section-title">
          <IconTrendingUp size={14} />
          {t("page.team_people.usage_activity")}
        </div>
        <span className="team-detail-section-meta">
          {t("page.team_people.company_scope_30d")}
        </span>
      </div>

      {loading && !member ? (
        <div className="team-detail-empty">
          {t("page.team_people.loading_usage")}
        </div>
      ) : error ? (
        <div className="team-detail-empty">
          {t("page.team_people.usage_unavailable")}
        </div>
      ) : (
        <>
          <div className="team-detail-metric-grid">
            <TeamMetric
              icon={<IconCreditCard size={14} />}
              label={t("page.team_people.credits_used")}
              value={formatInteger(usage?.credits_used || 0)}
            />
            <TeamMetric
              icon={<IconChecklist size={14} />}
              label={t("page.team_people.tasks")}
              value={formatInteger(usage?.task_count || 0)}
            />
            <TeamMetric
              icon={<IconKey size={14} />}
              label={t("page.team_people.requests")}
              value={formatInteger(usage?.request_count || 0)}
            />
            <TeamMetric
              icon={<IconClock size={14} />}
              label={t("page.team_people.active_time")}
              value={formatDuration(activity?.active_seconds || 0)}
            />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            <StaffInfoRow icon={<IconClock size={14} />} muted>
              {t("page.team_people.last_used")}: {formatDateTime(usage?.last_used_at)}
            </StaffInfoRow>
            <StaffInfoRow icon={<IconUser size={14} />} muted>
              {t("page.team_people.last_seen")}: {formatDateTime(activity?.last_seen_at)}
            </StaffInfoRow>
          </div>

          <div className="team-detail-activity">
            <div className="team-detail-subtitle">
              {t("page.team_people.recent_activity")}
            </div>
            {recent.length > 0 ? (
              <div className="team-detail-activity-list">
                {recent.map((item) => (
                  <TeamActivityRow key={item.id} item={item} />
                ))}
              </div>
            ) : (
              <div className="team-detail-empty">
                {t("page.team_people.no_recent_activity")}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function TeamMetric({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="team-detail-metric">
      <span className="team-detail-metric-icon">
        {icon}
      </span>
      <div className="team-detail-metric-copy">
        <span className="team-detail-metric-label">
          {label}
        </span>
        <span className="team-detail-metric-value">
          {value}
        </span>
      </div>
    </div>
  );
}

function TeamActivityRow({ item }: { item: TeamActivityItem }) {
  return (
    <div className="team-detail-activity-row">
      <span className="team-detail-activity-icon">
        <IconClock size={13} />
      </span>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="team-detail-activity-main">
          <div className="team-detail-activity-summary">
            {item.summary || item.event_type}
          </div>
          <div className="team-detail-activity-date">
            {formatShortDate(item.created_at)}
          </div>
        </div>
        {(item.workspace_name || item.event_type) && (
          <div className="team-detail-activity-meta">
            {[item.workspace_name, item.event_type].filter(Boolean).join(" · ")}
          </div>
        )}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────
//   Small helpers
// ──────────────────────────────────────────────────────────────────────────

interface TeamDetailAction {
  label: string;
  icon: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}

const brokenStaffAvatarUrls = new Set<string>();

function TeamDetailActions({ actions }: { actions: TeamDetailAction[] }) {
  return (
    <div
      style={{
        width: "100%",
        display: "flex",
        justifyContent: "flex-end",
        alignItems: "center",
        gap: 10,
      }}
    >
      {actions.map((action) => (
        <button
          key={action.label}
          type="button"
          title={action.label}
          aria-label={action.label}
          onClick={action.onClick}
          disabled={action.disabled}
          className="team-detail-action-button"
          style={{
            border: "none",
            background: "transparent",
            padding: 4,
            margin: 0,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            cursor: action.disabled ? "not-allowed" : "pointer",
            opacity: action.disabled ? 0.45 : 1,
          }}
        >
          {action.icon}
        </button>
      ))}
    </div>
  );
}

function StaffAvatar({
  avatarUrl,
  name,
  kind,
  size,
  iconSize,
}: {
  avatarUrl?: string | null;
  name: string;
  kind: Kind;
  size: number;
  iconSize: number;
}) {
  const src = userAvatarImage(avatarUrl);
  const [brokenSrc, setBrokenSrc] = useState<string | null>(() =>
    src && brokenStaffAvatarUrls.has(src) ? src : null,
  );
  const isBroken = Boolean(src && (brokenSrc === src || brokenStaffAvatarUrls.has(src)));

  if (src && !isBroken) {
    return (
      <img
        src={src}
        alt={name}
        onError={() => {
          brokenStaffAvatarUrls.add(src);
          setBrokenSrc(src);
        }}
        style={{
          width: size,
          height: size,
          minWidth: size,
          minHeight: size,
          borderRadius: "50%",
          objectFit: "cover",
          display: "block",
          flexShrink: 0,
          border: "1px solid rgba(28, 25, 23, 0.08)",
          background: "#fff",
        }}
      />
    );
  }

  return <StaffGlyph kind={kind} size={size} iconSize={iconSize} />;
}

function StaffGlyph({
  kind,
  size,
  iconSize,
}: {
  kind: Kind;
  size: number;
  iconSize: number;
}) {
  return (
    <span
      style={{
        width: size,
        height: size,
        minWidth: size,
        minHeight: size,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#1c1917",
        flexShrink: 0,
      }}
    >
      <StaffKindIcon kind={kind} size={iconSize} />
    </span>
  );
}

function StaffMetaItem({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <span
      className="team-detail-meta-item"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        fontSize: 12,
        lineHeight: 1.35,
        minWidth: 0,
      }}
    >
      <span className="team-detail-meta-icon" style={{ display: "inline-flex", flexShrink: 0 }}>
        {icon}
      </span>
      <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {children}
      </span>
    </span>
  );
}

function StaffCardStatus({ label }: { label: ReactNode }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        fontSize: 11,
        fontWeight: 650,
        whiteSpace: "nowrap",
      }}
    >
      <IconUser size={12} />
      {label}
    </span>
  );
}

function StaffInfoRow({
  icon,
  children,
}: {
  icon: ReactNode;
  children: ReactNode;
  muted?: boolean;
}) {
  return (
    <div
      className="team-detail-info-row"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        minWidth: 0,
        fontSize: 12,
        lineHeight: 1.35,
      }}
    >
      <span className="team-detail-info-icon" style={{ display: "inline-flex", flexShrink: 0 }}>
        {icon}
      </span>
      <span
        style={{
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {children}
      </span>
    </div>
  );
}

function StaffKindIcon({ kind, size = 14 }: { kind: Kind; size?: number }) {
  const style = { flexShrink: 0 };
  if (kind === "contractor") return <IconWrench size={size} style={style} />;
  if (kind === "vendor") return <IconBuilding size={size} style={style} />;
  if (kind === "external") return <IconGlobe size={size} style={style} />;
  return <IconUser size={size} style={style} />;
}

function staffViewModel(s: any, roleNameById: Map<string, string>) {
  const kind = (s.kind as Kind) || "employee";
  const kindCfg = KIND_BADGE[kind] || KIND_BADGE.employee;
  const isVendorLike = kind === "vendor" || kind === "contractor";
  const normalizedStatus = String(s.status || "active").toLowerCase();
  const statusCfg = STATUS_MAP[normalizedStatus] || STATUS_MAP.active;
  const statusLabel = staffStatusLabel(normalizedStatus);
  const cats: string[] = isVendorLike
    ? (s.service_categories || []).filter(Boolean)
    : (s.skills || []).filter(Boolean);
  const orgLabel = isVendorLike ? s.company_name : s.department;
  const subtitle = [s.title, orgLabel].filter(Boolean).join(" · ");
  const rate =
    isVendorLike && s.billing_rate != null
      ? `${s.billing_currency || ""} ${Number(s.billing_rate).toFixed(2)} /hr`
      : null;
  const displayName = s.name || s.email || t("page.team_people.na_dash");
  const roleLabel = normalizedRoleLabel(
    (s.role_id ? roleNameById.get(s.role_id) : null)
      || s.role_name
      || s.staff_role_name
      || s.role,
  );
  const avatarUrl = s.avatar_url || s.login_avatar_url || null;
  const hasLinkedAccount = Boolean(s.user_id);
  const hasUnlinkedUserAccount = Boolean(s.has_unlinked_user_account);
  const cardSubtitle = [
    kindCfg.label,
    roleLabel || s.email,
  ].filter(Boolean).join(" · ");

  return {
    kind,
    kindCfg,
    isVendorLike,
    normalizedStatus,
    statusCfg,
    statusLabel,
    cats,
    orgLabel,
    subtitle,
    rate,
    displayName,
    roleLabel,
    avatarUrl,
    hasLinkedAccount,
    hasUnlinkedUserAccount,
    cardSubtitle,
  };
}

function normalizedRoleLabel(value: unknown): string {
  const label = String(value || "").trim();
  if (!label || label.toLowerCase() === "staff") return "";
  return label;
}

function formatInteger(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(value || 0);
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Number(seconds) || 0);
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function formatDateTime(value?: string | null): string {
  if (!value) return t("page.team_people.na_dash");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t("page.team_people.na_dash");
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: getPreferredTimeZone(),
  });
}

function formatShortDate(value?: string | null): string {
  if (!value) return t("page.team_people.na_dash");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t("page.team_people.na_dash");
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: getPreferredTimeZone(),
  });
}

function EmptyUserIcon() {
  return (
    <svg
      style={{ width: 32, height: 32, color: "#d6d3d1" }}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={1.5}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z"
      />
    </svg>
  );
}
