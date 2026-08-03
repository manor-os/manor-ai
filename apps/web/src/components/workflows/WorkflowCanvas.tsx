import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Panel,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  addEdge,
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type Node,
  type Edge,
  type Connection,
  type NodeProps,
  type EdgeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import MediaPreview from "./MediaPreview";
import { resolveConnectorBrand } from "../../lib/workflowBrand";
import type { MediaRef } from "../../lib/workflowMedia";

/* ------------------------------------------------------------------ */
/*  Node-graph editor built on React Flow — the React sibling of the   */
/*  Vue Flow library n8n's editor uses. Typed input/output handles,    */
/*  drag-to-connect, draggable nodes with persisted positions, bezier  */
/*  edges (true/false branches coloured), pan/zoom, minimap, controls. */
/* ------------------------------------------------------------------ */

export interface CanvasStep {
  id: string;
  type: string;
  name?: string;
  next?: string[];
  true_next?: string[];
  false_next?: string[];
  config?: { cases?: { expression?: string; next?: string[] }[]; default_next?: string[]; [k: string]: any };
  position?: { x: number; y: number };
  meta?: { original_type?: string };
}

const NODE_W = 210;
const NODE_H = 60;
const GAP_X = 86;
const GAP_Y = 34;

// Each node belongs to a category; categories carry a distinct, *muted* accent
// (desaturated, on-brand) so node types are visually distinguishable at a
// glance — applied to the icon tile + type label, leaving the card neutral.
// Type is signalled primarily by the icon + label, with a small family of
// n8n-style card variants instead of a different flowchart polygon per type.
// Colour stays calm: a warm-stone neutral for working node types, teal only
// for entry points, gold for notes. (Connectors still show their brand colour;
// run state and typed sockets keep their own semantic hues.)
const CAT_COLOR = {
  entry: "#0f766e", // teal  — triggers / entry points
  ai: "#6f6861",    // neutral warm-stone ink
  logic: "#6f6861",
  data: "#6f6861",
  io: "#6f6861",
  media: "#6f6861",
  note: "#c79a3a",  // gold  — sticky notes
  term: "#9b938c",  // faint — terminal / unsupported markers
} as const;

const NODE_CATEGORY: Record<string, keyof typeof CAT_COLOR> = {
  trigger: "entry", webhook: "entry",
  llm: "ai", agent: "ai", rag: "ai", classifier: "ai", extract: "ai",
  condition: "logic", switch: "logic", loop: "logic", parallel: "logic", merge: "logic", wait: "logic", stage: "logic",
  transform: "data", split: "data", sort: "data", aggregate: "data", filter: "data",
  dedupe: "data", limit: "data", datetime: "data", extractfromfile: "data", code: "data",
  http: "io", connector: "io", respond: "io", notify: "io", subworkflow: "io", tool: "io",
  image: "media", video: "media", audio: "media", media: "media",
  end: "term", stop: "term", unsupported: "term",
  note: "note",
};

const TYPE_LABEL: Record<string, string> = {
  trigger: "TRIGGER", webhook: "WEBHOOK", llm: "LLM", rag: "KNOWLEDGE", agent: "AGENT",
  tool: "TOOL", connector: "CONNECTOR", code: "CODE", http: "HTTP", condition: "IF",
  switch: "SWITCH", loop: "LOOP", parallel: "PARALLEL", merge: "MERGE", subworkflow: "WORKFLOW",
  extract: "EXTRACT", filter: "FILTER", aggregate: "AGGREGATE", datetime: "DATE", split: "SPLIT",
  limit: "LIMIT", respond: "RESPOND", sort: "SORT", dedupe: "DEDUPE", stop: "STOP",
  extractfromfile: "EXTRACT FILE", transform: "SET", classifier: "CLASSIFY", wait: "WAIT",
  notify: "NOTIFY", end: "END", media: "MEDIA", image: "IMAGE", video: "VIDEO", audio: "AUDIO",
  unsupported: "UNSUPPORTED", note: "NOTE", stage: "STAGE",
};

// canonical node type -> accent colour (from its category) + short label
export const TYPE_META: Record<string, { color: string; label: string }> = Object.fromEntries(
  Object.entries(TYPE_LABEL).map(([t, label]) => [t, { color: CAT_COLOR[NODE_CATEGORY[t] || "data"], label }]),
);

function typeMeta(t: string) {
  return TYPE_META[t] || { color: "#9b938c", label: t.toUpperCase() };
}

// one outline glyph per node category (n8n-style icon tile). Every canonical
// node type resolves to a distinct, semantically-fitting glyph via iconCategory.
const ICON_PATHS: Record<string, string> = {
  trigger: "M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z",
  webhook: "M9 8.25H7.5a2.25 2.25 0 00-2.25 2.25v9a2.25 2.25 0 002.25 2.25h9a2.25 2.25 0 002.25-2.25v-9a2.25 2.25 0 00-2.25-2.25H15m0-3l-3-3m0 0l-3 3m3-3v12",
  ai: "M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z",
  agent: "M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23-.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5",
  knowledge: "M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25",
  classifier: "M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 01-.659 1.591l-5.432 5.432a2.25 2.25 0 00-.659 1.591v2.927a2.25 2.25 0 01-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 00-.659-1.591L3.659 7.409A2.25 2.25 0 013 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0112 3z",
  tool: "M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26",
  connector: "M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244",
  code: "M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5",
  http: "M12 21a9 9 0 100-18 9 9 0 000 18zm0 0c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m-9 9h18",
  branch: "M6 3v6m0 0a3 3 0 103 3m-3-3a3 3 0 113 3m6-9a3 3 0 11-3 3m0 0v6m0 0a3 3 0 103 3",
  switch: "M7.5 21L3 16.5m0 0L7.5 12M3 16.5h13.5a3 3 0 003-3v-1.5m-3-9L21 7.5m0 0L16.5 12M21 7.5H7.5a3 3 0 00-3 3V12",
  loop: "M16.023 9.348h4.992V4.356M2.985 19.644v-4.992h4.992m-7.491-3.66a8.25 8.25 0 0114.13-2.46l3.36 3.348m-17.49 1.572l3.36 3.348a8.25 8.25 0 0014.13-2.46",
  parallel: "M3.75 6h16.5M3.75 12h16.5m-16.5 6h16.5",
  merge: "M7.5 21L3 16.5m0 0L7.5 12M3 16.5h12a3 3 0 003-3V3",
  transform: "M3 7.5L7.5 3m0 0L12 7.5M7.5 3v13.5m13.5 0L16.5 21m0 0L12 16.5m4.5 4.5V7.5",
  wait: "M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z",
  stage: "M4.5 5.25h15m-15 6.75h15m-15 6.75h15M7.5 3v4.5m9-4.5v4.5m-9 2.25v4.5m9-4.5v4.5m-9 2.25V21m9-4.5V21",
  notify: "M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0",
  end: "M5.25 7.5A2.25 2.25 0 017.5 5.25h9a2.25 2.25 0 012.25 2.25v9a2.25 2.25 0 01-2.25 2.25h-9a2.25 2.25 0 01-2.25-2.25v-9z",
  media: "M3.375 19.5h17.25m-17.25 0a1.125 1.125 0 01-1.125-1.125M3.375 19.5h7.5c.621 0 1.125-.504 1.125-1.125m-9.75 0V5.625m0 12.75v-1.5c0-.621.504-1.125 1.125-1.125m18.375 2.625V5.625m0 12.75c0 .621-.504 1.125-1.125 1.125m1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125m0 3.75h-7.5A1.125 1.125 0 0112 18.375m9.75-12.75c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125m19.5 0v1.5c0 .621-.504 1.125-1.125 1.125M2.25 5.625v1.5c0 .621.504 1.125 1.125 1.125m0 0h17.25m-17.25 0h7.5c.621 0 1.125.504 1.125 1.125M3.375 8.25c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125m17.25-3.75h-7.5c-.621 0-1.125.504-1.125 1.125m8.625-1.125c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125m-17.25 0h7.5m-7.5 0c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125M12 10.875v-1.5m0 1.5c0 .621-.504 1.125-1.125 1.125M12 10.875c0 .621.504 1.125 1.125 1.125m-2.25 0c.621 0 1.125.504 1.125 1.125M13.125 12h7.5m-7.5 0c-.621 0-1.125.504-1.125 1.125M20.625 12c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125m-17.25 0h7.5M12 14.625v-1.5m0 1.5c0 .621-.504 1.125-1.125 1.125M12 14.625c0 .621.504 1.125 1.125 1.125m-2.25 0c.621 0 1.125.504 1.125 1.125m0 1.5v-1.5m0 0c0-.621.504-1.125 1.125-1.125m0 0h7.5",
  image: "M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909m-18 3.75h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5H3.75A1.5 1.5 0 002.25 6v12a1.5 1.5 0 001.5 1.5zm10.5-11.25h.008v.008h-.008V8.25z",
  video: "M15.75 10.5l4.72-4.72a.75.75 0 011.28.53v11.38a.75.75 0 01-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 002.25-2.25v-9a2.25 2.25 0 00-2.25-2.25h-9A2.25 2.25 0 002.25 7.5v9a2.25 2.25 0 002.25 2.25z",
  audio: "M9 9l10.5-3m0 6.553v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 11-.99-3.467l2.31-.66a2.25 2.25 0 001.632-2.163zm0 0V2.25L9 5.25v10.303m0 0v3.75a2.25 2.25 0 01-1.632 2.163l-1.32.377a1.803 1.803 0 01-.99-3.467l2.31-.66A2.25 2.25 0 009 15.553z",
  node: "M6 6.878V6a2.25 2.25 0 012.25-2.25h7.5A2.25 2.25 0 0118 6v.878m-12 0c.235-.083.487-.128.75-.128h10.5c.263 0 .515.045.75.128m-12 0A2.25 2.25 0 004.5 9v.878m13.5-3A2.25 2.25 0 0119.5 9v.878m0 0a2.246 2.246 0 00-.75-.128H5.25c-.263 0-.515.045-.75.128m15 0A2.25 2.25 0 0121 12v6a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 18v-6c0-.98.626-1.813 1.5-2.122",
  subworkflow: "M6.429 9.75L2.25 12l4.179 2.25m0-4.5l5.571 3 5.571-3m-11.142 0L2.25 7.5 12 2.25l9.75 5.25-4.179 2.25m0 0L21.75 12l-4.179 2.25m0 0l4.179 2.25L12 21.75 2.25 16.5l4.179-2.25m11.142 0l-5.571 3-5.571-3",
  extract: "M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z",
  filter: "M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0H12",
  aggregate: "M8.25 6.75h7.5M8.25 12h7.5m-7.5 5.25h7.5M3.75 6.75h.007v.008H3.75V6.75zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zM3.75 12h.007v.008H3.75V12zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm-.375 5.25h.007v.008H3.75v-.008zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z",
  datetime: "M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5",
  split: "M7.217 10.907a2.25 2.25 0 100 2.186m0-2.186c.18.324.283.696.283 1.093s-.103.77-.283 1.093m0-2.186l9.566-5.314m-9.566 7.5l9.566 5.314m0 0a2.25 2.25 0 103.935 2.186 2.25 2.25 0 00-3.935-2.186zm0-12.814a2.25 2.25 0 103.933-2.185 2.25 2.25 0 00-3.933 2.185z",
  limit: "M3.75 12h16.5M3.75 6.75h16.5M3.75 17.25h7.5",
  respond: "M9 15L3 9m0 0l6-6M3 9h12a6 6 0 010 12h-3",
  sort: "M3 7.5L7.5 3m0 0L12 7.5M7.5 3v13.5m6.75 3l3.75-3.75m0 0L21 16.5m-3-3.75V21",
  dedupe: "M16.5 8.25V6a2.25 2.25 0 00-2.25-2.25H6A2.25 2.25 0 003.75 6v8.25A2.25 2.25 0 006 16.5h2.25m4.5 0v3.75A2.25 2.25 0 0010.5 22.5h7.5A2.25 2.25 0 0020.25 20.25v-7.5A2.25 2.25 0 0018 10.5h-7.5A2.25 2.25 0 008.25 12.75v3.75",
  stop: "M9.75 9.75l4.5 4.5m0-4.5l-4.5 4.5M21 12a9 9 0 11-18 0 9 9 0 0118 0z",
  extractfromfile: "M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z",
  note: "M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10",
};

// Map each canonical node type to its glyph. Types not listed fall back to their
// own key if a path exists, else a generic "node" box — so adding a node type
// without a custom glyph still renders something sensible.
const ICON_CATEGORY: Record<string, string> = {
  trigger: "trigger",
  webhook: "webhook",
  llm: "ai",
  agent: "agent",
  classifier: "classifier",
  rag: "knowledge",
  tool: "tool",
  connector: "connector",
  condition: "branch",
  switch: "switch",
  image: "image",
  media: "media",
};

function iconCategory(t: string): string {
  if (ICON_CATEGORY[t]) return ICON_CATEGORY[t];
  if (ICON_PATHS[t]) return t;
  return "node";
}

export function NodeIcon({ type, size = 18 }: { type: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
      <path d={ICON_PATHS[iconCategory(type)]} />
    </svg>
  );
}

type NodeData = { type: string; label: string; branches: boolean; cases?: string[]; status?: string; preview?: MediaRef; issue?: "error" | "warning"; brand?: { color: string; path?: string }; agent?: { model?: string; tools?: string[]; memory?: boolean }; outType?: string; output?: string };

// ComfyUI-style typed sockets: the data type a node emits → a socket colour,
// also used to tint the data wire. ``any`` stays neutral so ordinary flows are
// calm; media / explicitly-typed outputs light up (blue image, rose video…).
const TYPE_COLOR: Record<string, string> = {
  any: "#b9b3ac", text: "#caa24a", string: "#caa24a", number: "#5fa37a",
  json: "#7f8fb0", object: "#7f8fb0", image: "#5b9bd5", video: "#b06f86", audio: "#c79a3a",
};
const typeColor = (t?: string) => TYPE_COLOR[t || "any"] || TYPE_COLOR.any;

function nodeOutputType(s: CanvasStep): string {
  if (s.type === "image" || s.type === "video" || s.type === "audio") return s.type;
  if (s.type === "media") return String(s.config?.kind || "image");
  const outs = s.config?.outputs as { type?: string }[] | undefined;
  if (Array.isArray(outs)) {
    const t = outs.map((o) => o?.type).find((x) => x && x !== "any");
    if (t) return t;
  }
  return "any";
}
const HANDLE_BASE: React.CSSProperties = { width: 12, height: 12, border: "none", background: "var(--surface-panel)" };

function activateHandleFromKeyboard(event: React.KeyboardEvent<HTMLDivElement>) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  event.currentTarget.click();
}

// Per-node run status → colour (matches the runner's result statuses).
const STATUS_COLOR: Record<string, string> = {
  completed: "#4f9c84",
  failed: "#d65f59",
  paused: "#cf9b44",
  running: "#0f766e",
  skipped: "#a8a29e",
};

const EDGE_FLOW = "#0f766e"; // teal — the run is moving across this edge right now
const EDGE_DONE = "#4f9c84"; // green — this edge's path has already been traversed

/** How a connection should look given the run-status of its endpoints, so the
 *  active path lights up as a run streams through: the leading edge animates
 *  (flowing), edges behind it stay green (done), the rest rest at their base. */
function edgeLook(srcStatus: string | undefined, tgtStatus: string | undefined, base: string) {
  const done = srcStatus === "completed" || srcStatus === "skipped";
  if (done && tgtStatus === "running") return { animated: true, stroke: EDGE_FLOW, strokeWidth: 2.4 };
  if (done && (tgtStatus === "completed" || tgtStatus === "skipped"))
    return { animated: false, stroke: EDGE_DONE, strokeWidth: 2 };
  if (srcStatus === "running") return { animated: true, stroke: EDGE_FLOW, strokeWidth: 2.4 };
  return { animated: false, stroke: base, strokeWidth: 1.6 };
}

// lets a node trigger "add a node connected from me" (n8n's output + affordance)
const AddFromContext = createContext<((sourceId: string) => void) | null>(null);
// lets a sticky note persist its edited text back to the steps array
const NoteEditContext = createContext<((id: string, text: string) => void) | null>(null);
// lets an edge delete itself (hover ✕) — so connections are removable without
// knowing the select-then-Backspace gesture
const EdgeDeleteContext = createContext<((edgeId: string) => void) | null>(null);
// per-node quick actions (test this node / delete it) shown on hover, so you
// don't have to open the detail panel
const NodeActionsContext = createContext<{ run?: (id: string) => void; remove?: (id: string) => void } | null>(null);

const TOOLBTN: React.CSSProperties = {
  width: 22, height: 22, borderRadius: 7, border: "none", cursor: "pointer", padding: 0,
  background: "var(--surface-panel)", boxShadow: "0 1px 3px rgba(28,25,23,0.16), 0 0 0 1px rgba(28,25,23,0.06)",
  display: "inline-flex", alignItems: "center", justifyContent: "center",
};

/** Hover toolbar (Run ▶ / Delete 🗑) floating above a node — n8n-style quick
 *  actions that don't require opening the node. */
function NodeToolbar({ id, show }: { id: string; show: boolean }) {
  const actions = useContext(NodeActionsContext);
  const addFrom = useContext(AddFromContext);
  if ((!actions || (!actions.run && !actions.remove)) && !addFrom) return null;
  return (
    <div
      className="nodrag"
      style={{
        position: "absolute", top: -14, right: 6, display: "flex", gap: 4, zIndex: 6,
        opacity: show ? 1 : 0, pointerEvents: show ? "all" : "none", transition: "opacity .12s",
      }}
    >
      {actions?.run && (
        <button type="button" title="Test this node" style={{ ...TOOLBTN, color: "#0f766e" }}
          onClick={(e) => { e.stopPropagation(); actions.run?.(id); }}>
          <svg width={12} height={12} viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>
        </button>
      )}
      {addFrom && (
        <button type="button" title="Add a connected node" style={{ ...TOOLBTN, color: "var(--text-muted)" }}
          onClick={(e) => { e.stopPropagation(); addFrom(id); }}>
          <span aria-hidden style={{ fontSize: 15, lineHeight: 1, fontWeight: 600 }}>+</span>
        </button>
      )}
      {actions?.remove && (
        <button type="button" title="Delete node" style={{ ...TOOLBTN, color: "#d65f59" }}
          onClick={(e) => { e.stopPropagation(); actions.remove?.(id); }}>
          <svg width={12} height={12} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round"><path d="M3 6h18M8 6V4h8v2m-9 0v14a1 1 0 001 1h8a1 1 0 001-1V6" /></svg>
        </button>
      )}
    </div>
  );
}

/** A curved connection with a hover ✕ to delete it, plus its branch label. */
function DeletableEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style, markerEnd, label, labelStyle, selected }: EdgeProps) {
  const onDelete = useContext(EdgeDeleteContext);
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  return (
    <>
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      <EdgeLabelRenderer>
        <div
          className="nodrag nopan"
          style={{ position: "absolute", transform: `translate(-50%,-50%) translate(${labelX}px,${labelY}px)`, pointerEvents: "all", display: "flex", alignItems: "center", gap: 4 }}
        >
          {label && <span style={labelStyle as React.CSSProperties}>{label}</span>}
          {onDelete && (
            <button
              className="nodrag"
              title="Delete connection"
              onClick={(e) => { e.stopPropagation(); onDelete(id); }}
              style={{
                width: 17, height: 17, borderRadius: "50%", border: "none", cursor: "pointer",
                background: selected ? "#d65f59" : "var(--surface-panel)", color: selected ? "#fff" : "#d65f59",
                boxShadow: `0 1px 3px rgba(28,25,23,0.18), 0 0 0 1px rgba(28,25,23,0.10)`,
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                fontSize: 11, lineHeight: 1, padding: 0, opacity: selected ? 1 : 0.8,
                transition: "opacity .12s, background .12s",
              }}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.opacity = "1"; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.opacity = selected ? "1" : "0.8"; }}
            >
              ✕
            </button>
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const edgeTypes = { deletable: DeletableEdge };

type NoteData = { text?: string; editable?: boolean };

/** A free-floating sticky note (n8n/ComfyUI-style annotation) — pale gold, no
 *  connection handles, not part of the run. Editable inline when the canvas is
 *  editable; read-only otherwise. */
function NoteNode({ id, data }: NodeProps<Node<NoteData>>) {
  const onEdit = useContext(NoteEditContext);
  const editable = !!onEdit && data.editable !== false;
  // Local text so typing stays smooth; persist to the steps array on blur
  // (avoids a save round-trip per keystroke).
  const [text, setText] = useState(data.text || "");
  useEffect(() => { setText(data.text || ""); }, [data.text]);
  return (
    <div
      style={{
        minWidth: 180, minHeight: 96, maxWidth: 320, padding: "10px 12px",
        background: "#fbf3d6", borderRadius: 10, color: "#6b5618",
        boxShadow: "0 1px 3px rgba(28,25,23,0.10), 0 6px 18px rgba(28,25,23,0.06)",
        display: "flex", flexDirection: "column", gap: 4,
      }}
    >
      <span style={{ fontSize: 9, fontWeight: 700, letterSpacing: 0.6, color: "#b08a2f", display: "flex", alignItems: "center", gap: 5 }}>
        <NodeIcon type="note" size={11} /> NOTE
      </span>
      {editable ? (
        <textarea
          className="nodrag"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => { if (text !== (data.text || "")) onEdit!(id, text); }}
          placeholder="Write a note…"
          style={{
            flex: 1, minHeight: 56, resize: "vertical", border: "none", outline: "none",
            background: "transparent", color: "#6b5618", fontSize: 12.5, lineHeight: 1.5,
            fontFamily: "var(--font-sans, inherit)", padding: 0,
          }}
        />
      ) : (
        <div style={{ fontSize: 12.5, lineHeight: 1.5, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
          {data.text || "—"}
        </div>
      )}
    </div>
  );
}

function PortLabel({ text, top }: { text: string; top: string }) {
  return (
    <span style={{ position: "absolute", right: 12, top, transform: "translateY(-50%)", fontSize: 9, color: "var(--text-faint)", pointerEvents: "none" }}>
      {text}
    </span>
  );
}

// Keep n8n's compact card/port language while giving the workflow roles that
// materially change control flow a recognisable silhouette. This makes a
// dense graph scannable without turning every integration into a new shape.
type NodeVariant =
  | "default"
  | "trigger"
  | "configuration"
  | "decision"
  | "merge"
  | "io"
  | "code"
  | "wait"
  | "terminal"
  | "agent";

function nodeVariant(t: string): NodeVariant {
  if (["trigger", "webhook"].includes(t)) return "trigger";
  if (t === "agent") return "agent";
  // These are the same AI-support roles n8n renders as configuration nodes
  // when they are attached to an agent (model, knowledge source, or tool).
  if (["llm", "rag", "tool"].includes(t)) return "configuration";
  if (["condition", "switch", "filter", "classifier"].includes(t)) return "decision";
  if (["merge", "aggregate"].includes(t)) return "merge";
  if (["http", "connector", "notify", "subworkflow", "media", "image", "video", "audio"].includes(t)) return "io";
  if (["code", "extractfromfile"].includes(t)) return "code";
  if (t === "wait") return "wait";
  if (["end", "stop", "respond", "unsupported"].includes(t)) return "terminal";
  return "default";
}

const NODE_RADIUS: Record<NodeVariant, string> = {
  default: "12px",
  trigger: "32px 12px 12px 32px",
  configuration: "999px",
  decision: "0",
  merge: "0",
  io: "0",
  code: "0",
  wait: "26px",
  terminal: "12px 32px 32px 12px",
  agent: "16px",
};

const NODE_SHAPE_PATH: Partial<Record<NodeVariant, string>> = {
  // Angled decision: the sloped sides read as a branch without consuming the
  // large vertical footprint of a traditional diamond.
  decision: "M18 1H192L209 50L192 99H18L1 50Z",
  // Two incoming slopes converge toward the output direction.
  merge: "M1 1H176L209 50L176 99H1L22 50Z",
  // A restrained parallelogram is the familiar data input/output silhouette.
  io: "M18 1H209L191 99H1Z",
  // Folded upper-right corner differentiates executable code/file parsing.
  code: "M13 1H188L209 22V87Q209 99 197 99H13Q1 99 1 87V13Q1 1 13 1Z",
};

type NodeInsets = { left: number; right: number; targetLeft: number; sourceRight: number };

const NODE_INSETS: Record<NodeVariant, NodeInsets> = {
  default: { left: 13, right: 13, targetLeft: 0, sourceRight: -18 },
  trigger: { left: 20, right: 13, targetLeft: 0, sourceRight: -18 },
  // The bottom of a pill narrows faster than its visual bounding box. The
  // larger inset prevents output/media footers from crossing its curve.
  configuration: { left: 30, right: 30, targetLeft: 0, sourceRight: -18 },
  decision: { left: 25, right: 25, targetLeft: 0, sourceRight: -18 },
  merge: { left: 29, right: 25, targetLeft: 14, sourceRight: -18 },
  io: { left: 24, right: 24, targetLeft: 8, sourceRight: -10 },
  code: { left: 15, right: 25, targetLeft: 0, sourceRight: -18 },
  wait: { left: 19, right: 19, targetLeft: 0, sourceRight: -18 },
  terminal: { left: 13, right: 20, targetLeft: 0, sourceRight: -18 },
  agent: { left: 15, right: 15, targetLeft: 0, sourceRight: -18 },
};

function NodeSurface({
  variant,
  statusColor,
  selected,
}: {
  variant: NodeVariant;
  statusColor?: string;
  selected?: boolean;
}) {
  const border = statusColor || "var(--glass-hairline)";
  const path = NODE_SHAPE_PATH[variant];
  if (path) {
    return (
      <svg
        aria-hidden="true"
        data-node-variant={variant}
        data-node-surface="polygon"
        viewBox="0 0 210 100"
        preserveAspectRatio="none"
        style={{
          position: "absolute", inset: 0, width: "100%", height: "100%", overflow: "visible", pointerEvents: "none",
          filter: selected
            ? "drop-shadow(0 0 5px rgba(15,118,110,0.24)) drop-shadow(0 4px 6px rgba(28,25,23,0.08))"
            : "drop-shadow(0 1px 1px rgba(28,25,23,0.10)) drop-shadow(0 4px 6px rgba(28,25,23,0.05))",
        }}
      >
        {selected && (
          <path d={path} fill="none" stroke="rgba(15,118,110,0.13)" strokeWidth={12} vectorEffect="non-scaling-stroke" />
        )}
        <path
          d={path}
          fill="var(--surface-panel)"
          stroke={border}
          strokeWidth={statusColor ? 2 : 1.5}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    );
  }
  return (
    <div
      aria-hidden="true"
      data-node-variant={variant}
      data-node-surface="rounded"
      style={{
        position: "absolute", inset: 0, pointerEvents: "none",
        border: `${statusColor ? 2 : 1.5}px solid ${border}`,
        borderRadius: NODE_RADIUS[variant],
        background: "var(--surface-panel)",
        boxShadow: selected
          ? "0 0 0 6px rgba(15,118,110,0.13), 0 1px 2px rgba(28,25,23,0.12), 0 5px 12px rgba(28,25,23,0.06)"
          : "0 1px 2px rgba(28,25,23,0.12), 0 5px 12px rgba(28,25,23,0.06)",
      }}
    />
  );
}

/** A folded agent sub-node, shown as a small pill on the agent card — echoes
 *  n8n's Chat Model / Memory / Tool ports (manor keeps them inline). */
function AgentPort({ icon, label, color }: { icon: string; label: string; color: string }) {
  return (
    <span
      title={label}
      style={{
        display: "inline-flex", alignItems: "center", gap: 4, maxWidth: 96,
        padding: "2px 7px", borderRadius: 999, background: `${color}14`, color,
      }}
    >
      <NodeIcon type={icon} size={10} />
      <span style={{ fontSize: 9, fontWeight: 600, color: "var(--text-muted)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
    </span>
  );
}

/** A single node card with typed input / output connection handles. */
function WorkflowNode({ id, data, selected }: NodeProps<Node<NodeData>>) {
  const m = typeMeta(data.type);
  const statusColor = data.status ? STATUS_COLOR[data.status] : undefined;
  const variant = nodeVariant(data.type);
  const insets = NODE_INSETS[variant];
  const tileColor = data.brand?.color || m.color; // brand hue for connectors, else category
  const padL = insets.left;
  const padR = insets.right;
  const [hovered, setHovered] = useState(false);
  return (
    <div
      style={{ position: "relative", width: NODE_W, minHeight: NODE_H }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <NodeSurface variant={variant} statusColor={statusColor} selected={selected} />
      <NodeToolbar id={id} show={hovered} />
      {statusColor && (
        <span
          title={data.status}
          style={{
            position: "absolute", top: -6, left: -6, width: 13, height: 13, borderRadius: "50%",
            background: statusColor, boxShadow: "0 0 0 2px var(--surface-panel)",
            animation: data.status === "running" ? "pulse 1.2s ease-in-out infinite" : "none",
          }}
        />
      )}
      {data.issue && (
        <span
          title={data.issue === "error" ? "Configuration error" : "Warning"}
          style={{
            position: "absolute", top: -7, right: -7, width: 16, height: 16, borderRadius: "50%",
            background: data.issue === "error" ? "#d65f59" : "#cf9b44", color: "#fff",
            boxShadow: "0 0 0 2px var(--surface-panel)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 11, fontWeight: 800, lineHeight: 1,
          }}
        >
          !
        </span>
      )}
      <Handle
        type="target"
        position={Position.Left}
        title={`Connect into ${data.label}`}
        aria-label={`Connect into ${data.label}`}
        role="button"
        tabIndex={0}
        onKeyDown={activateHandleFromKeyboard}
        style={{ ...HANDLE_BASE, left: insets.targetLeft, boxShadow: `0 0 0 2px ${m.color}` }}
      />
      <div style={{ position: "relative", zIndex: 1, display: "flex", alignItems: "center", gap: 10, padding: `11px ${padR}px 11px ${padL}px` }}>
        <div
          style={{
            width: 34, height: 34, borderRadius: 9, flexShrink: 0,
            background: `${tileColor}1a`, color: tileColor,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          {data.brand?.path ? (
            <svg width={19} height={19} viewBox="0 0 24 24" fill={tileColor} aria-hidden>
              <path d={data.brand.path} />
            </svg>
          ) : (
            <NodeIcon type={data.type} />
          )}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
          <span style={{ fontSize: 9, fontWeight: 700, letterSpacing: 0.5, color: tileColor }}>{m.label}</span>
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-strong)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {data.label}
          </span>
        </div>
      </div>
      {data.agent && (data.agent.model || data.agent.memory || (data.agent.tools?.length ?? 0) > 0) && (
        <div style={{ position: "relative", zIndex: 1, display: "flex", flexWrap: "wrap", gap: 4, padding: `0 ${padR}px 9px ${padL}px` }}>
          {data.agent.model && <AgentPort icon="ai" label={data.agent.model} color={m.color} />}
          {data.agent.memory && <AgentPort icon="wait" label="memory" color={m.color} />}
          {(data.agent.tools?.length ?? 0) > 0 && <AgentPort icon="tool" label={`${data.agent.tools!.length} tool${data.agent.tools!.length === 1 ? "" : "s"}`} color={m.color} />}
        </div>
      )}
      {data.preview && (
        <div className="nodrag" style={{ position: "relative", zIndex: 1, padding: `0 ${padR}px 10px ${padL}px` }}>
          <MediaPreview refItem={data.preview} compact />
        </div>
      )}
      {data.output != null && !data.preview && (
        // one-line preview of what this node produced on the last run — so the
        // result is visible on the canvas without opening the node.
        <div
          className="nodrag"
          data-node-output-footer
          style={{
            // Padding belongs to a real wrapper instead of the last child's
            // margin: a trailing vertical margin can collapse outside this
            // borderless React Flow node and leave the surface ending at the
            // result strip's baseline.
            position: "relative", zIndex: 1, boxSizing: "border-box",
            padding: `0 ${padR}px 12px ${padL}px`,
          }}
        >
          <div
            title={data.output}
            data-node-output-preview
            style={{
              minWidth: 0, maxWidth: NODE_W - padL - padR, boxSizing: "border-box",
              padding: "4px 7px", borderRadius: 7, background: "var(--surface-muted)",
              fontSize: 9.5, lineHeight: 1.4,
              color: data.status === "failed" ? "#b4534d" : "var(--text-muted)",
              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
              fontFamily: data.status === "skipped" ? "var(--font-sans, inherit)" : "var(--font-mono, monospace)",
            }}
          >
            <><span style={{ fontWeight: 700 }}>{data.status === "failed" ? "Error" : "Output"} · </span>{data.output || "(empty)"}</>
          </div>
        </div>
      )}
      {data.cases ? (
        // switch: one source handle per case + a default
        [...data.cases, "default"].map((label, i, arr) => {
          const top = `${((i + 1) / (arr.length + 1)) * 100}%`;
          const id = i < data.cases!.length ? `case${i}` : "default";
          return (
            <span key={id}>
              <Handle id={id} type="source" position={Position.Right} style={{ ...HANDLE_BASE, top, boxShadow: `0 0 0 2px ${m.color}` }} />
              <PortLabel text={label.length > 14 ? label.slice(0, 13) + "…" : label} top={top} />
            </span>
          );
        })
      ) : data.branches ? (
        <>
          <Handle id="true" type="source" position={Position.Right} style={{ ...HANDLE_BASE, top: "38%", boxShadow: "0 0 0 2px #4f9c84" }} />
          <Handle id="false" type="source" position={Position.Right} style={{ ...HANDLE_BASE, top: "72%", boxShadow: "0 0 0 2px #d65f59" }} />
        </>
      ) : (
        <Handle
          type="source"
          position={Position.Right}
          className="workflow-source-handle"
          title={`Drag to connect from ${data.label}; click to start a connection`}
          aria-label={`Connect from ${data.label}`}
          role="button"
          tabIndex={0}
          onKeyDown={activateHandleFromKeyboard}
          style={{
            width: 22, height: 22, right: insets.sourceRight, border: "none", borderRadius: "50%",
            background: "var(--surface-panel)", color: m.color,
            boxShadow: `0 0 0 1.5px ${data.outType && data.outType !== "any" ? typeColor(data.outType) : m.color}`,
            cursor: "crosshair", display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 16, lineHeight: 1, fontWeight: 600,
          }}
        >
          <span aria-hidden style={{ pointerEvents: "none", transform: "translateY(-0.5px)" }}>+</span>
        </Handle>
      )}
    </div>
  );
}

const nodeTypes = { workflow: WorkflowNode, note: NoteNode };

function computeLayout(allSteps: CanvasStep[]): Map<string, { x: number; y: number }> {
  // Notes are free-floating annotations — keep them out of the flow layout.
  const steps = allSteps.filter((s) => s.type !== "note");
  const byId = new Map(steps.map((s) => [s.id, s]));
  const succ = (s: CanvasStep) =>
    [...new Set([...(s.next || []), ...(s.true_next || []), ...(s.false_next || [])])].filter((id) => byId.has(id));
  const indeg = new Map(steps.map((s) => [s.id, 0]));
  steps.forEach((s) => succ(s).forEach((t) => indeg.set(t, (indeg.get(t) || 0) + 1)));
  const level = new Map<string, number>();
  const ind = new Map(indeg);
  const q = steps.filter((s) => (indeg.get(s.id) || 0) === 0).map((s) => s.id);
  q.forEach((id) => level.set(id, 0));
  while (q.length) {
    const id = q.shift()!;
    succ(byId.get(id)!).forEach((t) => {
      level.set(t, Math.max(level.get(t) ?? 0, (level.get(id) ?? 0) + 1));
      ind.set(t, (ind.get(t) || 0) - 1);
      if ((ind.get(t) || 0) === 0) q.push(t);
    });
  }
  steps.forEach((s) => { if (!level.has(s.id)) level.set(s.id, 0); });
  const byLevel: Record<number, string[]> = {};
  steps.forEach((s) => { const l = level.get(s.id)!; (byLevel[l] = byLevel[l] || []).push(s.id); });
  const pos = new Map<string, { x: number; y: number }>();
  Object.entries(byLevel).forEach(([l, ids]) =>
    ids.forEach((id, i) => pos.set(id, { x: Number(l) * (NODE_W + GAP_X), y: i * (NODE_H + GAP_Y) })),
  );
  return pos;
}

function buildGraph(
  steps: CanvasStep[],
  statusById?: Record<string, string>,
  previewById?: Record<string, MediaRef>,
  issueById?: Record<string, "error" | "warning">,
  outputById?: Record<string, string>,
): { nodes: Node<NodeData>[]; edges: Edge[] } {
  const layout = computeLayout(steps);
  const ids = new Set(steps.map((s) => s.id));
  const switchCases = (s: CanvasStep) => (s.type === "switch" ? s.config?.cases || [] : null);
  const nodes: Node<any>[] = steps.map((s) => {
    if (s.type === "note") {
      // sticky note — its own React Flow node type, no handles / not in the run
      return {
        id: s.id, type: "note",
        position: s.position || layout.get(s.id) || { x: 0, y: 0 },
        data: { text: s.config?.text ?? s.name ?? "" },
        zIndex: 0,
      };
    }
    const cases = switchCases(s);
    // connector nodes carry their integration's brand logo + colour
    const brand = s.type === "connector"
      ? (() => {
          const b = resolveConnectorBrand(s.meta?.original_type || s.config?.tool || s.config?.n8n?.type);
          return b.color || b.icon ? { color: b.color, path: b.icon?.path } : undefined;
        })()
      : undefined;
    // agent nodes surface their folded model / memory / tools as ports
    const agent = s.type === "agent"
      ? { model: s.config?.model, tools: s.config?.tools, memory: !!s.config?.memory }
      : undefined;
    return {
      id: s.id,
      type: "workflow",
      position: s.position || layout.get(s.id) || { x: 0, y: 0 },
      data: {
        type: s.type,
        label: s.name || s.meta?.original_type || s.type,
        branches: !!(s.true_next?.length || s.false_next?.length),
        cases: cases ? cases.map((c, i) => c.expression || `case ${i + 1}`) : undefined,
        status: statusById?.[s.id],
        preview: previewById?.[s.id],
        issue: issueById?.[s.id],
        output: outputById?.[s.id],
        brand,
        agent,
        outType: nodeOutputType(s),
      },
    };
  });

  const outTypeById = new Map(steps.map((s) => [s.id, nodeOutputType(s)]));
  const edges: Edge[] = [];
  const add = (from: string, to: string, kind: string, baseColor: string, handle?: string, label?: string) => {
    if (!ids.has(to)) return;
    // data ("default"/next) wires take the source node's output-type colour
    // (ComfyUI-style); branch wires (true/false/case) keep their semantic hue.
    const ot = outTypeById.get(from);
    const color = kind === "default" && ot && ot !== "any" ? typeColor(ot) : baseColor;
    const look = edgeLook(statusById?.[from], statusById?.[to], color);
    edges.push({
      id: `${from}-${to}-${kind}`, source: from, target: to,
      sourceHandle: handle, label, type: "deletable", // bezier curve + hover ✕
      animated: look.animated,
      data: { baseColor: color },
      style: { stroke: look.stroke, strokeWidth: look.strokeWidth },
      labelStyle: { fontSize: 10, fill: color, fontWeight: 600 },
    });
  };
  steps.forEach((s) => {
    const cases = switchCases(s);
    if (cases) {
      cases.forEach((c, i) => (c.next || []).forEach((t) => add(s.id, t, `case${i}`, "#8b6fb0", `case${i}`, c.expression?.slice(0, 14))));
      (s.config?.default_next || []).forEach((t) => add(s.id, t, "default", "#cfc9c1", "default", "default"));
      return; // switch routes via cases, not the flattened next
    }
    (s.next || []).forEach((t) => add(s.id, t, "default", "#cfc9c1"));
    (s.true_next || []).forEach((t) => add(s.id, t, "true", "#4f9c84", "true", "true"));
    (s.false_next || []).forEach((t) => add(s.id, t, "false", "#d65f59", "false", "false"));
  });
  return { nodes, edges };
}

// edits applied to the source-of-truth steps array, then persisted upstream
const branchKey = (h?: string | null) => (h === "true" ? "true_next" : h === "false" ? "false_next" : "next");

export default function WorkflowCanvas({
  steps,
  onStepsChange,
  onNodeOpen,
  onAddFrom,
  onAddNode,
  statusById,
  previewById,
  issueById,
  outputById,
  onRunNode,
}: {
  steps: CanvasStep[];
  onStepsChange?: (steps: CanvasStep[]) => void;
  onNodeOpen?: (stepId: string) => void;
  onAddFrom?: (sourceId: string) => void;
  onAddNode?: () => void;
  statusById?: Record<string, string>;
  previewById?: Record<string, MediaRef>;
  issueById?: Record<string, "error" | "warning">;
  outputById?: Record<string, string>;
  onRunNode?: (stepId: string) => void;
}) {
  const editable = !!onStepsChange;
  const initial = useMemo(() => buildGraph(steps, statusById, previewById, issueById, outputById), [steps, statusById, previewById, issueById, outputById]);
  const [nodes, setNodes, onNodesChange] = useNodesState(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initial.edges);

  // Full rebuild ONLY when the graph topology changes (nodes added/removed or
  // re-wired). Position-only changes are excluded so dragging isn't clobbered.
  const topoSig = useMemo(
    () => steps.map((s) => `${s.id}>${(s.next || []).join(",")}|${(s.true_next || []).join(",")}|${(s.false_next || []).join(",")}`).join(";"),
    [steps],
  );
  useEffect(() => {
    const g = buildGraph(steps, statusById, previewById, issueById, outputById);
    setNodes(g.nodes);
    setEdges(g.edges);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topoSig]);

  // Per-node run status / output preview / validation are PATCHED in place —
  // never a full rebuild — so a streaming run lighting up node-by-node doesn't
  // churn the whole graph (which can blank a wide canvas mid-stream).
  const decoSig = useMemo(
    () => steps.map((s) => `${s.id}#${statusById?.[s.id] || ""}@${previewById?.[s.id]?.url || ""}!${issueById?.[s.id] || ""}$${outputById?.[s.id] || ""}`).join(";"),
    [steps, statusById, previewById, issueById, outputById],
  );
  useEffect(() => {
    setNodes((nds) => nds.map((n) => (
      { ...n, data: { ...n.data, status: statusById?.[n.id], preview: previewById?.[n.id], issue: issueById?.[n.id], output: outputById?.[n.id] } }
    )));
    // Light up the active path: patch each edge from its endpoints' status so a
    // streaming run flows along the curves (leading edge animates, done edges
    // turn green) — patched in place, never a rebuild.
    setEdges((eds) => eds.map((e) => {
      const look = edgeLook(statusById?.[e.source], statusById?.[e.target], (e.data as { baseColor?: string })?.baseColor || "#cfc9c1");
      return { ...e, animated: look.animated, style: { ...e.style, stroke: look.stroke, strokeWidth: look.strokeWidth } };
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decoSig]);

  const onConnect = useCallback(
    (c: Connection) => {
      setEdges((eds) => addEdge({ ...c, type: "deletable" }, eds));
      if (!onStepsChange || !c.source || !c.target) return;
      const key = branchKey(c.sourceHandle);
      onStepsChange(
        steps.map((s) => {
          if (s.id !== c.source) return s;
          const arr = [...((s as any)[key] || [])];
          if (!arr.includes(c.target!)) arr.push(c.target!);
          return { ...s, [key]: arr };
        }),
      );
    },
    [steps, onStepsChange, setEdges],
  );

  const onEdgesDelete = useCallback(
    (removed: Edge[]) => {
      if (!onStepsChange) return;
      let next = steps;
      for (const e of removed) {
        const key = branchKey(e.sourceHandle);
        next = next.map((s) =>
          s.id === e.source ? { ...s, [key]: ((s as any)[key] || []).filter((t: string) => t !== e.target) } : s,
        );
      }
      onStepsChange(next);
    },
    [steps, onStepsChange],
  );

  const onNodeDragStop = useCallback(() => {
    if (!onStepsChange) return;
    const posById = new Map(nodes.map((n) => [n.id, n.position]));
    onStepsChange(steps.map((s) => ({ ...s, position: posById.get(s.id) || s.position })));
  }, [nodes, steps, onStepsChange]);

  const onEditNote = useCallback((nid: string, text: string) => {
    if (!onStepsChange) return;
    onStepsChange(steps.map((s) =>
      s.id === nid ? { ...s, name: (text.split("\n")[0] || "Note").slice(0, 40), config: { ...(s.config || {}), text } } : s,
    ));
  }, [steps, onStepsChange]);

  // delete a single connection (the edge's ✕): drop it from the canvas and
  // remove the corresponding link from the source step.
  const onDeleteEdge = useCallback((edgeId: string) => {
    const edge = edges.find((e) => e.id === edgeId);
    setEdges((eds) => eds.filter((e) => e.id !== edgeId));
    if (edge && onStepsChange) {
      const key = branchKey(edge.sourceHandle);
      onStepsChange(steps.map((s) =>
        s.id === edge.source ? { ...s, [key]: ((s as any)[key] || []).filter((t: string) => t !== edge.target) } : s,
      ));
    }
  }, [edges, steps, onStepsChange, setEdges]);

  // Delete one or more nodes from the source-of-truth steps. React Flow emits
  // onNodesDelete for keyboard deletion (including sticky notes); the hover
  // toolbar uses the same path so every deletion is persisted upstream.
  const deleteNodes = useCallback((nodeIds: string[]) => {
    if (!onStepsChange) return;
    const removed = new Set(nodeIds);
    onStepsChange(
      steps
        .filter((s) => !removed.has(s.id))
        .map((s) => ({
          ...s,
          next: (s.next || []).filter((t) => !removed.has(t)),
          true_next: (s.true_next || []).filter((t) => !removed.has(t)),
          false_next: (s.false_next || []).filter((t) => !removed.has(t)),
        })),
    );
  }, [steps, onStepsChange]);

  const onDeleteNode = useCallback((nodeId: string) => {
    deleteNodes([nodeId]);
  }, [deleteNodes]);

  const onNodesDelete = useCallback((removedNodes: Node[]) => {
    deleteNodes(removedNodes.map((node) => node.id));
  }, [deleteNodes]);

  const nodeActions = useMemo(
    () => ({ run: onRunNode, remove: editable ? onDeleteNode : undefined }),
    [onRunNode, editable, onDeleteNode],
  );

  return (
    <NodeActionsContext.Provider value={nodeActions}>
    <EdgeDeleteContext.Provider value={editable ? onDeleteEdge : null}>
    <NoteEditContext.Provider value={editable ? onEditNote : null}>
    <AddFromContext.Provider value={onAddFrom || null}>
    <div
      style={{ width: "100%", height: "100%", borderRadius: 16, overflow: "hidden" }}
      onDoubleClick={(e) => {
        if (onAddNode && (e.target as HTMLElement).classList?.contains("react-flow__pane")) onAddNode();
      }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={editable ? onConnect : undefined}
        onEdgesDelete={editable ? onEdgesDelete : undefined}
        onNodesDelete={editable ? onNodesDelete : undefined}
        onNodeDragStop={editable ? onNodeDragStop : undefined}
        onNodeDoubleClick={onNodeOpen ? (_, n) => { if (n.type !== "note") onNodeOpen(n.id); } : undefined}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
        nodesDraggable={editable}
        nodesConnectable={editable}
        connectOnClick
        connectionRadius={30}
        elementsSelectable
        fitView
        fitViewOptions={{ padding: 0.25 }}
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
        style={{ background: "var(--surface-app)" }}
      >
        {onAddNode && (
          <Panel position="top-left">
            <button
              onClick={onAddNode}
              style={{
                display: "flex", alignItems: "center", gap: 6,
                padding: "8px 13px", borderRadius: 10, border: "none", cursor: "pointer",
                background: "var(--accent)", color: "#fff", fontSize: 13, fontWeight: 600,
                boxShadow: "0 2px 8px rgba(15,118,110,0.25)",
              }}
            >
              <svg width={15} height={15} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
                <path d="M12 5v14M5 12h14" />
              </svg>
              Add node
            </button>
          </Panel>
        )}
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.4} color="rgba(28,25,23,0.12)" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => (n.type === "note" ? CAT_COLOR.note : typeMeta((n.data as NodeData)?.type || "").color)}
          maskColor="rgba(28,25,23,0.05)"
          style={{ background: "var(--surface-panel)" }}
        />
      </ReactFlow>
    </div>
    </AddFromContext.Provider>
    </NoteEditContext.Provider>
    </EdgeDeleteContext.Provider>
    </NodeActionsContext.Provider>
  );
}
