import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { useToastStore } from "../../stores/toast";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Select from "../ui/Select";

const TRIGGER_OPTIONS = [
  { value: "manual", label: "Manual — run on demand / API" },
  { value: "schedule", label: "Schedule — run on a cron" },
  { value: "event", label: "Event — a named app event" },
  { value: "workspace_event", label: "Workspace event — a workspace activity" },
  { value: "webhook", label: "Webhook — inbound URL" },
  { value: "error", label: "Error — run when a workflow fails" },
  { value: "mcp", label: "Tool — callable by AI agents (MCP)" },
];

const CRON_PRESETS = [
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Daily 9am", cron: "0 9 * * *" },
  { label: "Weekdays 8am", cron: "0 8 * * 1-5" },
  { label: "Mondays 8am", cron: "0 8 * * 1" },
];

/** Deploy a workflow into a workspace / business line as a triggered binding. */
export default function WorkflowDeployModal({
  flow,
  open,
  onClose,
}: {
  flow: { id: string; name: string } | null;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToastStore();
  const [workspaceId, setWorkspaceId] = useState("");
  const [businessLine, setBusinessLine] = useState("");
  const [triggerType, setTriggerType] = useState("manual");
  const [eventName, setEventName] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [toolDesc, setToolDesc] = useState("");

  const { data: workspaces = [] } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
    enabled: open,
  });

  const { data: bindings = [] } = useQuery({
    queryKey: ["workflow-bindings", flow?.id],
    queryFn: () => api.workflows.listBindings({ workflow_id: flow!.id }),
    enabled: open && !!flow,
  });

  const reset = () => {
    setWorkspaceId("");
    setBusinessLine("");
    setTriggerType("manual");
    setEventName("");
    setCron("0 9 * * *");
    setToolDesc("");
  };

  const deployMut = useMutation({
    mutationFn: () =>
      api.workflows.createBinding({
        workflow_id: flow!.id,
        workspace_id: workspaceId || undefined,
        business_line: businessLine || undefined,
        trigger_type: triggerType,
        trigger_config:
          triggerType === "schedule"
            ? { cron: cron.trim() }
            : triggerType === "event" || triggerType === "workspace_event"
              ? { event: eventName }
              : triggerType === "mcp"
                ? { description: toolDesc.trim() || undefined }
                : {},
      }),
    onSuccess: (res: { kind?: string; cron?: string; trigger_config?: { webhook_token?: string } }) => {
      const token = res?.trigger_config?.webhook_token;
      toast.success(
        res?.kind === "automation"
          ? `Scheduled · ${res.cron}`
          : token
            ? `Deployed. Webhook: /api/v1/workflows/webhook/${token}`
            : "Workflow deployed",
      );
      qc.invalidateQueries({ queryKey: ["workflow-bindings"] });
      qc.invalidateQueries({ queryKey: ["scheduled-jobs"] });
      reset();
      onClose();
    },
    onError: (e) => toast.error(translateApiError(e, "Deploy failed")),
  });

  const toggleMut = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.workflows.updateBinding(id, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workflow-bindings", flow?.id] }),
    onError: (e) => toast.error(translateApiError(e, "Couldn't update deployment")),
  });

  const removeMut = useMutation({
    mutationFn: (id: string) => api.workflows.deleteBinding(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workflow-bindings", flow?.id] });
      toast.success("Deployment removed");
    },
    onError: (e) => toast.error(translateApiError(e, "Couldn't remove deployment")),
  });

  const needsEvent = triggerType === "event" || triggerType === "workspace_event";
  const canDeploy =
    (!needsEvent || eventName.trim().length > 0) &&
    (triggerType !== "schedule" || cron.trim().split(/\s+/).length === 5);

  return (
    <Modal
      open={open}
      onClose={() => { reset(); onClose(); }}
      title={flow ? `Deploy "${flow.name}"` : "Deploy workflow"}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={() => { reset(); onClose(); }}>
            Cancel
          </Button>
          <Button onClick={() => deployMut.mutate()} loading={deployMut.isPending} disabled={!canDeploy}>
            Deploy
          </Button>
        </div>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>
          Deploy this workflow into a workspace as a business-line binding. The same
          workflow can be deployed to many workspaces; each binding runs with that
          workspace's connectors, knowledge and approvers.
        </p>

        {(bindings as any[]).length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 7, padding: "10px 0 14px", borderBottom: "1px solid rgba(28,25,23,0.08)" }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-faint)" }}>Existing deployments</span>
            {(bindings as any[]).map((binding) => {
              const workspace = (workspaces as { id: string; name: string }[]).find((w) => w.id === binding.workspace_id);
              return (
                <div key={binding.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 10px", borderRadius: 9, background: "var(--surface-muted)" }}>
                  <span style={{ width: 7, height: 7, borderRadius: "50%", background: binding.enabled ? "#4f9c84" : "#a8a29e", flexShrink: 0 }} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-strong)" }}>{binding.name || binding.trigger_type}</div>
                    <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
                      {binding.trigger_type} · {workspace?.name || "Entity-level"}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => toggleMut.mutate({ id: binding.id, enabled: !binding.enabled })}
                    disabled={toggleMut.isPending}
                    style={{ border: "none", background: "transparent", color: "var(--text-muted)", cursor: "pointer", fontSize: 11.5, fontWeight: 600 }}
                  >
                    {binding.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    type="button"
                    aria-label={`Remove ${binding.name || "deployment"}`}
                    onClick={() => removeMut.mutate(binding.id)}
                    disabled={removeMut.isPending}
                    style={{ border: "none", background: "transparent", color: "#b4534d", cursor: "pointer", fontSize: 16, lineHeight: 1 }}
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        )}

        <Field label="Workspace">
          <Select
            value={workspaceId}
            onChange={setWorkspaceId}
            placeholder="Entity-level (no workspace)"
            options={(workspaces as { id: string; name: string }[]).map((w) => ({
              value: w.id,
              label: w.name,
            }))}
          />
        </Field>

        <Field label="Business line">
          <Input
            placeholder="e.g. leasing, maintenance, billing (optional)"
            value={businessLine}
            onChange={(e) => setBusinessLine(e.target.value)}
          />
        </Field>

        <Field label="Trigger">
          <Select value={triggerType} onChange={setTriggerType} options={TRIGGER_OPTIONS} />
        </Field>

        {triggerType === "schedule" && (
          <Field label="Cron schedule">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
              {CRON_PRESETS.map((p) => {
                const active = cron.trim() === p.cron;
                return (
                  <button
                    key={p.cron}
                    type="button"
                    onClick={() => setCron(p.cron)}
                    style={{
                      fontSize: 12, fontWeight: 600, padding: "4px 10px", borderRadius: 999, border: "none", cursor: "pointer",
                      background: active ? "rgba(15,118,110,0.10)" : "var(--surface-muted)",
                      color: active ? "var(--accent)" : "var(--text-muted)",
                    }}
                  >
                    {p.label}
                  </button>
                );
              })}
            </div>
            <Input
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder="min hour day month weekday"
              className="mono"
            />
            <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
              Standard 5-field cron (UTC). Creates an automation that fires this workflow.
            </span>
          </Field>
        )}

        {needsEvent && (
          <Field label="Event name">
            <Input
              placeholder="e.g. lead.created"
              value={eventName}
              onChange={(e) => setEventName(e.target.value)}
            />
          </Field>
        )}

        {triggerType === "webhook" && (
          <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>
            A secret webhook URL is generated on deploy — POST to it to fire this workflow.
          </p>
        )}

        {triggerType === "error" && (
          <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>
            Runs whenever a workflow fails. The failed workflow id, run id and error
            arrive as trigger data — branch on them to alert, log or recover.
          </p>
        )}

        {triggerType === "mcp" && (
          <Field label="Tool description">
            <Input
              placeholder="What this workflow does (shown to the agent)"
              value={toolDesc}
              onChange={(e) => setToolDesc(e.target.value)}
            />
            <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
              Publishes this workflow as a tool agents can call via run_workflow. Its
              variables become the tool's inputs; its result is returned to the agent.
            </span>
          </Field>
        )}
      </div>
    </Modal>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style={{ fontSize: 11, fontWeight: 500, letterSpacing: 0.2, color: "var(--text-faint)" }}>
        {label}
      </span>
      {children}
    </label>
  );
}
