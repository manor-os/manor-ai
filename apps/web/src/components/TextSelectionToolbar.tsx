import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  ADD_SELECTION_TO_TASK_EVENT,
  INSERT_CHAT_COMPOSER_EVENT,
  OPEN_FLOATING_CHAT_EVENT,
  SELECTED_TEXT_TASK_DRAFT_KEY,
  type InsertChatComposerDetail,
  type OpenFloatingChatDetail,
  type SelectedTextTaskDraft,
} from "../lib/selectionActions";
import ChatActionButton from "./chat/ChatActionButton";
import { IconChatBubble, IconClipboard, IconInfo } from "./icons";

type ToolbarState = {
  text: string;
  top: number;
  left: number;
  surface: SelectionSurface;
  sourceLabel?: string;
  sourcePath?: string;
};

const MAX_SELECTION_LENGTH = 6000;
const SOURCE_SELECTOR = "[data-selection-context], [data-selection-source-label], [data-selection-source-path]";

type SelectionSurface = "chat" | "knowledge" | "task" | "workspace" | "generic";

function elementFromNode(node: Node | null) {
  return node instanceof Element ? node : node?.parentElement || null;
}

function surfaceFromPath(pathname: string): SelectionSurface {
  if (pathname === "/chat") return "chat";
  if (/^\/(knowledge|viewer|editor|video-editor)(\/|$)/.test(pathname)) return "knowledge";
  if (/^\/tasks(\/|$)/.test(pathname)) return "task";
  if (/^\/workspaces(\/|$)/.test(pathname)) return "workspace";
  return "generic";
}

function selectionSource(selection: Selection) {
  let range: Range | null = null;
  try {
    range = selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
  } catch {
    range = null;
  }
  const element =
    elementFromNode(range?.commonAncestorContainer || null) ||
    elementFromNode(selection.anchorNode) ||
    elementFromNode(selection.focusNode);
  const sourceElement = element?.closest(SOURCE_SELECTOR) as HTMLElement | null;
  const currentPath = `${window.location.pathname}${window.location.search}`;
  const context = sourceElement?.dataset.selectionContext;
  const surface =
    context === "knowledge" || context === "chat" || context === "task" || context === "workspace"
      ? context
      : surfaceFromPath(window.location.pathname);
  const sourcePath = sourceElement?.dataset.selectionSourcePath || currentPath;
  const sourceLabel =
    sourceElement?.dataset.selectionSourceLabel ||
    (surface === "knowledge" ? "Knowledge Base" : undefined);

  return { surface, sourceLabel, sourcePath };
}

function isBlockedSelectionTarget(node: Node | null) {
  const element = node instanceof Element ? node : node?.parentElement;
  if (!element) return false;
  return Boolean(
    element.closest(
      [
        "input",
        "textarea",
        "select",
        "button",
        "[contenteditable='true']",
        "[role='textbox']",
        ".no-selection-toolbar",
        ".text-selection-toolbar",
        ".ProseMirror",
        ".monaco-editor",
      ].join(","),
    ),
  );
}

function selectedTextTitle(text: string) {
  const firstLine = text
    .split(/\n+/)
    .map((line) => line.trim())
    .find(Boolean);
  const compact = (firstLine || "Selected text").replace(/\s+/g, " ");
  return compact.length > 88 ? `${compact.slice(0, 85)}...` : compact;
}

function sourceSummary(state: ToolbarState) {
  if (!state.sourceLabel && !state.sourcePath) return "";
  return [state.sourceLabel, state.sourcePath].filter(Boolean).join(" · ");
}

function buildSelectionPrompt(state: ToolbarState, kind: "details" | "side-chat") {
  if (state.surface === "knowledge") {
    const source = sourceSummary(state);
    const sourceBlock = source ? `Source: ${source}\n\n` : "";
    return kind === "details"
      ? `Explain this Knowledge Base selection and how it should be used:\n\n${sourceBlock}${state.text}`
      : `Use this Knowledge Base selection as context:\n\n${sourceBlock}${state.text}`;
  }

  return kind === "details"
    ? `Explain this selected text clearly and give the useful context:\n\n${state.text}`
    : `Help me with this selected text:\n\n${state.text}`;
}

function buildTaskDraft(state: ToolbarState, currentPath: string): SelectedTextTaskDraft {
  if (state.surface === "knowledge") {
    const source = sourceSummary(state);
    return {
      title: selectedTextTitle(state.text),
      description: `Selected Knowledge Base text:\n\n${state.text}`,
      sourcePath: source || currentPath,
    };
  }

  return {
    title: selectedTextTitle(state.text),
    description: state.text,
    sourcePath: currentPath,
  };
}

function getSelectionToolbarState(): ToolbarState | null {
  let selection: Selection | null = null;
  let range: Range | null = null;
  try {
    selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
    if (
      isBlockedSelectionTarget(selection.anchorNode) ||
      isBlockedSelectionTarget(selection.focusNode)
    ) {
      return null;
    }

    const text = selection.toString().trim();
    if (text.length < 2) return null;

    range = selection.getRangeAt(0);
    const rects = Array.from(range.getClientRects()).filter(
      (rect) => rect.width > 0 && rect.height > 0,
    );
    const rect = rects[0] || range.getBoundingClientRect();
    if (!rect || (!rect.width && !rect.height)) return null;

    const source = selectionSource(selection);
    const toolbarWidth = 120;
    const toolbarHeight = 40;
    const gap = 10;
    const left = Math.min(
      Math.max(rect.left + rect.width / 2 - toolbarWidth / 2, 12),
      window.innerWidth - toolbarWidth - 12,
    );
    const top =
      rect.top > toolbarHeight + gap + 8
        ? rect.top - toolbarHeight - gap
        : rect.bottom + gap;

    return {
      text: text.slice(0, MAX_SELECTION_LENGTH),
      top: Math.max(12, Math.min(top, window.innerHeight - toolbarHeight - 12)),
      left,
      ...source,
    };
  } catch {
    // Browser selection objects can become stale when rich previews rerender or
    // browser extensions mutate the DOM. The toolbar is optional, so fail closed.
    return null;
  }
}

export default function TextSelectionToolbar() {
  const [state, setState] = useState<ToolbarState | null>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    let raf = 0;
    const update = () => {
      window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(() => {
        const next = getSelectionToolbarState();
        setState(next);
      });
    };
    const hideIfOutside = (event: MouseEvent) => {
      if (
        toolbarRef.current &&
        event.target instanceof Node &&
        toolbarRef.current.contains(event.target)
      ) {
        return;
      }
      window.setTimeout(update, 0);
    };

    document.addEventListener("selectionchange", update);
    document.addEventListener("mouseup", update);
    document.addEventListener("keyup", update);
    document.addEventListener("mousedown", hideIfOutside);
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.cancelAnimationFrame(raf);
      document.removeEventListener("selectionchange", update);
      document.removeEventListener("mouseup", update);
      document.removeEventListener("keyup", update);
      document.removeEventListener("mousedown", hideIfOutside);
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, []);

  const clearSelection = () => {
    window.getSelection()?.removeAllRanges();
    setState(null);
  };

  const openSideChat = (kind: "details" | "side-chat") => {
    if (!state) return;
    const prompt = buildSelectionPrompt(state, kind);
    const isEmbeddedChatSurface = location.pathname === "/chat";
    const eventName = isEmbeddedChatSurface
      ? INSERT_CHAT_COMPOSER_EVENT
      : OPEN_FLOATING_CHAT_EVENT;
    window.dispatchEvent(
      new CustomEvent<OpenFloatingChatDetail | InsertChatComposerDetail>(eventName, {
        detail: { prompt, source: "text-selection-toolbar" },
      }),
    );
    clearSelection();
  };

  const openDetails = () => {
    if (!state) return;
    const currentPath = `${location.pathname}${location.search}`;
    if (
      state.surface === "knowledge" &&
      state.sourcePath &&
      state.sourcePath.startsWith("/") &&
      state.sourcePath !== currentPath
    ) {
      const nextPath = state.sourcePath;
      clearSelection();
      navigate(nextPath);
      return;
    }
    openSideChat("details");
  };

  const addToTask = () => {
    if (!state) return;
    const draft = buildTaskDraft(state, `${location.pathname}${location.search}`);
    sessionStorage.setItem(SELECTED_TEXT_TASK_DRAFT_KEY, JSON.stringify(draft));
    window.dispatchEvent(
      new CustomEvent<SelectedTextTaskDraft>(ADD_SELECTION_TO_TASK_EVENT, {
        detail: draft,
      }),
    );
    clearSelection();
    navigate("/tasks");
  };

  if (!state) return null;

  return (
    <div
      ref={toolbarRef}
      className="text-selection-toolbar"
      style={{ top: state.top, left: state.left }}
      onMouseDown={(event) => event.preventDefault()}
    >
      <ChatActionButton
        className="text-selection-toolbar-action"
        onClick={addToTask}
        title={state.surface === "knowledge" ? "Add Knowledge Base selection to task" : "Create a task from the selected text"}
        aria-label={state.surface === "knowledge" ? "Add Knowledge Base selection to task" : "Add selected text to task"}
      >
        <IconClipboard size={13} aria-hidden="true" />
      </ChatActionButton>
      <ChatActionButton
        className="text-selection-toolbar-action"
        onClick={openDetails}
        title={state.surface === "knowledge" ? "More details about this Knowledge Base selection" : "Ask Manor to explain the selected text"}
        aria-label={state.surface === "knowledge" ? "More details about this Knowledge Base selection" : "Explain selected text"}
      >
        <IconInfo size={13} aria-hidden="true" />
      </ChatActionButton>
      <ChatActionButton
        className="text-selection-toolbar-action"
        onClick={() => openSideChat("side-chat")}
        title={state.surface === "knowledge" ? "Ask with this Knowledge Base selection" : "Open side chat with the selected text"}
        aria-label={state.surface === "knowledge" ? "Ask with this Knowledge Base selection" : "Ask in side chat about selected text"}
      >
        <IconChatBubble size={13} aria-hidden="true" />
      </ChatActionButton>
    </div>
  );
}
