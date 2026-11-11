import { t } from "../../lib/i18n";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { useToastStore } from "../../stores/toast";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Select from "../ui/Select";
import { workflowRunInputs } from "../../lib/workflowRunInputs";

const TRIGGER_OPTIONS = [
  { value: "manual", label: t("ui.manual_run_on_demand_api") },
  { value: "schedule", label: t("ui.schedule_run_on_a_cron") },
  { value: "event", label: t("ui.event_a_named_app_event") },
  { value: "workspace_event", label: t("ui.workspace_event_a_workspace_activity") },
  { value: "webhook", label: t("ui.webhook_inbound_url") },
  { value: "error", label: t("ui.error_run_when_a_workflow_fails") },
  { value: "mcp", label: t("ui.tool_callable_by_ai_agents_mcp") },
];

const CRON_PRESETS = [
  { label: t("ui.every_hour"), cron: "0 * * * *" },
  { label: t("ui.daily_9am"), cron: "0 9 * * *" },
  { label: t("ui.weekdays_8am"), cron: "0 8 * * 1-5" },
  { label: t("ui.mondays_8am"), cron: "0 8 * * 1" },
];

/** Deploy a workflow into a workspace / business line as a triggered binding. */
export default function WorkflowDeployModal({
  flow,
  open,
  onClose,
}: {
  flow: Parameters<typeof workflowRunInputs>[0] & { id: string; name: string } | null;
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
  // A cron fires with no one to fill the Run dialog; the server rejects this too.
  const unscheduledInputs = flow
    ? workflowRunInputs(flow).filter((input) => input.required && !input.defaultValue.trim())
    : [];
  const scheduleBlocked = triggerType === "schedule" && unscheduledInputs.length > 0;
  const canDeploy =
    (!needsEvent || eventName.trim().length > 0) &&
    (triggerType !== "schedule" || cron.trim().split(/\s+/).length === 5) &&
    !scheduleBlocked;

  return (
    <Modal
      open={open}
      onClose={() => { reset(); onClose(); }}
      title={flow ? `Deploy "${flow.name}"` : "Deploy workflow"}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={() => { reset(); onClose(); }}>
            {t("ui.cancel")}
          </Button>
          <Button onClick={() => deployMut.mutate()} loading={deployMut.isPending} disabled={!canDeploy}>
            {t("ui.deploy")}
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
          <div style={{ display: "flex", flexDirection: "column", gap: 7, padding: "10px 0 14px", borderBottom: "0.5px solid rgba(29, 29, 31,0.08)" }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-faint)" }}>{t("ui.existing_deployments")}</span>
            {(bindings as any[]).map((binding) => {
              const workspace = (workspaces as { id: string; name: string }[]).find((w) => w.id === binding.workspace_id);
              return (
                <div key={binding.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 10px", borderRadius: 9, background: "var(--surface-muted)" }}>
                  <span style={{ width: 7, height: 7, borderRadius: "50%", background: binding.enabled ? "#4f9c84" : "#aeaeb2", flexShrink: 0 }} />
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

        <Field label={t("ui.workspace")}>
          <Select
            value={workspaceId}
            onChange={setWorkspaceId}
            placeholder={t("ui.entity_level_no_workspace")}
            options={(workspaces as { id: string; name: string }[]).map((w) => ({
              value: w.id,
              label: w.name,
            }))}
          />
        </Field>

        <Field label={t("ui.business_line")}>
          <Input
            placeholder="e.g. leasing, maintenance, billing (optional)"
            value={businessLine}
            onChange={(e) => setBusinessLine(e.target.value)}
          />
        </Field>

        <Field label={t("ui.trigger")}>
          <Select value={triggerType} onChange={setTriggerType} options={TRIGGER_OPTIONS} />
        </Field>

        {triggerType === "schedule" && (
          <Field label={t("ui.cron_schedule")}>
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
            {scheduleBlocked && (
              <span role="alert" style={{ fontSize: 12, color: "var(--danger, #c14a44)" }}>
                A schedule can't supply required inputs ({unscheduledInputs.map((input) => input.label).join(", ")}).
                Give them default values on the start node, or run this workflow manually or from a Workspace.
              </span>
            )}
          </Field>
        )}

        {needsEvent && (
          <Field label={t("ui.event_name")}>
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
          <Field label={t("ui.tool_description")}>
            <Input
              placeholder={t("ui.what_this_workflow_does_shown_to_the_agent")}
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
      <span style={{ fontSize: 11, fontWeight: 500, color: "var(--text-faint)" }}>
        {label}
      </span>
      {children}
    </label>
  );
}
