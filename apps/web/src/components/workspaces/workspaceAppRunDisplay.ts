import { t } from "../../lib/i18n";
import {
  IconAcademicCap, IconAudioWave, IconBolt, IconBookmark, IconBox, IconCalendar, IconChatBubble, IconChecklist, IconClipboard, IconClock, IconCode, IconComment, IconDocument, IconDollar, IconFlow, IconGlobe, IconHeart, IconImage, IconMail, IconMegaphone,
  IconMicrophone, IconMusicNote, IconPalette, IconPlay, IconPresentation, IconRefresh, IconReport, IconSearch, IconSend, IconShoppingCart,
  IconRocket, IconScale, IconSparkles, IconStar, IconTrendingUp, IconUsers,
} from "../icons";
import type { WorkspaceAppEntryIcon } from "../../lib/api";
import { workflowRunStatusPresentation, type WorkflowRunView } from "../workflows/workflowRunDisplay";

/** Two hues drawn from a title, so a record sleeve or a campaign cover without a picture looks like itself. */
export function titleHues(title: string): [number, number] {
  let hash = 0;
  for (const char of title) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return [hash % 360, (hash % 360 + 40 + (hash >> 9) % 80) % 360];
}

/** The short run status the Workspace App shows on a flow page and in its outputs. */
export function runStatusLabel(status: string, businessOutcome?: string | null): string {
  return t(workflowRunStatusPresentation({ status: status as WorkflowRunView["status"], businessOutcome: businessOutcome || undefined }).stateKey);
}

const METRIC_PHASE_TONES: Record<string, "active" | "failed" | "attention"> = {
  assessing: "active",
  assessment_error: "failed", configuration_error: "failed", collection_error: "failed",
  stale: "attention", insufficient_evidence: "attention",
};

/** The status-dot tone for a tracked number's freshness or assessment phase. */
export function metricPhaseTone(phase: string): "active" | "failed" | "attention" | "idle" {
  return METRIC_PHASE_TONES[phase] || "idle";
}

export function fileVisual(name: string) {
  const extension = name.match(/\.([^.]+)$/)?.[1]?.toLowerCase() || "";
  let Icon = IconDocument;
  if (/^(png|jpe?g|gif|webp|svg|heic)$/.test(extension)) Icon = IconImage;
  else if (/^(mp3|wav|flac|m4a|ogg|aac|aiff)$/.test(extension)) Icon = IconAudioWave;
  else if (/^(mp4|mov|webm|m4v|avi|mkv)$/.test(extension)) Icon = IconPlay;
  else if (/^(json|csv|tsv|xml|yaml|yml)$/.test(extension)) Icon = IconCode;
  return { extension: extension.toUpperCase(), Icon };
}

// First match wins, so more specific words come before general ones.
const ACTION_ICONS: Array<[RegExp, typeof IconFlow]> = [
  [/repl|comment|回复|评论/i, IconComment],
  [/publish|dispatch|upload|post to|发布|上传|分发/i, IconSend],
  [/web|url|site|网页|网址|网站/i, IconGlobe],
  [/video|clip|reel|footage|shorts?\b|视频|短片|实拍|剪辑/i, IconPlay],
  [/illustrat|carousel|art\b|插画|漫画|国风/i, IconPalette],
  [/image|photo|cover|poster|图|封面|海报/i, IconImage],
  [/social|promo|commerce|advert|announce|广告|社交|帖子|宣传|商业|投流|公告/i, IconMegaphone],
  [/how-?to|steps|步骤|操作指南/i, IconChecklist],
  [/myth|fact|versus|\bvs\b|误区|对比/i, IconScale],
  [/story|narrative|故事/i, IconBookmark],
  [/\btips?\b|技巧|小贴士/i, IconBolt],
  [/motion|animation|动效|动画/i, IconSparkles],
  [/podcast|audio|voice|播客|音频|配音/i, IconAudioWave],
  [/music|song|音乐|歌/i, IconMusicNote],
  [/pitch|investor|fundrais|融资|路演/i, IconRocket],
  [/training|course|lesson|培训|课程|课件/i, IconAcademicCap],
  [/event|workshop|meetup|活动|工作坊|讲座/i, IconCalendar],
  [/proposal|提案|方案/i, IconClipboard],
  [/product|service intro|产品介绍|服务介绍/i, IconBox],
  [/slide|deck|presentation|ppt|explain|tutorial|演示|幻灯|讲解|叙事|教程/i, IconPresentation],
  [/article|blog|文章|撰写|稿/i, IconDocument],
  [/topic|idea|research|find|search|选题|调研|研究|找/i, IconSearch],
  [/write|draft|copy|写/i, IconDocument],
  [/report|review|summary|weekly|news|roundup|guide|handbook|报告|周报|日报|复盘|总结|手册|新闻/i, IconReport],
  [/metric|track|assess|analytics|数据|指标|评估|追踪/i, IconTrendingUp],
  [/email|mail|邮件/i, IconMail],
  [/order|shop|store|refund|订单|店铺|退款/i, IconShoppingCart],
  [/sync|inventory|同步|库存/i, IconRefresh],
];

/** The glyph each icon name a page config may choose maps to. */
export const ENTRY_ICONS: Record<WorkspaceAppEntryIcon, typeof IconFlow> = {
  flow: IconFlow, chat: IconChatBubble, clock: IconClock, play: IconPlay, music: IconMusicNote, audio: IconAudioWave,
  microphone: IconMicrophone, image: IconImage, palette: IconPalette, sparkles: IconSparkles, document: IconDocument,
  report: IconReport, presentation: IconPresentation, checklist: IconChecklist, clipboard: IconClipboard, search: IconSearch,
  megaphone: IconMegaphone, send: IconSend, comment: IconComment, mail: IconMail, globe: IconGlobe, calendar: IconCalendar,
  trending: IconTrendingUp, rocket: IconRocket, academic: IconAcademicCap, box: IconBox, cart: IconShoppingCart,
  refresh: IconRefresh, bolt: IconBolt, bookmark: IconBookmark, scale: IconScale, code: IconCode, users: IconUsers,
  dollar: IconDollar, heart: IconHeart, star: IconStar,
};

/** An App entry's icon: the one its page config chose, else what its name says it does, else its kind's. */
export function actionIcon(action: { kind: string; title: string; source_title?: string; icon?: WorkspaceAppEntryIcon | null }): typeof IconFlow {
  if (action.icon && ENTRY_ICONS[action.icon]) return ENTRY_ICONS[action.icon];
  const text = `${action.title} ${action.source_title || ""}`;
  return ACTION_ICONS.find(([pattern]) => pattern.test(text))?.[1] ?? (action.kind === "chat" ? IconChatBubble : IconFlow);
}
