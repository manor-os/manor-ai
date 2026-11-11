import { Fragment, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import Modal from "../../components/ui/Modal";
import Button from "../../components/ui/Button";
import Input from "../../components/ui/Input";
import { ConfigAddRow, ConfigBlock, ConfigGroup, ConfigPage, ConfigRow } from "../../components/ui/GroupedList";
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

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto pb-2">
        <ConfigPage wide>
          <ConfigGroup>
            {rolesLoading ? (
              <ConfigBlock><ListRowsSkeleton rows={5} action /></ConfigBlock>
            ) : roles.map((r: any) => {
              const isExpanded = expandedRole === r.id;
              const isEditing = editingRole?.id === r.id;
              const editPerms: Set<string> = isEditing ? editingRole.permissions : new Set(r.permissions || []);
              const lockedPermissions = REQUIRED_SYSTEM_ROLE_PERMISSIONS[String(r.name || "").toLowerCase()] || new Set<string>();
              return (
                <Fragment key={r.id}>
                  <ConfigRow
                    label={<span className="capitalize">{r.name}</span>}
                    note={(
                      <>
                        {r.staff_count} {r.staff_count === 1 ? t("page.team_roles.person") : t("page.team_roles.people")}
                        {" · "}
                        {r.permissions.length} {t("page.team_roles.permission")}{r.permissions.length === 1 ? "" : "s"}
                      </>
                    )}
                    meta={[
                      r.is_system ? t("page.team_roles.system_2") : null,
                      r.is_default ? t("page.api_keys.default") : null,
                    ].filter(Boolean).join(" · ") || undefined}
                    expanded={isExpanded}
                    onClick={() => setExpandedRole(isExpanded ? null : r.id)}
                  />
                  {isExpanded && (
                    <div className="team-role-detail">
                      {r.is_system && isEditing && (
                        <p className="team-role-warning">
                          <strong className="capitalize">{r.name}</strong> {t("page.team_roles.is_a_system_role_changing_its_permissions_affect")} {r.staff_count}{" "}
                          {r.staff_count === 1 ? t("page.team_roles.person") : t("page.team_roles.people")} {t("page.team_roles.today_the_role_can_t_be_renamed_or_deleted")}
                        </p>
                      )}
                      <PermissionChecklist
                        catalog={catalog}
                        loading={catalogLoading}
                        selected={editPerms}
                        locked={lockedPermissions}
                        onToggle={(key) => {
                          if (!canManage || !isEditing || lockedPermissions.has(key)) return;
                          togglePerm(key, editPerms, (next) => setEditingRole({ ...editingRole, permissions: next }));
                        }}
                        readOnly={!canManage || !isEditing}
                      />
                      {canManage && (
                        <div className="team-role-actions">
                          {!isEditing && !r.is_system && (
                            <Button variant="ghost" size="sm" onClick={() => setConfirmDeleteRole(r)}>
                              {t("action.delete")}
                            </Button>
                          )}
                          {!isEditing && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => setEditingRole({ id: r.id, name: r.name, permissions: new Set(r.permissions || []) })}
                            >
                              {t("page.team_roles.edit_permissions")}
                            </Button>
                          )}
                          {isEditing && (
                            <Button variant="ghost" size="sm" onClick={() => setEditingRole(null)}>
                              {t("action.cancel")}
                            </Button>
                          )}
                          {isEditing && (
                            <Button
                              variant="primary"
                              size="sm"
                              loading={updateRoleMutation.isPending}
                              onClick={() => updateRoleMutation.mutate({ id: r.id, permissions: Array.from(editPerms) })}
                            >
                              {t("action.save")}
                            </Button>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </Fragment>
              );
            })}
            {canManage && <ConfigAddRow label={t("page.team_roles.add_role")} onClick={() => setCreating(true)} />}
          </ConfigGroup>
        </ConfigPage>

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
    <div className="team-permission-list">
      {catalog.map((group) => {
        const granted = group.permissions.filter((p) => selected.has(p.key));
        return (
          <div className="team-permission-row" key={group.name}>
            <div className="team-permission-name">{group.name}</div>
            {readOnly ? (
              <div className={`team-permission-value${granted.length ? "" : " is-empty"}`}>
                {granted.length ? granted.map((p) => p.label).join(" · ") : "—"}
              </div>
            ) : (
              <div className="team-permission-choices">
                {group.permissions.map((p) => {
                  const checked = selected.has(p.key);
                  return (
                    <button
                      key={p.key}
                      type="button"
                      aria-pressed={checked}
                      disabled={locked.has(p.key)}
                      className={`team-permission-choice${checked ? " is-on" : ""}`}
                      onClick={() => onToggle(p.key)}
                    >
                      {p.label}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
