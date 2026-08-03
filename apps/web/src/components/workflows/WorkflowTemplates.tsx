import Modal from "../ui/Modal";
import IconTile from "../ui/IconTile";
import { TYPE_META, NodeIcon } from "./WorkflowCanvas";

/* One-click starting points (n8n / ComfyUI / Dify all ship a template gallery).
   Each template is a ready-made, valid canonical graph — trigger + end, required
   config filled — so it passes validation and runs out of the box. */

export interface WorkflowTemplate {
  key: string;
  name: string;
  description: string;
  icon: string; // a node type, for the tile glyph
  trigger_type: string;
  steps: any[];
}

export const TEMPLATES: WorkflowTemplate[] = [
  {
    key: "ai-image",
    name: "AI image pipeline",
    description: "Expand an idea into a prompt, generate an image, share it.",
    icon: "image",
    trigger_type: "manual",
    steps: [
      { id: "t", type: "trigger", name: "Start", config: {}, next: ["p"] },
      { id: "p", type: "llm", name: "Expand prompt", config: { prompt: "Turn this idea into a vivid image prompt: {{idea}}" }, next: ["img"] },
      { id: "img", type: "image", name: "Generate image", config: { prompt: "{{p.output}}" }, next: ["n"] },
      { id: "n", type: "notify", name: "Share result", config: { channel: "slack", message: "New image is ready." }, next: ["e"] },
      { id: "e", type: "end", name: "Done", config: {}, next: [] },
    ],
  },
  {
    key: "support-triage",
    name: "Support email triage",
    description: "Classify urgency, draft a reply, route urgent tickets to on-call.",
    icon: "classifier",
    trigger_type: "webhook",
    steps: [
      { id: "w", type: "webhook", name: "Inbound email", config: {}, next: ["c"] },
      { id: "c", type: "classifier", name: "Classify urgency", config: { prompt: "Classify urgency (low/medium/high) for this email: {{w.body}}" }, next: ["a"] },
      { id: "a", type: "agent", name: "Draft reply", config: { input: "Draft a reply to: {{w.body}}" }, next: ["cond"] },
      { id: "cond", type: "condition", name: "Urgent?", config: { expression: "{{c.output}} == 'high'" }, true_next: ["nu"], false_next: ["nr"], next: [] },
      { id: "nu", type: "notify", name: "Alert on-call", config: { channel: "slack", message: "🚨 Urgent ticket: {{w.subject}}" }, next: ["e"] },
      { id: "nr", type: "notify", name: "Queue reply", config: { channel: "email", message: "New ticket: {{w.subject}}" }, next: ["e"] },
      { id: "e", type: "end", name: "Done", config: {}, next: [] },
    ],
  },
  {
    key: "daily-digest",
    name: "Daily digest",
    description: "Gather the week's updates, summarize, and post to the team.",
    icon: "rag",
    trigger_type: "manual",
    steps: [
      { id: "t", type: "trigger", name: "Daily trigger", config: {}, next: ["r"] },
      { id: "r", type: "rag", name: "Gather updates", config: { query: "this week's updates", limit: 10 }, next: ["l"] },
      { id: "l", type: "llm", name: "Summarize", config: { prompt: "Summarize into a short digest:\n{{r.output}}" }, next: ["n"] },
      { id: "n", type: "notify", name: "Post digest", config: { channel: "slack", message: "{{l.output}}" }, next: ["e"] },
      { id: "e", type: "end", name: "Done", config: {}, next: [] },
    ],
  },
  {
    key: "web-summary",
    name: "Web page → summary",
    description: "Fetch a URL, summarize the content, email it over.",
    icon: "http",
    trigger_type: "manual",
    steps: [
      { id: "t", type: "trigger", name: "Start", config: {}, next: ["h"] },
      { id: "h", type: "http", name: "Fetch page", config: { method: "GET", url: "{{url}}" }, next: ["l"] },
      { id: "l", type: "llm", name: "Summarize", config: { prompt: "Summarize this page content:\n{{h.body}}" }, next: ["n"] },
      { id: "n", type: "notify", name: "Send summary", config: { channel: "email", message: "{{l.output}}" }, next: ["e"] },
      { id: "e", type: "end", name: "Done", config: {}, next: [] },
    ],
  },
];

export default function WorkflowTemplates({
  open,
  onClose,
  onPick,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (template: WorkflowTemplate) => void;
}) {
  return (
    <Modal open={open} onClose={onClose} title="Start from a template">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 12 }}>
        {TEMPLATES.map((tpl) => {
          const m = TYPE_META[tpl.icon] || { color: "#4f7d75" };
          return (
            <button
              key={tpl.key}
              onClick={() => onPick(tpl)}
              style={{
                display: "flex", flexDirection: "column", gap: 8, padding: 14, borderRadius: 14,
                border: "none", cursor: "pointer", textAlign: "left",
                background: "var(--surface-muted)", transition: "background .15s",
              }}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "var(--surface-sunken)"; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "var(--surface-muted)"; }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <IconTile color={m.color} size={34}>
                  <NodeIcon type={tpl.icon} size={18} />
                </IconTile>
                <span style={{ fontSize: 13.5, fontWeight: 700, color: "var(--text-strong)" }}>{tpl.name}</span>
              </div>
              <span style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>{tpl.description}</span>
              <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>{tpl.steps.length} nodes</span>
            </button>
          );
        })}
      </div>
    </Modal>
  );
}
