/**
 * CategoryChip — slate pill with the category's icon + label. Used
 * anywhere a task's category is displayed (sidebar Details, list rows,
 * filter pills). Hides itself when no category key resolves so a
 * missing / unknown key doesn't leave a blank chip.
 */
import { getCategory } from "../../lib/taskCategories";
import { t } from "../../lib/i18n";

interface CategoryChipProps {
  categoryKey?: string | null;
  size?: "sm" | "md";
}

export default function CategoryChip({ categoryKey, size = "md" }: CategoryChipProps) {
  const cat = getCategory(categoryKey);
  if (!cat) return null;
  const Icon = cat.Icon;
  const fs = size === "sm" ? 10 : 11;
  const iconSize = size === "sm" ? 10 : 12;
  const pad = size === "sm" ? "2px 7px" : "3px 10px";

  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: pad, borderRadius: 20, fontSize: fs, fontWeight: 600,
      color: "#57534e", background: "#f5f5f4",
      letterSpacing: "0.01em",
    }}>
      <Icon size={iconSize} />
      {t(cat.labelKey)}
    </span>
  );
}
