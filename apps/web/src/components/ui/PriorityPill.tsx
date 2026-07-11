/**
 * PriorityPill — per-level icon + label chip.
 *
 * Each level has its own glyph so colour-blind users (and users on
 * monochrome printouts) can still tell priorities apart at a glance.
 *
 * Usage:
 *   <PriorityPill priority={5} />
 *   <PriorityPill priority={3} size="sm" />
 */
import { IconBolt, IconArrowUp, IconArrowRight, IconArrowDown, IconCircleDot } from "../icons";
import { t } from "../../lib/i18n";

type PriorityEntry = {
  labelKey: string;
  color: string;
  Icon: React.ComponentType<{ size?: number; className?: string; style?: React.CSSProperties }>;
};

const PRIORITY_CONFIG: Record<number, PriorityEntry> = {
  5: { labelKey: "component.priority.critical", color: "#d65f59", Icon: IconBolt },
  4: { labelKey: "component.priority.high",     color: "#d3873f", Icon: IconArrowUp },
  3: { labelKey: "component.priority.medium",   color: "#c3a63f", Icon: IconArrowRight },
  2: { labelKey: "component.priority.low",      color: "#8aa9d1", Icon: IconArrowDown },
  1: { labelKey: "component.priority.minimal",  color: "#a8a29e", Icon: IconCircleDot },
};

interface PriorityPillProps {
  priority: number;
  size?: "sm" | "md";
}

export default function PriorityPill({ priority, size = "md" }: PriorityPillProps) {
  const cfg = PRIORITY_CONFIG[priority] || PRIORITY_CONFIG[3];
  const fs = size === "sm" ? 9 : 11;
  const iconSize = size === "sm" ? 10 : 12;
  const pad = size === "sm" ? "2px 7px" : "3px 10px";
  const Icon = cfg.Icon;
  // Distinct glyph + coloured icon carry the priority; the chip stays
  // neutral. Only the top level (critical) colours its label for emphasis.
  const isCritical = priority >= 5;

  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: pad, borderRadius: 20, fontSize: fs, fontWeight: 600,
      color: isCritical ? cfg.color : "#57534e",
      background: "#f5f5f4", letterSpacing: "0.01em",
    }}>
      <Icon size={iconSize} style={{ color: cfg.color }} />
      {t(cfg.labelKey)}
    </span>
  );
}

export { PRIORITY_CONFIG };
