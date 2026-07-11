import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Modal from "../../components/ui/Modal";
import Button from "../../components/ui/Button";
import Input from "../../components/ui/Input";
import Textarea from "../../components/ui/Textarea";
import { AiBuildConversation } from "../../components/ai/AiBuildConversation";
import { useToastStore } from "../../stores/toast";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { CATEGORIES, formatCategory } from "./skillTypes";

interface SkillFormModalProps {
  /** Skill to edit; null = create mode. */
  skill: any | null;
  open: boolean;
  onClose: () => void;
}

type CreateMode = "ai" | "manual";

export function SkillFormModal({ skill, open, onClose }: SkillFormModalProps) {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  // Editing always uses the manual form; creating defaults to the AI flow.
  const [mode, setMode] = useState<CreateMode>("ai");

  // ── Manual form state ──
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [prompt, setPrompt] = useState("");
  const [category, setCategory] = useState("");
  const [tools, setTools] = useState("");
  const [outputFormat, setOutputFormat] = useState("text");

  useEffect(() => {
    if (!open) return;
    setMode(skill ? "manual" : "ai");
    if (skill) {
      setName((skill.name as string) || "");
      setDesc((skill.description as string) || "");
      setPrompt((skill.system_prompt as string) || "");
      setCategory((skill.category as string) || "");
      setTools(Array.isArray(skill.tools) ? (skill.tools as string[]).join(", ") : "");
      setOutputFormat((skill.output_format as string) || "text");
    } else {
      setName("");
      setDesc("");
      setPrompt("");
      setCategory("");
      setTools("");
      setOutputFormat("text");
    }
  }, [open, skill]);

  const createMutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.skills.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills"] });
      toast.success(t("page.skills.skill_created"));
      onClose();
    },
    onError: (e: any) => toast.error(e?.message || t("page.skills.failed_create_skill")),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      api.skills.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills"] });
      onClose();
    },
    onError: (e: any) => toast.error(e?.message || t("page.skills.failed_update_skill")),
  });

  const handleSubmit = () => {
    if (!name.trim()) return;
    const payload = {
      name,
      description: desc,
      system_prompt: prompt,
      category,
      tools: tools.split(",").map((x) => x.trim()).filter(Boolean),
      output_format: outputFormat,
    };
    if (skill) updateMutation.mutate({ id: skill.id as string, data: payload });
    else createMutation.mutate(payload);
  };

  const isPending = createMutation.isPending || updateMutation.isPending;
  const creating = !skill;
  const aiMode = mode === "ai";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={skill ? t("page.skills.edit_skill") : t("page.skills.create_entity_skill")}
      maxWidth="36rem"
      footer={
        aiMode ? undefined : (
          <>
            <Button variant="outline" onClick={onClose}>{t("action.cancel")}</Button>
            <Button variant="primary" onClick={handleSubmit} disabled={!name.trim() || isPending}>
              {skill ? t("page.custom_fields.update") : t("action.create")}
            </Button>
          </>
        )
      }
    >
      {/* Mode switch — available for both create and edit */}
      <div style={{ display: "flex", gap: 6, marginBottom: 14 }}>
        {(["ai", "manual"] as CreateMode[]).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            style={{
              flex: 1,
              padding: "8px 10px",
              borderRadius: 8,
              border: `1px solid ${mode === m ? "var(--modal-border-strong)" : "var(--modal-border)"}`,
              background: mode === m ? "var(--modal-muted-bg)" : "transparent",
              color: mode === m ? "var(--text-strong)" : "var(--text-muted)",
              fontSize: 13,
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {m === "ai"
              ? (creating ? t("page.skill_form.ai_mode") : t("page.skill_form.ai_edit_mode"))
              : t("page.skill_form.manual_mode")}
          </button>
        ))}
      </div>

      {aiMode ? (
        creating ? (
          <AiBuildConversation
            intro={t("page.skill_form.ai_intro")}
            describePlaceholder={t("page.skill_form.ai_describe_placeholder")}
            answersPlaceholder={t("page.skill_form.ai_answers_placeholder")}
            buildingHint={t("page.skill_form.ai_building_hint")}
            draftQuestions={(p) => api.skills.draftQuestions(p)}
            generate={async (p, onStep) => {
              try {
                await api.skills.generateStream(p, onStep);
              } catch (e: any) {
                toast.error(e?.message || t("page.skills.failed_create_skill"));
                throw e;
              }
              queryClient.invalidateQueries({ queryKey: ["skills"] });
              toast.success(t("page.skills.skill_created"));
              onClose();
            }}
          />
        ) : (
          <AiBuildConversation
            intro={t("page.skill_form.ai_edit_intro")}
            describePlaceholder={t("page.skill_form.ai_edit_describe_placeholder")}
            answersPlaceholder={t("page.skill_form.ai_answers_placeholder")}
            buildingHint={t("page.skill_form.ai_edit_building_hint")}
            // Edits go straight to applying the change — no clarifying step.
            draftQuestions={async () => ({ questions: [], ready: true })}
            generate={async (p) => {
              try {
                await api.skills.aiUpdate(skill.id as string, p);
              } catch (e: any) {
                toast.error(e?.message || t("page.skills.failed_update_skill"));
                throw e;
              }
              queryClient.invalidateQueries({ queryKey: ["skills"] });
              toast.success(t("page.skills.skill_updated"));
              onClose();
            }}
          />
        )
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Input
            label={t("page.flows.name")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("page.skill_form.name_placeholder")}
          />
          <Textarea
            label={t("page.flows.description")}
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            rows={2}
            placeholder={t("page.skill_form.description_placeholder")}
          />
          <Textarea
            label={t("page.skill_form.system_prompt")}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={5}
            placeholder={t("page.skill_form.system_prompt_placeholder")}
          />
          <Input
            label={t("page.skill_form.tools_csv")}
            value={tools}
            onChange={(e) => setTools(e.target.value)}
            placeholder={t("page.skill_form.tools_placeholder")}
          />
          <div
            style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 14 }}
          >
            <div>
              <label
                style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}
              >
                {t("page.workspaces.category")}
              </label>
              <select value={category} onChange={(e) => setCategory(e.target.value)} className="manor-input">
                <option value="">{t("page.skill_form.select")}</option>
                {CATEGORIES.filter((c) => c !== "All").map((cat) => (
                  <option key={cat} value={cat}>{formatCategory(cat)}</option>
                ))}
              </select>
            </div>
            <div>
              <label
                style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}
              >
                {t("page.skill_form.output_format")}
              </label>
              <select value={outputFormat} onChange={(e) => setOutputFormat(e.target.value)} className="manor-input">
                <option value="text">{t("page.skill_form.text")}</option>
                <option value="json">{t("page.skill_form.json")}</option>
                <option value="markdown">{t("page.skill_form.markdown")}</option>
                <option value="html">{t("page.skill_form.html")}</option>
              </select>
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}
