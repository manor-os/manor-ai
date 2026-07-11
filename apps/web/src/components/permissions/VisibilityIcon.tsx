/**
 * VisibilityIcon — P0 global element.
 *
 * Lower-information than ClassificationBadge. Shown beside file names so
 * users can tell at a glance whether a doc is private to them, scoped to
 * their workspace, visible to the whole entity, or fully public.
 *
 * See docs/PERMISSIONS_UX_DESIGN_ZH.md §1.2.
 */
import { IconLock, IconHome, IconBuilding, IconGlobe } from "../icons";
import type { Visibility } from "../../lib/types";
import { t } from "../../lib/i18n";

const VISIBILITY_CONFIG: Record<
  Visibility,
  {
    Icon: React.ComponentType<{ size?: number; className?: string }>;
    color: string;
    titleKey: string;
  }
> = {
  private: {
    Icon: IconLock,
    color: "#78716c",
    titleKey: "permissions.visibility.private.tooltip",
  },
  workspace: {
    Icon: IconHome,
    color: "#436b65",
    titleKey: "permissions.visibility.workspace.tooltip",
  },
  entity: {
    Icon: IconBuilding,
    color: "#3f57a0",
    titleKey: "permissions.visibility.entity.tooltip",
  },
  public: {
    Icon: IconGlobe,
    color: "#9333ea",
    titleKey: "permissions.visibility.public.tooltip",
  },
};

interface Props {
  visibility?: Visibility | string | null;
  size?: number;
  /** Override the default tooltip (e.g. include workspace name). */
  title?: string;
}

export default function VisibilityIcon({
  visibility,
  size = 14,
  title,
}: Props) {
  if (!visibility) return null;
  const cfg = VISIBILITY_CONFIG[visibility as Visibility];
  if (!cfg) return null;
  const Icon = cfg.Icon;
  return (
    <span
      title={title ?? t(cfg.titleKey)}
      aria-label={title ?? t(cfg.titleKey)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        color: cfg.color,
        cursor: "help",
      }}
    >
      <Icon size={size} />
    </span>
  );
}
