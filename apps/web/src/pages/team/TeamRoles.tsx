import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { api } from "../../lib/api";
import StatusBadge from "../../components/ui/StatusBadge";
import Modal from "../../components/ui/Modal";
import Button from "../../components/ui/Button";
import Input from "../../components/ui/Input";
import Card from "../../components/ui/Card";
import { PageHeaderAddButton } from "../../components/ui/PageHeader";
import { ListRowsSkeleton, PanelLoading } from "../../components/ui/Skeleton";
import { usePermission } from "../../lib/permissions";

import { t } from "../../lib/i18n";

const REQUIRED_SYSTEM_ROLE_PERMISSIONS: Record<string, Set<string>> = {
  owner: new Set([
    "docs.read.all",
    "docs.manage.all",
    "docs.transfer_departed",
    "workspaces.read.all",
  ]),
  admin: new Set(["docs.transfer_departed"]),
};

export default function TeamRoles() {
  const queryClient = useQueryClient();
  const can = usePermission();
  const { setTeamControls } = (useOutletContext() || {}) as {
    setTeamControls?: (controls: { toolbar?: ReactNode; actions?: ReactNode }) => void;
  };
  const canManage = can("users.manage");
  const [expandedRole, setExpandedRole] = useState<string | null>(null);
  const [editingRole, setEditingRole] = useState<any | null>(null);
  const [confirmDeleteRole, setConfirmDeleteRole] = useState<any | null>(null);
  const [reassignTarget, setReassignTarget] = useState<string>("");
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newPerms, setNewPerms] = useState<Set<string>>(new Set());

  const { data: roles = [], isLoading: rolesLoading } = useQuery({
    queryKey: ["staff-roles"],
    queryFn: () => api.staff.roles.list(),
  });

  const { data: catalog = [], isLoading: catalogLoading } = useQuery({
    queryKey: ["permissions-catalog"],
    queryFn: () => api.staff.permissionsCatalog(),
  });

  const createRoleMutation = useMutation({
    mutationFn: () => api.staff.roles.create({ name: newName, permissions: Array.from(newPerms) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["staff-roles"] });
      setCreating(false); setNewName(""); setNewPerms(new Set());
    },
  });

  const updateRoleMutation = useMutation({
    mutationFn: ({ id, permissions, name }: { id: string; permissions?: string[]; name?: string }) =>
      api.staff.roles.update(id, { permissions, name }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["staff-roles"] });
      setEditingRole(null);
    },
  });

  const deleteRoleMutation = useMutation({
    mutationFn: ({ id, reassignTo }: { id: string; reassignTo?: string }) =>
      api.staff.roles.delete(id, reassignTo),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["staff-roles"] });
      queryClient.invalidateQueries({ queryKey: ["staff"] });
      setConfirmDeleteRole(null); setReassignTarget("");
    },
  });

  function togglePerm(key: string, set: Set<string>, setState: (s: Set<string>) => void) {
    const next = new Set(set);
    if (next.has(key)) next.delete(key); else next.add(key);
    setState(next);
  }

  useEffect(() => {
    if (!setTeamControls) return;
    setTeamControls({
      actions: canManage ? (
        <PageHeaderAddButton label={t("page.team_roles.add_role")} onClick={() => setCreating(true)} />
      ) : null,
    });
    return () => setTeamControls({});
  }, [canManage, setTeamControls]);

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto pb-2">
        <div className="flex flex-col gap-3">
          {rolesLoading ? (
            <ListRowsSkeleton rows={5} action />
          ) : roles.map((r: any) => {
            const isExpanded = expandedRole === r.id;
            const isEditing = editingRole?.id === r.id;
            const editPerms: Set<string> = isEditing ? editingRole.permissions : new Set(r.permissions || []);
            const lockedPermissions = REQUIRED_SYSTEM_ROLE_PERMISSIONS[String(r.name || "").toLowerCase()] || new Set<string>();
            return (
              <Card
                key={r.id}
                minHeight={0}
                hoverable={!isExpanded}
                onClick={!isExpanded ? () => setExpandedRole(r.id) : undefined}
                title={
                  <span className="capitalize" style={{ fontSize: 15 }}>{r.name}</span>
                }
                description={
                  <>
                    {r.staff_count} {r.staff_count === 1 ? t("page.team_roles.person") : t("page.team_roles.people")}
                    {" · "}
                    {r.permissions.length} {t("page.team_roles.permission")}{r.permissions.length === 1 ? "" : "s"}
                  </>
                }
                badges={
                  <>
                    {r.is_system && <StatusBadge type="inactive">{t("page.team_roles.system_2")}</StatusBadge>}
                    {r.is_default && <StatusBadge type="info">{t("page.api_keys.default")}</StatusBadge>}
                  </>
                }
                headerAction={
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setExpandedRole(isExpanded ? null : r.id);
                    }}
                    style={{
                      background: "transparent", border: "none", padding: 4,
                      cursor: "pointer", color: "#a8a29e",
                      transform: isExpanded ? "rotate(180deg)" : "none",
                      transition: "transform 0.15s ease",
                    }}
                    aria-label={isExpanded ? t("page.team_roles.collapse") : t("page.team_roles.expand")}
                  >
                    <svg width={18} height={18} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                    </svg>
                  </button>
                }
                footer={
                  isExpanded && canManage ? (
                    <>
                      <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
                        {!isEditing && (
                          <>
                            {!r.is_system && (
                              <Button variant="outline" size="sm" onClick={() => setConfirmDeleteRole(r)}>
                                {t("action.delete")}
                              </Button>
                            )}
                            <Button
                              variant="primary"
                              size="sm"
                              onClick={() =>
                                setEditingRole({
                                  id: r.id,
                                  name: r.name,
                                  permissions: new Set(r.permissions || []),
                                })
                              }
                            >
                              {t("page.team_roles.edit_permissions")}
                            </Button>
                          </>
                        )}
                        {isEditing && (
                          <>
                            <Button variant="outline" size="sm" onClick={() => setEditingRole(null)}>
                              {t("action.cancel")}
                            </Button>
                            <Button
                              variant="primary"
                              size="sm"
                              loading={updateRoleMutation.isPending}
                              onClick={() =>
                                updateRoleMutation.mutate({
                                  id: r.id,
                                  permissions: Array.from(editPerms),
                                })
                              }
                            >
                              {t("action.save")}
                            </Button>
                          </>
                        )}
                      </div>
                    </>
                  ) : undefined
                }
              >
                {isExpanded && (
                  <div style={{
                    marginTop: 12,
                    paddingTop: 12,
                    borderTop: "1px solid #f5f5f4",
                  }}>
                    {r.is_system && isEditing && (
                      <div className="p-3 bg-amber-50/80 border border-amber-300/40 rounded-lg flex items-start gap-2 mb-3">
                        <svg className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                        </svg>
                        <div className="text-xs text-amber-900 leading-relaxed">
                          <strong className="capitalize">{r.name}</strong> {t("page.team_roles.is_a_system_role_changing_its_permissions_affect")}{r.staff_count}{" "}
                          {r.staff_count === 1 ? t("page.team_roles.person") : t("page.team_roles.people")} {t("page.team_roles.today_the_role_can_t_be_renamed_or_deleted")}
                        </div>
                      </div>
                    )}

                    <PermissionChecklist
                      catalog={catalog}
                      loading={catalogLoading}
                      selected={editPerms}
                      locked={lockedPermissions}
                      onToggle={(key) => {
                        if (!canManage || !isEditing || lockedPermissions.has(key)) return;
                        togglePerm(key, editPerms, (s) => setEditingRole({ ...editingRole, permissions: s }));
                      }}
                      readOnly={!canManage || !isEditing}
                    />
                  </div>
                )}
              </Card>
            );
          })}
        </div>

        <Modal
          open={creating}
          onClose={() => { setCreating(false); setNewName(""); setNewPerms(new Set()); }}
          title={t("page.team_roles.create_role")}
          footer={
            <>
              <Button variant="outline" onClick={() => { setCreating(false); setNewName(""); setNewPerms(new Set()); }}>{t("action.cancel")}</Button>
              <Button
                variant="primary"
                disabled={!newName}
                loading={createRoleMutation.isPending}
                onClick={() => createRoleMutation.mutate()}
              >
                {t("action.create")}
              </Button>
            </>
          }
        >
          <div className="space-y-4">
            <Input
              label={t("page.team_roles.role_name")}
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={t("page.team_roles.e_g_finance_lead")}
            />
            <PermissionChecklist
              catalog={catalog}
              loading={catalogLoading}
              selected={newPerms}
              onToggle={(key) => togglePerm(key, newPerms, setNewPerms)}
            />
            {createRoleMutation.isError && (
              <p className="text-red-600 text-sm">{(createRoleMutation.error as Error).message}</p>
            )}
          </div>
        </Modal>

        <Modal
          open={!!confirmDeleteRole}
          onClose={() => { setConfirmDeleteRole(null); setReassignTarget(""); }}
          title={t("page.team_roles.delete_role_title").replace("{name}", confirmDeleteRole?.name ?? "")}
          footer={
            <>
              <Button variant="outline" onClick={() => { setConfirmDeleteRole(null); setReassignTarget(""); }}>{t("action.cancel")}</Button>
              <Button
                variant="danger"
                loading={deleteRoleMutation.isPending}
                onClick={() => deleteRoleMutation.mutate({
                  id: confirmDeleteRole.id,
                  reassignTo: reassignTarget || undefined,
                })}
              >
                {t("action.delete")}
              </Button>
            </>
          }
        >
          <div className="space-y-4">
            <p className="text-sm text-stone-600">
              <strong>{confirmDeleteRole?.staff_count ?? 0}</strong> {t("page.team_roles.people_have_this_role_reassign_them_to_another_r")}
            </p>
            <div>
              <label className="block text-sm font-medium text-stone-600 mb-1">{t("page.team_roles.reassign_to")}</label>
              <select
                value={reassignTarget}
                onChange={(e) => setReassignTarget(e.target.value)}
                className="manor-input"
              >
                <option value="">{t("page.team_roles.leave_without_role")}</option>
                {roles.filter((r: any) => r.id !== confirmDeleteRole?.id).map((r: any) => (
                  <option key={r.id} value={r.id}>{r.name}</option>
                ))}
              </select>
            </div>
          </div>
        </Modal>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────
//   PermissionChecklist — reusable within the Roles page
// ──────────────────────────────────────────────────────────────────────────

function PermissionChecklist({
  catalog, selected, locked = new Set<string>(), onToggle, readOnly = false, loading = false,
}: {
  catalog: { name: string; permissions: { key: string; label: string }[] }[];
  selected: Set<string>;
  locked?: Set<string>;
  onToggle: (key: string) => void;
  readOnly?: boolean;
  loading?: boolean;
}) {
  if (loading) {
    return <PanelLoading rows={3} minHeight={180} />;
  }

  return (
    <div className="team-permission-checklist">
      {catalog.map((group) => (
        <div className="team-permission-group" key={group.name}>
          <div className="team-permission-group-label">
            {group.name}
          </div>
          <div className="team-permission-grid">
            {group.permissions.map((p) => {
              const checked = selected.has(p.key);
              const disabled = readOnly || locked.has(p.key);
              return (
                <label
                  key={p.key}
                  className={[
                    "team-permission-item",
                    checked && "is-checked",
                    disabled && !checked && "is-muted",
                    disabled && "is-readonly",
                  ].filter(Boolean).join(" ")}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={disabled}
                    onChange={() => !disabled && onToggle(p.key)}
                    style={{ accentColor: "#436b65", flexShrink: 0 }}
                  />
                  <span className="team-permission-label">{p.label}</span>
                </label>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
