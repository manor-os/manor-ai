import type {
  ChatScrollRailMarker,
  ChatScrollRailMarkerTone,
} from "./ChatScrollRail";

export type ChatScrollRailTurnSourceRole =
  | "user"
  | "assistant"
  | "action"
  | "artifact"
  | "system";

export interface ChatScrollRailTurnSource {
  id: string;
  role: ChatScrollRailTurnSourceRole;
  tone?: ChatScrollRailMarkerTone;
  title?: string;
  excerpt?: string;
  text?: string;
  fileKind?: string;
  fileLabel?: string;
  sourceIndex: number;
}

const TITLE_LIMIT = 58;
const EXCERPT_LIMIT = 150;

function compactTurnText(value: unknown) {
  const raw =
    typeof value === "string"
      ? value
      : value == null
        ? ""
        : JSON.stringify(value);
  return raw
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[#>*_~]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function firstCompactText(...values: unknown[]) {
  for (const value of values) {
    const compact = compactTurnText(value);
    if (compact) return compact;
  }
  return "";
}

function clampText(value: string, max: number) {
  return value.length > max ? `${value.slice(0, max).trim()}...` : value;
}

function markerForStandaloneSource(
  source: ChatScrollRailTurnSource,
): ChatScrollRailMarker {
  const title = firstCompactText(source.title, source.text) || "Manor AI";
  const excerptCandidate = firstCompactText(source.excerpt);
  const excerpt =
    excerptCandidate && excerptCandidate !== title
      ? excerptCandidate
      : firstCompactText(source.text) !== title
        ? firstCompactText(source.text)
        : "";
  return {
    id: source.id,
    sourceIndex: source.sourceIndex,
    tone: source.tone || source.role,
    title: clampText(title, TITLE_LIMIT),
    excerpt: clampText(excerpt, EXCERPT_LIMIT),
    fileKind: source.fileKind,
    fileLabel: source.fileLabel,
  };
}

export function buildChatScrollRailTurnMarkers(
  sources: ChatScrollRailTurnSource[],
): ChatScrollRailMarker[] {
  const markers: ChatScrollRailMarker[] = [];
  let currentTurn: {
    id: string;
    sourceIndex: number;
    title: string;
    userText: string;
    answerParts: string[];
    fileKind?: string;
    fileLabel?: string;
  } | null = null;

  const flushCurrentTurn = () => {
    if (!currentTurn) return;
    const answerText = currentTurn.answerParts.join(" · ").trim();
    const excerptSource = answerText || currentTurn.userText;
    markers.push({
      id: currentTurn.id,
      sourceIndex: currentTurn.sourceIndex,
      tone: "user",
      title: clampText(currentTurn.title, TITLE_LIMIT),
      excerpt: clampText(excerptSource, EXCERPT_LIMIT),
      fileKind: currentTurn.fileKind,
      fileLabel: currentTurn.fileLabel,
    });
    currentTurn = null;
  };

  for (const source of sources) {
    if (source.role === "user") {
      flushCurrentTurn();
      currentTurn = {
        id: source.id,
        sourceIndex: source.sourceIndex,
        title: firstCompactText(source.title, source.text) || "You",
        userText: firstCompactText(source.text, source.excerpt, source.title),
        answerParts: [],
        fileKind: source.fileKind,
        fileLabel: source.fileLabel,
      };
      continue;
    }

    if (!currentTurn) {
      markers.push(markerForStandaloneSource(source));
      continue;
    }

    const answerText = firstCompactText(source.text, source.excerpt, source.title);
    if (answerText) {
      currentTurn.answerParts.push(answerText);
    }
    if (!currentTurn.fileLabel && source.fileLabel) {
      currentTurn.fileLabel = source.fileLabel;
    }
    if (!currentTurn.fileKind && source.fileKind) {
      currentTurn.fileKind = source.fileKind;
    }
  }

  flushCurrentTurn();
  return markers;
}
