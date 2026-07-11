import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { useToastStore } from "../stores/toast";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import StatusBadge from "../components/ui/StatusBadge";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import { IconPlus, IconChevronLeft, IconClose, IconFlow, IconClock, IconEdit, IconPlay, IconTrash } from "../components/icons";
import SmartToolbar from "../components/ui/SmartToolbar";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface FlowStep {
  id: string;
  type: "agent" | "tool" | "condition" | "wait" | "notify" | "transform";
  name: string;
  status?: "pending" | "running" | "done" | "error";
  config?: Record<string, unknown>;
}

interface Flow {
  id: string;
  name: string;
  description: string;
  trigger: "manual" | "event" | "schedule";
  status: "active" | "draft";
  steps: FlowStep[];
  last_run?: string;
  created_at: string;
}

/* ------------------------------------------------------------------ */
/*  Step type icons                                                    */
/* ------------------------------------------------------------------ */

const STEP_ICONS: Record<FlowStep["type"], React.ReactNode> = {
  agent: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
    </svg>
  ),
  tool: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17l-5.1 5.1a2.121 2.121 0 01-3-3l5.1-5.1m0 0L15.17 4.42a2.121 2.121 0 013 3l-7.75 7.75z" />
    </svg>
  ),
  condition: (
    <IconFlow size={16} />
  ),
  wait: (
    <IconClock size={16} />
  ),
  notify: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
    </svg>
  ),
  transform: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" />
    </svg>
  ),
};

const STEP_GRADIENTS: Record<FlowStep["type"], string> = {
  agent: "linear-gradient(135deg, #4f7d75, #436b65)",
  tool: "linear-gradient(135deg, #cf9b44, #b66a3c)",
  condition: "linear-gradient(135deg, #a07fc0, #c96a98)",
  wait: "linear-gradient(135deg, #5f84bd, #5a55a6)",
  notify: "linear-gradient(135deg, #4f9c84, #5f928a)",
  transform: "linear-gradient(135deg, #5e9098, #5f84bd)",
};

const TRIGGER_LABELS: Record<string, string> = {
  manual: "page.flows.trigger_manual",
  event: "page.flows.trigger_event",
  schedule: "page.flows.trigger_schedule",
};

const STATUS_DOT: Record<string, string> = {
  pending: "#a8a29e",
  running: "#cf9b44",
  done: "#4f9c84",
  error: "#d65f59",
};

const STEP_TYPES: FlowStep["type"][] = ["agent", "tool", "condition", "wait", "notify", "transform"];

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function Flows() {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const [search, setSearch] = useState("");
  const [selectedFlow, setSelectedFlow] = useState<Flow | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showStepPanel, setShowStepPanel] = useState(false);
  const [editingStep, setEditingStep] = useState<FlowStep | null>(null);
  const [showRunHistory, setShowRunHistory] = useState(false);
  const [hoveredCard, setHoveredCard] = useState<string | null>(null);
  const [hoveredAction, setHoveredAction] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  // Create form
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [formTrigger, setFormTrigger] = useState<"manual" | "event" | "schedule">("manual");

  // Add step form
  const [addStepType, setAddStepType] = useState<FlowStep["type"]>("agent");
  const [addStepName, setAddStepName] = useState("");
  const [showAddStep, setShowAddStep] = useState(false);
  const [_addStepIndex, setAddStepIndex] = useState(-1);

  const { data: flows, isLoading } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
  });

  const { data: runs } = useQuery({
    queryKey: ["workflow-runs", selectedFlow?.id],
    queryFn: () => (selectedFlow ? api.workflows.runs(selectedFlow.id) : Promise.resolve([])),
    enabled: !!selectedFlow,
  });

  const createMutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.workflows.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      setShowCreateModal(false);
      setFormName("");
      setFormDesc("");
      setFormTrigger("manual");
      toast.success(t("page.flows.toast_created"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.workflows.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      setSelectedFlow(null);
      toast.success(t("page.flows.toast_deleted"));
    },
  });

  const runMutation = useMutation({
    mutationFn: (id: string) => api.workflows.startRun(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] });
    },
  });

  const filtered = (flows || []).filter(
    (f: any) =>
      f.name?.toLowerCase().includes(search.toLowerCase()) ||
      f.description?.toLowerCase().includes(search.toLowerCase()),
  );

  const handleCreate = () => {
    if (!formName.trim()) return;
    createMutation.mutate({ name: formName, description: formDesc, trigger: formTrigger });
  };

  const openAddStep = (index: number) => {
    setAddStepIndex(index);
    setAddStepType("agent");
    setAddStepName("");
    setShowAddStep(true);
  };

  /* ---------------------------------------------------------------- */
  /*  Flow editor view                                                 */
  /* ---------------------------------------------------------------- */

  if (selectedFlow) {
    const flow = selectedFlow;
    const steps: FlowStep[] = flow.steps || [];

    return (
      <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "1rem", overflow: "hidden", position: "relative", zIndex: 10 }}>
        {/* Back + header */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24, flexShrink: 0 }}>
          <button
            onClick={() => setSelectedFlow(null)}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = "#f5f5f4"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "transparent"; }}
            style={{
              width: 34,
              height: 34,
              borderRadius: "50%",
              background: "transparent",
              border: "1px solid #f5f5f4",
              color: "#a8a29e",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              transition: "all 0.2s",
            }}
          >
            <IconChevronLeft size={16} />
          </button>
          <div style={{ flex: 1 }}>
            <h1 style={{ fontSize: 22, fontWeight: 900, color: "#1c1917", margin: "0 0 2px" }}>{flow.name}</h1>
            <p style={{ fontSize: 13, color: "#a8a29e", margin: 0, fontWeight: 500 }}>{flow.description}</p>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "2px 8px",
              borderRadius: 6,
              fontSize: 10,
              fontWeight: 600,
              background: flow.status === "active" ? "#f1f6f3" : "#fafaf9",
              color: flow.status === "active" ? "#437f6b" : "#a8a29e",
            }}>
              <span style={{
                width: 6, height: 6, borderRadius: "50%",
                background: flow.status === "active" ? "#4f9c84" : "#d6d3d1",
                boxShadow: flow.status === "active" ? "0 0 4px #4f9c84" : "none",
              }} />
              {flow.status === "active" ? t("page.flows.active") : t("page.flows.draft")}
            </span>
            <span style={{
              padding: "2px 8px",
              background: "#fafaf9",
              color: "#a8a29e",
              fontSize: 8,
              fontWeight: 800,
              textTransform: "uppercase" as const,
              letterSpacing: "0.1em",
              borderRadius: 6,
              border: "1px solid #f5f5f4",
            }}>
              {t(TRIGGER_LABELS[flow.trigger] || flow.trigger)}
            </span>
            <Button
              variant="primary"
              onClick={() => runMutation.mutate(flow.id)}
              disabled={runMutation.isPending}
              className="disabled:opacity-50"
            >
              {runMutation.isPending ? t("page.flows.starting") : t("page.flows.run")}
            </Button>
            <Button
              variant="outline"
              onClick={() => setShowRunHistory(!showRunHistory)}
            >
              {t("page.flows.history")}
            </Button>
          </div>
        </div>

        {/* Visual pipeline */}
        <div style={{
          background: "#fff",
          borderRadius: 32,
          border: "1px solid #f5f5f4",
          padding: 28,
          marginBottom: 24,
          overflowX: "auto",
          boxShadow: "0 4px 24px rgba(0,0,0,0.04)",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: "max-content" }}>
            {/* Start node */}
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
              <div style={{ width: 40, height: 40, borderRadius: "50%", background: "#f1f6f3", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <div style={{ width: 12, height: 12, borderRadius: "50%", background: "#4f9c84" }} />
              </div>
              <span style={{ fontSize: 11, color: "#a8a29e", marginTop: 4, fontWeight: 600 }}>{t("page.flows.start")}</span>
            </div>

            {steps.length === 0 && (
              <>
                <div style={{ width: 32, height: 2, background: "#f5f5f4" }} />
                <button
                  onClick={() => openAddStep(0)}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.borderColor = "#436b65"; (e.currentTarget as HTMLElement).style.color = "#436b65"; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.borderColor = "#e7e5e4"; (e.currentTarget as HTMLElement).style.color = "#a8a29e"; }}
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    border: "2px dashed rgba(28,25,23,0.06)",
                    background: "transparent",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "#a8a29e",
                    cursor: "pointer",
                    transition: "all 0.2s",
                  }}
                >
                  <IconPlus size={14} />
                </button>
              </>
            )}

            {steps.map((step, idx) => (
              <div key={step.id} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                {/* Arrow connector */}
                <div style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 32, height: 2, background: "#f5f5f4" }} />
                  <svg style={{ width: 10, height: 10, color: "#e7e5e4", marginLeft: -4 }} fill="currentColor" viewBox="0 0 12 12">
                    <path d="M4 0l8 6-8 6z" />
                  </svg>
                </div>

                {/* Step card */}
                <button
                  onClick={() => { setEditingStep(step); setShowStepPanel(true); }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = "var(--card-hover-border)";
                    e.currentTarget.style.background = "var(--card-hover-bg)";
                    e.currentTarget.style.boxShadow = "var(--card-hover-shadow)";
                    e.currentTarget.style.transform = "var(--card-hover-transform)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = "#f5f5f4";
                    e.currentTarget.style.background = "rgba(255,255,255,0.8)";
                    e.currentTarget.style.boxShadow = "0 2px 8px rgba(0,0,0,0.04)";
                    e.currentTarget.style.transform = "none";
                  }}
                  style={{
                    position: "relative",
                    background: "rgba(255,255,255,0.8)",
                    backdropFilter: "blur(8px)",
                    border: "1px solid #f5f5f4",
                    borderRadius: 16,
                    padding: 16,
                    width: 176,
                    textAlign: "left" as const,
                    cursor: "pointer",
                    transition: "all 0.25s",
                    boxShadow: "0 2px 8px rgba(0,0,0,0.04)",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                    <div style={{
                      width: 32,
                      height: 32,
                      borderRadius: 10,
                      background: STEP_GRADIENTS[step.type],
                      color: "#fff",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}>
                      {STEP_ICONS[step.type]}
                    </div>
                    <span style={{ fontSize: 8, color: "#a8a29e", textTransform: "uppercase" as const, letterSpacing: "0.1em", fontWeight: 800 }}>{step.type}</span>
                  </div>
                  <p style={{ fontSize: 13, fontWeight: 700, color: "#292524", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{step.name}</p>
                  {step.status && (
                    <span style={{
                      position: "absolute",
                      top: 10,
                      right: 10,
                      width: 8,
                      height: 8,
                      borderRadius: "50%",
                      background: STATUS_DOT[step.status] || "#a8a29e",
                      boxShadow: step.status === "running" ? `0 0 6px ${STATUS_DOT[step.status]}` : "none",
                    }} />
                  )}
                  <span style={{
                    position: "absolute",
                    top: -8,
                    left: -8,
                    width: 20,
                    height: 20,
                    borderRadius: "50%",
                    background: "#f5f5f4",
                    fontSize: 10,
                    fontWeight: 800,
                    color: "#78716c",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}>
                    {idx + 1}
                  </span>
                </button>

                {/* Add step button after each step */}
                <div style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 16, height: 2, background: "#f5f5f4" }} />
                  <button
                    onClick={() => openAddStep(idx + 1)}
                    onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.borderColor = "#436b65"; (e.currentTarget as HTMLElement).style.color = "#436b65"; }}
                    onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.borderColor = "#e7e5e4"; (e.currentTarget as HTMLElement).style.color = "#a8a29e"; }}
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: "50%",
                      border: "2px dashed rgba(28,25,23,0.06)",
                      background: "transparent",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      color: "#a8a29e",
                      cursor: "pointer",
                      transition: "all 0.2s",
                    }}
                  >
                    <IconPlus size={12} />
                  </button>
                </div>
              </div>
            ))}

            {/* End node */}
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <div style={{ width: 32, height: 2, background: "#f5f5f4" }} />
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <div style={{ width: 40, height: 40, borderRadius: "50%", background: "#f8f0ef", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <div style={{ width: 12, height: 12, borderRadius: "50%", background: "#d18b86" }} />
                </div>
                <span style={{ fontSize: 11, color: "#a8a29e", marginTop: 4, fontWeight: 600 }}>{t("page.flows.end")}</span>
              </div>
            </div>
          </div>
        </div>

        {/* Add step modal */}
        <Modal
          open={showAddStep}
          onClose={() => setShowAddStep(false)}
          title={t("page.flows.add_step")}
          footer={
            <>
              <Button variant="outline" onClick={() => setShowAddStep(false)}>
                {t("action.cancel")}
              </Button>
              <Button
                variant="primary"
                onClick={() => { setShowAddStep(false); }}
                disabled={!addStepName.trim()}
              >
                {t("page.flows.add_step")}
              </Button>
            </>
          }
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div>
              <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>{t("page.flows.step_type")}</label>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 8 }}>
                {STEP_TYPES.map((t) => (
                  <button
                    key={t}
                    onClick={() => setAddStepType(t)}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      gap: 6,
                      padding: 12,
                      borderRadius: 14,
                      border: addStepType === t ? "2px solid #436b65" : "1px solid #f5f5f4",
                      background: addStepType === t ? "#f5f5f4" : "#fff",
                      color: addStepType === t ? "#1c1917" : "#78716c",
                      fontSize: 11,
                      fontWeight: 700,
                      cursor: "pointer",
                      transition: "all 0.2s",
                      textTransform: "capitalize" as const,
                    }}
                  >
                    <div style={{
                      width: 32,
                      height: 32,
                      borderRadius: 10,
                      background: STEP_GRADIENTS[t],
                      color: "#fff",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}>
                      {STEP_ICONS[t]}
                    </div>
                    {t}
                  </button>
                ))}
              </div>
            </div>
            <Input
              label={t("page.flows.step_name")}
              value={addStepName}
              onChange={(e) => setAddStepName(e.target.value)}
              placeholder={t("page.flows.step_name_placeholder")}
            />
          </div>
        </Modal>

        {/* Step config slide-in panel */}
        {showStepPanel && editingStep && (
          <div
            onClick={() => setShowStepPanel(false)}
            style={{
              position: "fixed",
              inset: 0,
              zIndex: 50,
              display: "flex",
              justifyContent: "flex-end",
              background: "rgba(0,0,0,0.15)",
              backdropFilter: "blur(4px)",
            }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                width: "100%",
                maxWidth: 420,
                background: "#fff",
                boxShadow: "0 20px 60px rgba(0,0,0,0.15)",
                height: "100%",
                overflowY: "auto",
                padding: 28,
                borderRadius: "32px 0 0 32px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
                <h3 style={{ fontSize: 17, fontWeight: 900, color: "#1c1917", margin: 0 }}>{t("page.flows.step_config")}</h3>
                <button
                  onClick={() => setShowStepPanel(false)}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.color = "#1c1917"; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.color = "#a8a29e"; }}
                  style={{ background: "transparent", border: "none", cursor: "pointer", color: "#a8a29e", transition: "color 0.2s", padding: 4 }}
                >
                  <IconClose size={16} />
                </button>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <div style={{
                    width: 44,
                    height: 44,
                    borderRadius: 14,
                    background: STEP_GRADIENTS[editingStep.type],
                    color: "#fff",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}>
                    {STEP_ICONS[editingStep.type]}
                  </div>
                  <div>
                    <p style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0 }}>{editingStep.name}</p>
                    <p style={{ fontSize: 11, color: "#a8a29e", margin: 0, textTransform: "capitalize" as const, fontWeight: 600 }}>{editingStep.type}</p>
                  </div>
                </div>
                <Input
                  label={t("page.flows.name")}
                  value={editingStep.name}
                  onChange={() => {}}
                  disabled
                />
                <Textarea
                  label={t("page.flows.configuration")}
                  value={editingStep.config ? JSON.stringify(editingStep.config, null, 2) : ""}
                  onChange={() => {}}
                  placeholder={t("page.flows.key_value")}
                  rows={5}
                />
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 12, marginTop: 24 }}>
                <Button variant="outline" onClick={() => setShowStepPanel(false)}>
                  {t("page.flows.close")}
                </Button>
                <Button variant="primary">
                  {t("action.save")}
                </Button>
              </div>
            </div>
          </div>
        )}

        {/* Run history */}
        {showRunHistory && (
          <div style={{
            background: "#fff",
            borderRadius: 32,
            border: "1px solid #f5f5f4",
            padding: 28,
            boxShadow: "0 4px 24px rgba(0,0,0,0.04)",
          }}>
            <h3 style={{ fontSize: 14, fontWeight: 900, color: "#1c1917", marginBottom: 16, marginTop: 0 }}>{t("page.flows.run_history")}</h3>
            {(runs || []).length === 0 ? (
              <p style={{ fontSize: 13, color: "#a8a29e", fontWeight: 500, margin: 0 }}>{t("page.flows.no_runs")}</p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {(runs || []).map((run: any) => (
                  <div key={run.id} style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: 12,
                    background: "#fafaf9",
                    borderRadius: 14,
                    border: "1px solid #f5f5f4",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                      <StatusBadge
                        type={run.status === "completed" ? "success" : run.status === "failed" ? "danger" : "warning"}
                        dot
                      >
                        {run.status}
                      </StatusBadge>
                      <span style={{ fontSize: 11, color: "#a8a29e", fontWeight: 500 }}>{run.started_at}</span>
                    </div>
                    {run.finished_at && (
                      <span style={{ fontSize: 11, color: "#a8a29e", fontWeight: 500 }}>{t("page.flows.finished")}: {run.finished_at}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  /* ---------------------------------------------------------------- */
  /*  Flow list view                                                   */
  /* ---------------------------------------------------------------- */

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "1rem", overflow: "hidden", position: "relative", zIndex: 10 }}>
      {/* Header */}
      <PageHeader
        title={t("nav.flows")}
        subtitle={t("page.flows.subtitle")}
        toolbar={(
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.flows.search_placeholder")}
            className="w-full sm:w-[280px]"
          />
        )}
        actions={(
          <PageHeaderAddButton label={t("page.flows.add_flow")} onClick={() => setShowCreateModal(true)} />
        )}
      />

      {/* Grid */}
      <div style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
        {isLoading ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, color: "#a8a29e" }}>
              <LoadingSpinner size={20} />
              <span style={{ fontSize: 14 }}>{t("page.flows.loading")}</span>
            </div>
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={
              <IconFlow size={32} className="text-stone-300" />
            }
            title={t("page.flows.no_flows")}
            description={t("page.flows.no_flows_desc")}
          />
        ) : (
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))",
            gap: 24,
          }}>
            {/* Add New Flow Card */}
            <div
              onClick={() => setShowCreateModal(true)}
              onMouseEnter={() => setHoveredCard("add")}
              onMouseLeave={() => setHoveredCard(null)}
              style={{
                border: `3px dashed ${hoveredCard === "add" ? "rgba(28,25,23,0.3)" : "#f5f5f4"}`,
                borderRadius: 32,
                padding: 32,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                gap: 14,
                cursor: "pointer",
                transition: "all 0.25s",
                color: "#d6d3d1",
                minHeight: 260,
                background: hoveredCard === "add" ? "rgba(28,25,23,0.03)" : "transparent",
              }}
            >
              <div style={{
                width: 60,
                height: 60,
                background: "#fafaf9",
                borderRadius: "50%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: hoveredCard === "add" ? "#1c1917" : "#d6d3d1",
                transition: "all 0.25s",
                transform: hoveredCard === "add" ? "scale(1.08)" : "none",
              }}>
                <IconPlus size={28} />
              </div>
              <div style={{ textAlign: "center" }}>
                <p style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: "0 0 4px" }}>{t("page.flows.create_new_flow")}</p>
                <p style={{ fontSize: 12, color: "#a8a29e", fontWeight: 500, margin: 0 }}>{t("page.flows.automate_workflow")}</p>
              </div>
            </div>

            {/* Flow cards */}
            {filtered.map((flow: any) => {
              const isHovered = hoveredCard === flow.id;
              const stepCount = (flow.steps || []).length;
              return (
                <div
                  key={flow.id}
                  onMouseEnter={() => setHoveredCard(flow.id)}
                  onMouseLeave={() => setHoveredCard(null)}
                  style={{
                    padding: "28px 28px 20px",
                    borderRadius: 32,
                    border: isHovered ? "1px solid var(--card-hover-border)" : "1px solid #f5f5f4",
                    boxShadow: isHovered
                      ? "var(--card-hover-shadow)"
                      : "0 4px 24px rgba(0, 0, 0, 0.04)",
                    display: "flex",
                    flexDirection: "column",
                    transition: "all 0.25s",
                    textAlign: "left" as const,
                    background: isHovered ? "var(--card-hover-bg)" : "#fff",
                    transform: isHovered ? "var(--card-hover-transform)" : "none",
                    cursor: "pointer",
                  }}
                  onClick={() => setSelectedFlow(flow)}
                >
                  {/* Header: icon + status */}
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 18 }}>
                    <div style={{
                      width: 60,
                      height: 60,
                      background: "linear-gradient(135deg, #efedea, #f4f7fa)",
                      borderRadius: 20,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      boxShadow: "inset 0 2px 4px rgba(0,0,0,0.06)",
                      transition: "transform 0.25s",
                      transform: isHovered ? "scale(1.06)" : "none",
                    }}>
                      <IconFlow size={24} className="text-manor-700" />
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
                      <span style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 4,
                        padding: "2px 8px",
                        borderRadius: 6,
                        fontSize: 10,
                        fontWeight: 600,
                        background: flow.status === "active" ? "#f1f6f3" : "#fafaf9",
                        color: flow.status === "active" ? "#437f6b" : "#a8a29e",
                      }}>
                        <span style={{
                          width: 6, height: 6, borderRadius: "50%",
                          background: flow.status === "active" ? "#4f9c84" : "#d6d3d1",
                          boxShadow: flow.status === "active" ? "0 0 4px #4f9c84" : "none",
                        }} />
                        {flow.status === "active" ? t("page.flows.active") : t("page.flows.draft")}
                      </span>
                      {/* Step count badge */}
                      <span style={{
                        fontSize: 10,
                        fontWeight: 700,
                        padding: "2px 8px",
                        borderRadius: 6,
                        background: "#4f7d75",
                        color: "#fff",
                      }}>
                        {stepCount} {stepCount !== 1 ? t("page.flows.steps") : t("page.flows.step")}
                      </span>
                    </div>
                  </div>

                  {/* Name */}
                  <h3 style={{
                    fontSize: 17,
                    fontWeight: 900,
                    color: "#1c1917",
                    margin: "0 0 6px",
                    transition: "color 0.2s",
                  }}>
                    {flow.name}
                  </h3>

                  {/* Description */}
                  {flow.description && (
                    <p style={{
                      fontSize: 12,
                      color: "#a8a29e",
                      fontWeight: 500,
                      lineHeight: 1.6,
                      margin: "0 0 12px",
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical" as const,
                      overflow: "hidden",
                    }}>
                      {flow.description}
                    </p>
                  )}

                  {/* Trigger tag */}
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 16 }}>
                    <span style={{
                      padding: "2px 8px",
                      background: "#fafaf9",
                      color: "#a8a29e",
                      fontSize: 8,
                      fontWeight: 800,
                      textTransform: "uppercase" as const,
                      letterSpacing: "0.1em",
                      borderRadius: 6,
                      border: "1px solid #f5f5f4",
                    }}>
                      {t(TRIGGER_LABELS[flow.trigger] || flow.trigger)}
                    </span>
                    {flow.last_run && (
                      <span style={{
                        padding: "2px 8px",
                        background: "#fafaf9",
                        color: "#a8a29e",
                        fontSize: 8,
                        fontWeight: 800,
                        textTransform: "uppercase" as const,
                        letterSpacing: "0.1em",
                        borderRadius: 6,
                        border: "1px solid #f5f5f4",
                      }}>
                        {t("page.flows.last")}: {flow.last_run}
                      </span>
                    )}
                  </div>

                  {/* Actions */}
                  <div style={{ display: "flex", gap: 8, marginTop: "auto", paddingTop: 16, borderTop: "1px solid #fafaf9" }}>
                    {/* Edit button */}
                    <button
                      onMouseEnter={() => setHoveredAction(`edit-${flow.id}`)}
                      onMouseLeave={() => setHoveredAction(null)}
                      onClick={(e) => { e.stopPropagation(); setSelectedFlow(flow); }}
                      style={{
                        width: 34,
                        height: 34,
                        borderRadius: "50%",
                        background: hoveredAction === `edit-${flow.id}` ? "#fff" : "#fafaf9",
                        border: `1px solid ${hoveredAction === `edit-${flow.id}` ? "#4f7d75" : "#f5f5f4"}`,
                        color: hoveredAction === `edit-${flow.id}` ? "#4f7d75" : "#a8a29e",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        cursor: "pointer",
                        transition: "all 0.2s",
                        flexShrink: 0,
                        boxShadow: hoveredAction === `edit-${flow.id}` ? "0 0 0 3px rgba(79,125,117,0.2)" : "none",
                        transform: hoveredAction === `edit-${flow.id}` ? "scale(1.08)" : "none",
                      }}
                      title={t("action.edit")}
                    >
                      <IconEdit size={14} />
                    </button>
                    {/* Run button */}
                    <button
                      onMouseEnter={() => setHoveredAction(`run-${flow.id}`)}
                      onMouseLeave={() => setHoveredAction(null)}
                      onClick={(e) => { e.stopPropagation(); runMutation.mutate(flow.id); }}
                      disabled={runMutation.isPending}
                      style={{
                        width: 34,
                        height: 34,
                        borderRadius: "50%",
                        background: hoveredAction === `run-${flow.id}` ? "#fff" : "#fafaf9",
                        border: `1px solid ${hoveredAction === `run-${flow.id}` ? "#1c1917" : "#f5f5f4"}`,
                        color: hoveredAction === `run-${flow.id}` ? "#1c1917" : "#a8a29e",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        cursor: "pointer",
                        transition: "all 0.2s",
                        flexShrink: 0,
                        boxShadow: hoveredAction === `run-${flow.id}` ? "0 0 0 3px rgba(28,25,23,0.2)" : "none",
                        transform: hoveredAction === `run-${flow.id}` ? "scale(1.08)" : "none",
                      }}
                      title={t("page.flows.run")}
                    >
                      <IconPlay size={14} />
                    </button>
                    {/* Delete button */}
                    <button
                      onMouseEnter={() => setHoveredAction(`delete-${flow.id}`)}
                      onMouseLeave={() => setHoveredAction(null)}
                      onClick={(e) => { e.stopPropagation(); setDeleteTarget(flow.id); }}
                      disabled={deleteMutation.isPending}
                      style={{
                        width: 34,
                        height: 34,
                        borderRadius: "50%",
                        background: hoveredAction === `delete-${flow.id}` ? "#fff" : "#fafaf9",
                        border: `1px solid ${hoveredAction === `delete-${flow.id}` ? "#ddafac" : "#f5f5f4"}`,
                        color: hoveredAction === `delete-${flow.id}` ? "#d65f59" : "#a8a29e",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        cursor: "pointer",
                        transition: "all 0.2s",
                        flexShrink: 0,
                        boxShadow: hoveredAction === `delete-${flow.id}` ? "0 0 0 3px rgba(214,95,89,0.15)" : "none",
                        transform: hoveredAction === `delete-${flow.id}` ? "scale(1.08)" : "none",
                      }}
                      title={t("action.delete")}
                    >
                      <IconTrash size={14} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Delete confirm */}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}
        title={t("page.flows.delete_flow")}
        message={t("page.flows.delete_message")}
        confirmLabel={t("action.delete")}
        danger
      />

      {/* Create modal */}
      <Modal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title={t("page.flows.create_flow")}
        footer={
          <>
            <Button variant="outline" onClick={() => setShowCreateModal(false)}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={handleCreate}
              disabled={!formName.trim() || createMutation.isPending}
            >
              {createMutation.isPending ? t("page.flows.creating") : t("action.create")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Input
            label={t("page.flows.name")}
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            placeholder={t("page.flows.name_placeholder")}
          />
          <Textarea
            label={t("page.flows.description")}
            value={formDesc}
            onChange={(e) => setFormDesc(e.target.value)}
            placeholder={t("page.flows.description_placeholder")}
            rows={3}
          />
          <div>
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>{t("page.flows.trigger_type")}</label>
            <select
              value={formTrigger}
              onChange={(e) => setFormTrigger(e.target.value as any)}
              className="manor-input"
            >
              <option value="manual">{t("page.flows.trigger_manual_option")}</option>
              <option value="event">{t("page.flows.trigger_event_option")}</option>
              <option value="schedule">{t("page.flows.trigger_schedule_option")}</option>
            </select>
          </div>
        </div>
      </Modal>
    </div>
  );
}
