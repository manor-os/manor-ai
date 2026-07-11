/**
 * PersonRow — single row in the "People with access" list.
 *
 * Built on project primitives (<UserAvatar>, <Select>) so visuals match
 * the rest of Manor. Workspace-inherited grants are shown but locked
 * (no role dropdown, no trash) so users understand they need to go to
 * the workspace settings to revoke.
 */
import { IconTrash } from "../icons";
import { t } from "../../lib/i18n";
import Select from "../ui/Select";
import UserAvatar from "../ui/UserAvatar";

export type ShareRole = "viewer" | "commenter" | "editor" | "curator";

function _roleOptions(): { value: ShareRole; label: string; hint: string }[] {
  return [
    { value: "viewer", label: t("permissions.role.viewer.label"), hint: t("permissions.role.viewer.hint") },
    { value: "commenter", label: t("permissions.role.commenter.label"), hint: t("permissions.role.commenter.hint") },
    { value: "editor", label: t("permissions.role.editor.label"), hint: t("permissions.role.editor.hint") },
    { value: "curator", label: t("permissions.role.curator.label"), hint: t("permissions.role.curator.hint") },
  ];
}

interface Props {
  /** Display name. */
  name: string;
  email: string;
  avatarUrl?: string | null;
  role: ShareRole;
  /** Where the grant came from — affects whether row is locked. */
  source: "explicit" | "workspace";
  expiresAt?: string;
  /** Loading flag for in-flight role change or remove. */
  busy?: boolean;
  onRoleChange?: (role: ShareRole) => void;
  onRemove?: () => void;
}

export default function PersonRow({
  name,
  email,
  avatarUrl,
  role,
  source,
  expiresAt,
  busy,
  onRoleChange,
  onRemove,
}: Props) {
  const locked = source === "workspace";
  const expired = !!expiresAt && new Date(expiresAt) < new Date();
  const roleOpts = _roleOptions();

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "32px 1fr 140px 28px",
        alignItems: "center",
        gap: 10,
        padding: "8px 6px",
        borderRadius: 6,
        opacity: busy ? 0.5 : 1,
        transition: "background 0.12s",
      }}
    >
      <UserAvatar name={name} avatarUrl={avatarUrl} size={32} />

      <div style={{ minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "#292524",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {name}
          {expired && (
            <span style={{ marginLeft: 6, fontSize: 10, color: "#a23e38", fontWeight: 500 }}>
              {t("permissions.row.expired")}
            </span>
          )}
        </div>
        <div
          style={{
            fontSize: 11,
            color: "#78716c",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {email}
          {source === "workspace" && ` · ${t("permissions.row.workspace_inherited")}`}
          {expiresAt && !expired && ` · ${t("permissions.row.expires_on", { date: new Date(expiresAt).toLocaleDateString() })}`}
        </div>
      </div>

      {locked ? (
        <span style={{ fontSize: 12, color: "#a8a29e", textAlign: "center" }}>
          {roleOpts.find((r) => r.value === role)?.label ?? role}
        </span>
      ) : (
        <div title={roleOpts.find((r) => r.value === role)?.hint}>
          <Select
            value={role}
            onChange={(v) => onRoleChange?.(v as ShareRole)}
            options={roleOpts.map((r) => ({ value: r.value, label: r.label }))}
          />
        </div>
      )}

      {!locked && onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          disabled={busy}
          aria-label={t("permissions.action.remove")}
          title={t("permissions.action.remove")}
          style={{
            width: 26,
            height: 26,
            borderRadius: 6,
            background: "transparent",
            border: "none",
            cursor: "pointer",
            color: "#a8a29e",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.background = "rgba(241,221,219,0.6)";
            (e.currentTarget as HTMLElement).style.color = "#c14a44";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.background = "transparent";
            (e.currentTarget as HTMLElement).style.color = "#a8a29e";
          }}
        >
          <IconTrash size={14} />
        </button>
      ) : (
        <span />
      )}
    </div>
  );
}
