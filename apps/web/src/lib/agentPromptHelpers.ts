import type { RefObject } from "react";
import { api } from "./api";
import { t } from "./i18n";
import { useToastStore } from "../stores/toast";

// Improve prompt with AI
export const improvePrompt = async (
  currentPrompt: string,
  agentName: string,
  description: string,
  onResult: (improved: string) => void,
  setLoading: (v: boolean) => void,
) => {
  if (!currentPrompt.trim()) return;
  setLoading(true);
  try {
    const meta = `${t("page.agents.improve_prompt_meta_agent").replace("{name}", agentName)}${description ? `\n${t("page.agents.improve_prompt_meta_description").replace("{description}", description)}` : ""}`;
    const systemPrompt = t("page.agents.improve_prompt_system_prompt")
      .replace("{agentName}", agentName)
      .replace("{meta}", meta)
      .replace("{currentPrompt}", currentPrompt);
    const res = await api.agents.previewPrompt(
      systemPrompt,
      t("page.agents.improve_prompt_user_message"),
    );
    let improved = (res.response || "").trim();
    improved = improved.replace(/^[\s\S]*?---\s*\n/m, "").trim();
    improved = improved
      .replace(/^```[\s\S]*?\n/, "")
      .replace(/\n```\s*$/, "")
      .trim();
    improved = improved.replace(/^\*\*Improved .*?\*\*:?\s*/i, "").trim();
    improved = improved
      .replace(/^Here'?s the improved prompt.*?:\s*/i, "")
      .trim();
    if (improved && improved.length > 20) {
      onResult(improved);
      useToastStore.getState().success(t("page.agents.prompt_improved"));
    } else {
      useToastStore
        .getState()
        .error(t("page.agents.could_not_improve_try_adding_more_details_first"));
    }
  } catch {
    useToastStore.getState().error(t("page.agents.failed_to_improve_prompt"));
  } finally {
    setLoading(false);
  }
};

export const runTest = async (
  systemPrompt: string,
  testMsg: string,
  setResp: (v: string) => void,
  setLoading: (v: boolean) => void,
) => {
  if (!testMsg.trim() || !systemPrompt.trim()) return;
  setLoading(true);
  setResp("");
  try {
    const res = await api.agents.previewPrompt(systemPrompt, testMsg);
    setResp(res.response || "");
  } catch {
    setResp(t("page.agents.request_failed"));
  } finally {
    setLoading(false);
  }
};

export const PROMPT_VARIABLES = [
  "{{agentName}}",
  "{{clientName}}",
  "{{entityName}}",
];

export const insertVariable = (
  v: string,
  ref: RefObject<HTMLTextAreaElement>,
  text: string,
  setText: (t: string) => void,
) => {
  const el = ref.current;
  if (el) {
    const start = el.selectionStart;
    const end = el.selectionEnd;
    const newText = text.slice(0, start) + v + text.slice(end);
    setText(newText);
    setTimeout(() => {
      el.selectionStart = el.selectionEnd = start + v.length;
      el.focus();
    }, 0);
  } else {
    setText(text + v);
  }
};
