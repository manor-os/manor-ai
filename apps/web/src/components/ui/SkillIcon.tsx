const SKILL_COLORS = [
  { bg: "#efedea", fg: "#57534e" },
  { bg: "#e3e9f1", fg: "#3f57a0" },
  { bg: "#f3e5ed", fg: "#a04a72" },
  { bg: "#ece9f5", fg: "#6443a0" },
  { bg: "#f3ecd6", fg: "#936027" },
  { bg: "#dceae3", fg: "#3f7361" },
  { bg: "#e8eff4", fg: "#426c87" },
  { bg: "#f1dddb", fg: "#a23e38" },
];

const SKILL_CATEGORY_ICONS: Record<string, string> = {
  analysis: "M21 21l-4.35-4.35M11 18a7 7 0 110-14 7 7 0 010 14z",
  research: "M21 21l-4.35-4.35M11 18a7 7 0 110-14 7 7 0 010 14z",
  communication: "M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z",
  data: "M12 3c4.97 0 9 1.34 9 3s-4.03 3-9 3-9-1.34-9-3 4.03-3 9-3zM3 6v6c0 1.66 4.03 3 9 3s9-1.34 9-3V6M3 12v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6",
  operations: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  development: "M16 18l6-6-6-6M8 6l-6 6 6 6",
  automation: "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  "brand-design": "M12 19l7-7 3 3-7 7-3-3zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5zM2 2l7.586 7.586M11 13a2 2 0 11-4 0 2 2 0 014 0z",
  launch: "M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 00-2.91-.09zM12 15l-3-3a22 22 0 012-3.95A12.88 12.88 0 0122 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 01-4 2z",
  growth: "M23 6l-9.5 9.5-5-5L1 18M17 6h6v6",
  marketing: "M3 11l18-5v12L3 14v-3zM11.6 16.8a3 3 0 11-5.8-1.6",
  media: "M19 3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V5a2 2 0 00-2-2zM8.5 10a1.5 1.5 0 100-3 1.5 1.5 0 000 3zM21 15l-5-5L5 21",
  docs: "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8l-6-6zM14 2v6h6M16 13H8M16 17H8M10 9H8",
  finance: "M12 1v22M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6",
};

const SKILL_DEFAULT_ICON =
  "M9 18h6M10 21h4M12 3a6 6 0 00-3.6 10.8c.5.37.85.9.95 1.5l.15.7h5l.15-.7c.1-.6.45-1.13.95-1.5A6 6 0 0012 3z";

function hashString(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash);
}

function skillColor(seed: string) {
  return SKILL_COLORS[hashString(seed || "skill") % SKILL_COLORS.length];
}

function skillIconPath(skill: any): string {
  const cat = String(skill?.category || "").toLowerCase().trim();
  if (cat && SKILL_CATEGORY_ICONS[cat]) return SKILL_CATEGORY_ICONS[cat];
  const hay = `${skill?.name || ""} ${skill?.slug || ""} ${cat}`.toLowerCase();
  const keywords: Array<[RegExp, string]> = [
    [/research|search|scan|find|scrape/, "research"],
    [/mail|email|message|chat|slack|notif|reply|comm/, "communication"],
    [/sheet|table|data|sql|dataset|csv|extract/, "data"],
    [/code|dev|api|script|deploy|git/, "development"],
    [/slide|deck|doc|write|report|summary|pdf|note/, "docs"],
    [/image|photo|video|media|design|logo|brand/, "media"],
    [/finance|invoice|payment|budget|expense|account/, "finance"],
    [/market|seo|campaign|content|social|post/, "marketing"],
    [/auto|schedule|workflow|trigger|cron/, "automation"],
    [/launch|publish|release|ship/, "launch"],
    [/growth|trend|analy|metric|insight/, "growth"],
  ];
  for (const [re, key] of keywords) {
    if (re.test(hay)) return SKILL_CATEGORY_ICONS[key];
  }
  return SKILL_DEFAULT_ICON;
}

export default function SkillIcon({ skill, size = 52 }: { skill: any; size?: number }) {
  const c = skillColor(skill?.category || skill?.name || "");
  return (
    <div
      style={{
        width: size,
        height: size,
        minWidth: size,
        borderRadius: Math.round(size * 0.25),
        background: c.bg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        transition: "transform 0.25s",
      }}
    >
      <svg
        style={{ width: size * 0.46, height: size * 0.46, color: c.fg }}
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={skillIconPath(skill)} />
      </svg>
    </div>
  );
}
