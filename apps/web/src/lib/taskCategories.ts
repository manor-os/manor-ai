/**
 * Shared task category catalog — single source of truth for the 33
 * built-in categories. Used by:
 *   - TaskDetail Properties picker
 *   - Task drawer
 *   - Tasks page filter pills
 *
 * Each entry has a key (DB value), a human label, and an Icon component
 * picked from the global icon library so the category chip is
 * recognisable at a glance even when the colour all blends together
 * (long category column, colourblind users, dark backgrounds, etc.).
 */
import {
  IconGear, IconWrench, IconHome, IconClipboard, IconShield, IconHeadphones,
  IconChat, IconWarning, IconUserPlus, IconDollar, IconShoppingCart,
  IconCreditCard, IconPeople, IconAcademicCap, IconCode, IconServer,
  IconBug, IconTerminal, IconMegaphone, IconDocument, IconPalette,
  IconShare, IconTruck, IconBox, IconFacilities, IconScale, IconChecklist,
  IconRocket, IconCalendar, IconBeaker, IconTag,
} from "../components/icons";

export type CategoryEntry = {
  key: string;
  label: string;
  labelKey: string;
  Icon: React.ComponentType<{ size?: number; className?: string; style?: React.CSSProperties }>;
};

function category(key: string, label: string, Icon: CategoryEntry["Icon"]): CategoryEntry {
  return { key, label, labelKey: `task.category.${key}`, Icon };
}

export const CATEGORIES: CategoryEntry[] = [
  category("operations",       "Operations",       IconGear),
  category("maintenance",      "Maintenance",      IconWrench),
  category("housekeeping",     "Housekeeping",     IconHome),
  category("inspection",       "Inspection",       IconClipboard),
  category("security",         "Security",         IconShield),
  category("support",          "Support",          IconHeadphones),
  category("customer_request", "Customer Request", IconChat),
  category("complaint",        "Complaint",        IconWarning),
  category("onboarding",       "Onboarding",       IconUserPlus),
  category("sales",            "Sales",            IconDollar),
  category("finance",          "Finance",          IconDollar),
  category("procurement",      "Procurement",      IconShoppingCart),
  category("billing",          "Billing",          IconCreditCard),
  category("hr",               "HR",               IconPeople),
  category("training",         "Training",         IconAcademicCap),
  category("recruitment",      "Recruitment",      IconUserPlus),
  category("development",      "Development",      IconCode),
  category("it",               "IT",               IconServer),
  category("bug",              "Bug Fix",          IconBug),
  category("devops",           "DevOps",           IconTerminal),
  category("marketing",        "Marketing",        IconMegaphone),
  category("content",          "Content",          IconDocument),
  category("design",           "Design",           IconPalette),
  category("social_media",     "Social Media",     IconShare),
  category("logistics",        "Logistics",        IconTruck),
  category("inventory",        "Inventory",        IconBox),
  category("facilities",       "Facilities",       IconFacilities),
  category("compliance",       "Compliance",       IconScale),
  category("legal",            "Legal",            IconScale),
  category("audit",            "Audit",            IconChecklist),
  category("project",          "Project",          IconRocket),
  category("meeting",          "Meeting",          IconCalendar),
  category("research",         "Research",         IconBeaker),
  category("other",            "Other",            IconTag),
];

const CATEGORY_BY_KEY: Record<string, CategoryEntry> = Object.fromEntries(
  CATEGORIES.map((c) => [c.key, c]),
);

export function getCategory(key: string | null | undefined): CategoryEntry | undefined {
  if (!key) return undefined;
  return CATEGORY_BY_KEY[key];
}
