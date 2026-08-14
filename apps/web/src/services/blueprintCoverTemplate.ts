import type { BlueprintCoverTemplate } from "../lib/api";

export type BlueprintCoverTheme = "white" | "dark";

const LEGACY_GENERATED_COVER_PREFIX = "/assets/blueprints/generated/";
const OIL_PAINTING_COVER_BASE = "/assets/blueprints/oil-paintings/";
const OIL_PAINTING_COVER_VERSION = "diverse-scenes-v4";
const coverCache = new Map<string, string>();

interface BlueprintCoverSource {
  id: string;
  title: string;
  summary?: string | null;
  description?: string | null;
  tags: string[];
  cover_template?: BlueprintCoverTemplate;
}

export type BlueprintCoverScene =
  | "automation-launch"
  | "content-distribution"
  | "creator-fundraising"
  | "creator-partnership"
  | "digital-store"
  | "product-video"
  | "productized-service"
  | "stickman-storyboard"
  | "video-account"
  | "workspace-system";

export type ResolvedBlueprintCoverTemplate = BlueprintCoverTemplate & {
  scene: BlueprintCoverScene;
};

const OIL_PAINTING_COVERS: Partial<Record<BlueprintCoverScene, string>> = {
  "automation-launch": "automation-launch.webp",
  "content-distribution": "content-distribution.png",
  "creator-fundraising": "creator-fundraising.webp",
  "creator-partnership": "creator-partnership.webp",
  "digital-store": "digital-store.png",
  "product-video": "product-video.png",
  "productized-service": "productized-service.png",
  "stickman-storyboard": "stickman-storyboard.png",
  "video-account": "video-account.png",
};

const SCENE_SIGNALS: ReadonlyArray<[
  BlueprintCoverScene,
  readonly string[],
]> = [
  ["stickman-storyboard", ["stickman", "faceless", "火柴人", "无脸"]],
  ["product-video", ["product video", "product-video", "产品视频", "product capture"]],
  ["video-account", ["video account", "short video account", "视频账号", "短视频账号"]],
  ["automation-launch", ["automation launch", "launch studio", "launch workflow", "自动化发布", "自动化启动"]],
  ["creator-fundraising", ["fundraising", "investor", "diligence", "融资", "投资人", "尽调"]],
  ["creator-partnership", ["partnership", "deal room", "sponsorship", "合作", "品牌交易"]],
  ["digital-store", ["digital product store", "store os", "product store", "数字产品商店", "商店运营"]],
  ["productized-service", ["productized service", "service os", "产品化服务", "服务产品化"]],
  ["content-distribution", ["content distribution", "content studio and distribution", "distribution studio", "内容分发", "内容工作室"]],
];

const FALLBACK_MOTIF_SIGNALS: ReadonlyArray<[
  BlueprintCoverTemplate["motif"],
  readonly string[],
]> = [
  ["distribution", ["distribution", "dispatch", "publish", "social", "分发", "发布", "社媒"]],
  ["video", ["video", "camera", "film", "youtube", "视频", "拍摄", "剪辑"]],
  ["commerce", ["store", "shop", "commerce", "inventory", "checkout", "商店", "电商", "库存"]],
  ["service", ["service", "client", "revenue", "consulting", "服务", "客户", "收入"]],
  ["analytics", ["analytics", "analysis", "testing", "metrics", "分析", "测试", "指标"]],
  ["content", ["content", "editorial", "writing", "script", "内容", "写作", "脚本"]],
];

function stableFallbackSeed(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function resolveCoverScene(
  semanticText: string,
  motif: BlueprintCoverTemplate["motif"],
): BlueprintCoverScene {
  for (const [scene, signals] of SCENE_SIGNALS) {
    if (signals.some((signal) => semanticText.includes(signal))) return scene;
  }

  switch (motif) {
    case "video":
      return "product-video";
    case "distribution":
    case "content":
      return "content-distribution";
    case "commerce":
      return "digital-store";
    case "service":
      return "productized-service";
    default:
      return "workspace-system";
  }
}

/** Keep mixed-version deployments usable while the API gains cover_template. */
export function resolveBlueprintCoverTemplate(
  blueprint: BlueprintCoverSource,
): ResolvedBlueprintCoverTemplate {
  const semanticText = [
    blueprint.title,
    blueprint.description,
    blueprint.summary,
    ...blueprint.tags,
  ].filter(Boolean).join(" ").toLocaleLowerCase();

  if (blueprint.cover_template) {
    return {
      ...blueprint.cover_template,
      scene: resolveCoverScene(semanticText, blueprint.cover_template.motif),
    };
  }

  let motif: BlueprintCoverTemplate["motif"] = "workspace";
  let bestScore = 0;
  for (const [candidate, signals] of FALLBACK_MOTIF_SIGNALS) {
    const score = signals.reduce(
      (total, signal) => total + semanticText.split(signal).length - 1,
      0,
    );
    if (score > bestScore) {
      motif = candidate;
      bestScore = score;
    }
  }

  const seed = stableFallbackSeed(`${blueprint.id}:${semanticText}`);
  const palettes = ["sage", "peach", "blue", "stone"] as const;
  return {
    motif,
    palette: palettes[(seed >>> 8) % palettes.length],
    variant: (seed % 3) as 0 | 1 | 2,
    seed,
    scene: resolveCoverScene(semanticText, motif),
  };
}

const PALETTES = {
  white: {
    background: "#ffffff",
    line: "#2f302e",
    secondaryLine: "#6f716d",
    sage: "#b9d3c8",
    peach: "#ecc6b2",
    blue: "#b8cae2",
    stone: "#d8c9b5",
  },
  dark: {
    background: "#090909",
    line: "#f2f0ec",
    secondaryLine: "#aaa7a1",
    sage: "#334841",
    peach: "#543a31",
    blue: "#34445a",
    stone: "#4a4035",
  },
} as const;

const BLOB_PATHS = [
  [
    "M288 281C323 205 443 185 529 228C613 270 640 380 583 463C526 548 379 560 298 492C220 427 242 345 288 281Z",
    "M724 154C796 105 912 126 947 201C981 274 918 343 830 342C742 341 661 273 677 213C684 187 698 172 724 154Z",
    "M796 476C866 430 976 455 1005 528C1034 602 965 660 881 642C798 625 738 541 796 476Z",
    "M158 511C197 468 275 474 304 527C334 580 288 634 224 627C160 619 119 554 158 511Z",
  ],
  [
    "M319 245C397 185 530 201 595 280C658 357 617 477 512 521C407 564 278 510 257 414C242 348 271 282 319 245Z",
    "M753 128C836 91 941 133 960 214C978 294 894 347 810 320C727 293 674 185 753 128Z",
    "M740 501C810 438 927 441 980 506C1034 571 989 658 899 673C808 687 676 576 740 501Z",
    "M149 259C183 214 261 213 299 260C338 308 301 371 239 375C177 379 112 309 149 259Z",
  ],
  [
    "M272 315C311 214 457 181 552 244C648 307 644 443 550 508C456 572 306 535 260 438C241 398 249 357 272 315Z",
    "M765 163C847 111 957 151 979 236C1000 318 916 376 831 347C746 319 691 211 765 163Z",
    "M780 480C852 427 957 466 984 544C1010 620 931 672 849 642C769 613 720 536 780 480Z",
    "M157 472C199 424 278 440 310 498C341 555 291 613 225 603C159 594 113 522 157 472Z",
  ],
] as const;

function accentOrder(template: BlueprintCoverTemplate) {
  const all = ["sage", "peach", "blue", "stone"] as const;
  const start = all.indexOf(template.palette);
  return [...all.slice(start), ...all.slice(0, start)];
}

function sceneLabel(scene: BlueprintCoverScene): string {
  return {
    "automation-launch": "LAUNCH AUTOMATION",
    "content-distribution": "CONTENT DISTRIBUTION",
    "creator-fundraising": "FUNDRAISING ROOM",
    "creator-partnership": "PARTNERSHIP PIPELINE",
    "digital-store": "DIGITAL COMMERCE",
    "product-video": "PRODUCT FILM",
    "productized-service": "CLIENT DELIVERY",
    "stickman-storyboard": "STORYBOARD SYSTEM",
    "video-account": "CREATOR CHANNEL",
    "workspace-system": "WORKSPACE SYSTEM",
  }[scene];
}

/** Each scene is a semantic illustration, not a decorative category icon. */
function sceneArtwork(
  scene: BlueprintCoverScene,
  surface: string,
  accent: string,
): string {
  switch (scene) {
    case "stickman-storyboard":
      return `<g>
        <rect x="205" y="185" width="790" height="420" rx="36" fill="${surface}" opacity="0.72" />
        <path d="M468 185V605M733 185V605" opacity="0.2" />
        <circle cx="350" cy="310" r="42" fill="${accent}" opacity="0.35" />
        <path d="M350 352V463M350 380L282 421M350 380L414 329M350 463L298 535M350 463L411 523" />
        <circle cx="600" cy="300" r="38" /><path d="M600 338V453M600 371L530 334M600 371L671 411M600 453L545 523M600 453L652 528" />
        <circle cx="864" cy="304" r="40" fill="${accent}" opacity="0.22" /><path d="M864 344V455M864 378L807 428M864 378L930 341M864 455L816 526M864 455L922 513" />
        <path d="M247 567H953M247 567L247 585M491 567V585M737 567V585M953 567V585" opacity="0.38" />
      </g>`;
    case "product-video":
      return `<g>
        <path d="M176 248V181H263M937 181H1024V248M176 548V615H263M937 615H1024V548" opacity="0.52" />
        <ellipse cx="570" cy="567" rx="258" ry="48" fill="${accent}" opacity="0.28" />
        <path d="M454 304C454 272 480 246 512 246H642C674 246 700 272 700 304V511H454V304Z" fill="${surface}" opacity="0.82" />
        <path d="M492 246V210H662V246M511 327H642M511 374H623" opacity="0.45" />
        <path d="M782 327L897 278V469L782 420Z" fill="${surface}" opacity="0.56" />
        <circle cx="821" cy="522" r="54" fill="${accent}" opacity="0.4" /><path d="M796 522L815 541L849 499" />
        <path d="M262 469C305 425 353 411 409 421M262 469L292 469M262 469L265 439" stroke-dasharray="13 18" />
      </g>`;
    case "content-distribution":
      return `<g>
        <path d="M210 197H595L655 257V591H210Z" fill="${surface}" opacity="0.78" />
        <path d="M595 197V257H655M284 324H564M284 383H532M284 442H573M284 501H468" opacity="0.58" />
        <path d="M656 394C740 394 756 280 826 280M656 394H826M656 394C740 394 756 508 826 508" stroke-dasharray="14 17" />
        <circle cx="886" cy="280" r="60" fill="${accent}" opacity="0.42" /><circle cx="886" cy="394" r="60" fill="${surface}" opacity="0.62" /><circle cx="886" cy="508" r="60" fill="${accent}" opacity="0.24" />
        <path d="M858 280H914M886 252V308M854 394L877 417L920 369M861 486L912 530M912 486L861 530" />
      </g>`;
    case "video-account":
      return `<g>
        <circle cx="334" cy="330" r="118" fill="${surface}" opacity="0.75" />
        <circle cx="334" cy="292" r="45" fill="${accent}" opacity="0.4" />
        <path d="M241 414C258 350 410 350 427 414" />
        <path d="M483 256H974V566H483Z" fill="${surface}" opacity="0.62" />
        <path d="M728 256V566M483 411H974" opacity="0.24" />
        <path d="M586 321L586 379L635 350Z" /><path d="M829 321L829 379L878 350Z" /><path d="M586 477L586 535L635 506Z" /><path d="M829 477L829 535L878 506Z" />
        <path d="M240 501H421M240 535H376" opacity="0.45" />
      </g>`;
    case "automation-launch":
      return `<g>
        <path d="M591 186C689 253 722 355 661 471L599 549L534 478C467 363 495 259 591 186Z" fill="${surface}" opacity="0.78" />
        <circle cx="592" cy="341" r="57" fill="${accent}" opacity="0.42" />
        <path d="M532 475L455 528L481 421M658 469L735 523L711 416M552 556L529 627M590 559V646M628 555L652 627" />
        <circle cx="256" cy="327" r="60" fill="${surface}" opacity="0.6" /><circle cx="914" cy="327" r="60" fill="${surface}" opacity="0.6" />
        <path d="M286 327H475M710 327H884" stroke-dasharray="13 18" /><path d="M229 327L252 350L286 307M887 327L910 350L944 307" />
      </g>`;
    case "creator-partnership":
      return `<g>
        <circle cx="318" cy="315" r="103" fill="${surface}" opacity="0.7" /><circle cx="882" cy="315" r="103" fill="${surface}" opacity="0.7" />
        <circle cx="318" cy="282" r="36" fill="${accent}" opacity="0.38" /><circle cx="882" cy="282" r="36" fill="${accent}" opacity="0.24" />
        <path d="M249 383C264 333 371 333 387 383M813 383C829 333 936 333 951 383" />
        <path d="M421 394L535 337L602 375L668 337L779 394L650 532L587 486L535 532Z" fill="${surface}" opacity="0.78" />
        <path d="M535 337L584 420L622 398L650 532M535 532L584 477L622 503" />
        <path d="M272 497H400M800 497H928" opacity="0.42" />
      </g>`;
    case "productized-service":
      return `<g>
        <path d="M230 283L460 181L689 283L460 394Z" fill="${accent}" opacity="0.35" />
        <path d="M230 283V500L460 613V394M689 283V500L460 613" fill="${surface}" opacity="0.68" />
        <path d="M342 232L574 339M574 230L342 339" opacity="0.32" />
        <path d="M737 284H954V508H737Z" fill="${surface}" opacity="0.72" />
        <circle cx="800" cy="349" r="31" fill="${accent}" opacity="0.4" /><path d="M856 337H913M856 370H894M780 449L807 474L857 418" />
        <path d="M690 397H737" stroke-dasharray="10 15" />
      </g>`;
    case "digital-store":
      return `<g>
        <path d="M228 286H853V603H228Z" fill="${surface}" opacity="0.72" />
        <path d="M189 286L248 172H835L894 286Z" fill="${accent}" opacity="0.38" />
        <path d="M248 172V286M377 172V286M506 172V286M635 172V286M764 172V286" opacity="0.34" />
        <path d="M290 379H470V603M555 371H790V520H555Z" />
        <path d="M603 416H742M603 459H702" opacity="0.45" />
        <path d="M898 384H1003L983 518H921Z" fill="${surface}" opacity="0.68" /><path d="M923 384C924 318 978 318 979 384M934 452H970" />
      </g>`;
    case "creator-fundraising":
      return `<g>
        <path d="M205 199H725V590H205Z" fill="${surface}" opacity="0.72" />
        <path d="M263 506V419M354 506V345M445 506V389M536 506V280M627 506V230" />
        <path d="M263 380L354 306L445 348L536 239L627 191" stroke-width="11" />
        <circle cx="263" cy="380" r="13" fill="${accent}" /><circle cx="354" cy="306" r="13" fill="${accent}" /><circle cx="445" cy="348" r="13" fill="${accent}" /><circle cx="536" cy="239" r="13" fill="${accent}" /><circle cx="627" cy="191" r="13" fill="${accent}" />
        <circle cx="882" cy="292" r="76" fill="${surface}" opacity="0.68" /><circle cx="882" cy="510" r="76" fill="${surface}" opacity="0.68" />
        <path d="M725 321L812 302M725 475L812 500M853 292H911M853 510L876 533L916 485" stroke-dasharray="12 15" />
      </g>`;
    default:
      return `<g>
        <circle cx="600" cy="393" r="166" fill="${surface}" opacity="0.62" />
        <circle cx="600" cy="393" r="74" fill="${accent}" opacity="0.42" />
        <circle cx="302" cy="287" r="76" fill="${surface}" opacity="0.65" /><circle cx="898" cy="287" r="76" fill="${surface}" opacity="0.65" /><circle cx="302" cy="526" r="76" fill="${surface}" opacity="0.65" /><circle cx="898" cy="526" r="76" fill="${surface}" opacity="0.65" />
        <path d="M365 324L451 358M749 358L835 324M365 489L451 451M749 451L835 489" stroke-dasharray="13 18" />
        <path d="M570 393H630M600 363V423M274 287H330M870 287H926M274 526H330M870 526H926" />
      </g>`;
  }
}

function buildSvg(
  template: BlueprintCoverTemplate & { scene?: BlueprintCoverScene },
  theme: BlueprintCoverTheme,
): string {
  const colors = PALETTES[theme];
  const accents = accentOrder(template);
  const paths = BLOB_PATHS[template.variant] ?? BLOB_PATHS[0];
  const scene = template.scene ?? resolveCoverScene("", template.motif);
  const label = sceneLabel(scene);
  const surface = theme === "dark" ? "#141414" : "#ffffff";
  const gridLine = theme === "dark" ? "#ffffff" : "#1c1917";
  const artRotation = [-1, 1.5, 0][template.variant] ?? 0;

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 800" width="1200" height="800">
    <defs>
      <linearGradient id="cover-gradient" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="${colors[accents[0]]}" />
        <stop offset="0.58" stop-color="${colors[accents[1]]}" />
        <stop offset="1" stop-color="${colors[accents[2]]}" />
      </linearGradient>
      <radialGradient id="spotlight" cx="0.76" cy="0.2" r="0.88">
        <stop offset="0" stop-color="${surface}" stop-opacity="0.76" />
        <stop offset="1" stop-color="${surface}" stop-opacity="0" />
      </radialGradient>
      <pattern id="grid" width="42" height="42" patternUnits="userSpaceOnUse">
        <path d="M42 0H0V42" fill="none" stroke="${gridLine}" stroke-width="1" opacity="0.12" />
      </pattern>
      <filter id="panel-shadow" x="-20%" y="-20%" width="140%" height="150%">
        <feDropShadow dx="0" dy="16" stdDeviation="22" flood-color="#000000" flood-opacity="0.1" />
      </filter>
    </defs>
    <rect width="1200" height="800" fill="${colors.background}" />
    <rect width="1200" height="800" fill="url(#cover-gradient)" opacity="0.82" />
    <rect width="1200" height="800" fill="url(#spotlight)" />
    <rect width="1200" height="800" fill="url(#grid)" opacity="0.32" />
    <g opacity="0.2">
      <path d="${paths[template.variant]}" fill="${surface}" transform="translate(-210 -140) scale(1.36)" />
      <path d="${paths[(template.variant + 1) % paths.length]}" fill="${colors[accents[3]]}" transform="translate(520 320) scale(0.78)" />
    </g>
    <g transform="rotate(${artRotation} 600 400)" filter="url(#panel-shadow)" fill="none" stroke="${colors.line}" stroke-width="9" stroke-linecap="round" stroke-linejoin="round">
      ${sceneArtwork(scene, surface, colors[accents[3]])}
    </g>
    <g font-family="Inter, Arial, sans-serif" fill="${colors.line}">
      <text x="58" y="101" font-size="21" font-weight="750" letter-spacing="4">${label}</text>
      <text x="58" y="708" font-size="16" font-weight="700" letter-spacing="5" opacity="0.62">WORKSPACE BLUEPRINT · CONTENT-DRIVEN COVER</text>
      <path d="M946 700H1100" fill="none" stroke="${colors.line}" stroke-width="3" opacity="0.26" />
      <circle cx="1122" cy="700" r="7" /><circle cx="1148" cy="700" r="7" opacity="0.5" />
    </g>
  </svg>`;
}

/** True only for the superseded raster fallback path, never user uploads. */
export function isLegacyGeneratedBlueprintCover(url: string | null | undefined): boolean {
  return Boolean(url?.startsWith(LEGACY_GENERATED_COVER_PREFIX));
}

/** Generated SVGs may appear in older showcase rows; never treat them as uploads. */
export function isAutomaticBlueprintCover(url: string | null | undefined): boolean {
  return isLegacyGeneratedBlueprintCover(url)
    || Boolean(url?.startsWith("data:image/svg+xml"));
}

/** Prefer the curated oil-painting series when a semantic scene has one. */
export function blueprintOilPaintingCoverUrl(
  template: BlueprintCoverTemplate & { scene?: BlueprintCoverScene },
): string | null {
  const scene = template.scene ?? resolveCoverScene("", template.motif);
  const filename = OIL_PAINTING_COVERS[scene];
  return filename
    ? `${OIL_PAINTING_COVER_BASE}${filename}?v=${OIL_PAINTING_COVER_VERSION}`
    : null;
}

/** Render a stable, theme-specific SVG data URL from the backend cover spec. */
export function blueprintCoverDataUrl(
  template: BlueprintCoverTemplate & { scene?: BlueprintCoverScene },
  theme: BlueprintCoverTheme,
): string {
  const key = `${template.scene ?? "auto"}:${template.motif}:${template.palette}:${template.variant}:${template.seed}:${theme}`;
  const cached = coverCache.get(key);
  if (cached) return cached;

  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(buildSvg(template, theme))}`;
  coverCache.set(key, url);
  return url;
}
