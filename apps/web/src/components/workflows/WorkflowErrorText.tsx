import { splitWorkflowErrorItems } from "./workflowRunDisplay";

/** A run error: its lead-in sentence, then each listed item on its own line. */
export default function WorkflowErrorText({ text, className }: { text: string; className?: string }) {
  const { lead, items } = splitWorkflowErrorItems(text);
  return (
    <div className={`workflow-error-text${className ? ` ${className}` : ""}`}>
      {lead.map((line, index) => <p key={index}>{line}</p>)}
      {items.length > 0 && (
        <ul>
          {items.map((item, index) => <li key={index}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}
