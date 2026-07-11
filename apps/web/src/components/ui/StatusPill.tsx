/**
 * StatusPill — colored icon + label chip for task / job status.
 *
 * Usage:
 *   <StatusPill status="in_progress" />
 *   <StatusPill label="Active" color="#4f9c84" />
 */
import {
  IconPlus, IconClock, IconCalendar, IconPlay, IconHeadphones, IconPause,
  IconWarning, IconCheckCircle, IconClose, IconError, IconCircleDot,
} from "../icons";
import { t } from "../../lib/i18n";

type StatusEntry = {
  labelKey: string;
  color: string;
  bg: string;
  Icon: React.ComponentType<{ size?: number; className?: string; style?: React.CSSProperties }>;
};

const STATUS_CONFIG: Record<string, StatusEntry> = {
  created:              { labelKey: "component.status.created",     color: "#78716c", bg: "#f5f5f4", Icon: IconPlus },
  proposed:             { labelKey: "component.status.proposed",    color: "#6f4ba8", bg: "#ece9f5", Icon: IconCircleDot },
  pending:              { labelKey: "component.status.pending",     color: "#b27c34", bg: "#f3ecd6", Icon: IconClock },
  scheduled:            { labelKey: "component.status.scheduled",   color: "#6f4ba8", bg: "#ece9f5", Icon: IconCalendar },
  in_progress:          { labelKey: "component.status.in_progress", color: "#426c87", bg: "#e8eff4", Icon: IconPlay },
  waiting_on_customer:  { labelKey: "component.status.waiting",     color: "#b66a3c", bg: "#ffedd5", Icon: IconHeadphones },
  on_hold:              { labelKey: "component.status.on_hold",     color: "#6f4ba8", bg: "#ece9f5", Icon: IconPause },
  blocked:              { labelKey: "component.status.blocked",     color: "#c14a44", bg: "#f1dddb", Icon: IconWarning },
  completed:            { labelKey: "component.status.completed",   color: "#437f6b", bg: "#dceae3", Icon: IconCheckCircle },
  cancelled:            { labelKey: "component.status.cancelled",   color: "#57534e", bg: "#e7e5e4", Icon: IconClose },
  failed:               { labelKey: "component.status.failed",      color: "#be123c", bg: "#ffe4e6", Icon: IconError },
};

interface StatusPillProps {
  status?: string;
  label?: string;
  color?: string;
  bg?: string;
  size?: "sm" | "md";
}

// Statuses that warrant colour on the label itself — they need attention.
const ALERT_STATUSES = new Set(["blocked", "failed"]);

export default function StatusPill({ status, label, color, bg, size = "md" }: StatusPillProps) {
  const cfg = status ? STATUS_CONFIG[status] : null;
  // The status colour now lives on the icon only — a small, meaningful signal.
  const iconColor = color || cfg?.color || "#78716c";
  const isAlert = status ? ALERT_STATUSES.has(status) : false;
  // Neutral surface + neutral label by default; alerts keep their colour.
  const textColor = color ? iconColor : isAlert ? iconColor : "#57534e";
  const b = bg || (isAlert ? cfg?.bg : "#f5f5f4") || "#f5f5f4";
  const l = label || (cfg?.labelKey ? t(cfg.labelKey) : status) || "";
  const Icon = cfg?.Icon || IconCircleDot;
  const fs = size === "sm" ? 10 : 11;
  const iconSize = size === "sm" ? 10 : 12;
  const pad = size === "sm" ? "2px 7px" : "3px 10px";

  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      padding: pad, borderRadius: 20, fontSize: fs, fontWeight: 600,
      color: textColor, background: b, letterSpacing: "0.01em",
    }}>
      <Icon size={iconSize} style={{ color: iconColor }} />
      {l}
    </span>
  );
}

export { STATUS_CONFIG };
