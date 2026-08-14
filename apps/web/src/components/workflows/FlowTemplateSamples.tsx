import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";

import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { IconFlow, IconSparkles } from "../icons";
import Button from "../ui/Button";
import EmptyState from "../ui/EmptyState";
import LoadingSpinner from "../ui/LoadingSpinner";
import Modal from "../ui/Modal";
import type { WorkflowTemplate } from "./WorkflowTemplates";
import "./FlowTemplateSamples.css";

type PreviewNodeKind =
  | "knowledge"
  | "tool"
  | "agent"
  | "approval"
  | "condition"
  | "image"
  | "video"
  | "end";

type PreviewNode = {
  id: string;
  label: string;
  kind: PreviewNodeKind;
  x: number;
  y: number;
};

type PreviewEdge = {
  from: string;
  to: string;
  branch?: "approved" | "changes";
};

type PreviewGraph = {
  nodes: PreviewNode[];
  edges: PreviewEdge[];
};

const NODE_WIDTH = 126;
const NODE_HEIGHT = 54;

const PREVIEW_GRAPHS: Record<string, PreviewGraph> = {
  "opc-generate-topic-from-knowledge-v1": {
    nodes: [
      { id: "knowledge", label: "Workspace knowledge", kind: "knowledge", x: 28, y: 118 },
      { id: "signals", label: "Public signals", kind: "tool", x: 178, y: 118 },
      { id: "draft", label: "Draft topic brief", kind: "agent", x: 328, y: 118 },
      { id: "review", label: "Approve topic", kind: "approval", x: 478, y: 118 },
      { id: "gate", label: "Topic approved?", kind: "condition", x: 628, y: 118 },
      { id: "approved", label: "Approved topic", kind: "end", x: 818, y: 54 },
      { id: "changes", label: "Revise or cancel", kind: "end", x: 818, y: 184 },
    ],
    edges: [
      { from: "knowledge", to: "signals" },
      { from: "signals", to: "draft" },
      { from: "draft", to: "review" },
      { from: "review", to: "gate" },
      { from: "gate", to: "approved", branch: "approved" },
      { from: "gate", to: "changes", branch: "changes" },
    ],
  },
  "opc-write-article-from-topic-v1": {
    nodes: [
      { id: "knowledge", label: "Article evidence", kind: "knowledge", x: 82, y: 118 },
      { id: "draft", label: "Draft article", kind: "agent", x: 262, y: 118 },
      { id: "review", label: "Editorial review", kind: "approval", x: 442, y: 118 },
      { id: "gate", label: "Article approved?", kind: "condition", x: 622, y: 118 },
      { id: "approved", label: "Approved article", kind: "end", x: 818, y: 54 },
      { id: "changes", label: "Revise or cancel", kind: "end", x: 818, y: 184 },
    ],
    edges: [
      { from: "knowledge", to: "draft" },
      { from: "draft", to: "review" },
      { from: "review", to: "gate" },
      { from: "gate", to: "approved", branch: "approved" },
      { from: "gate", to: "changes", branch: "changes" },
    ],
  },
  "opc-create-image-from-topic-v1": {
    nodes: [
      { id: "knowledge", label: "Visual knowledge", kind: "knowledge", x: 10, y: 54 },
      { id: "draft", label: "Draft image brief", kind: "agent", x: 150, y: 54 },
      { id: "brief_review", label: "Review brief", kind: "approval", x: 290, y: 54 },
      { id: "brief_gate", label: "Brief approved?", kind: "condition", x: 430, y: 54 },
      { id: "generate", label: "Generate image", kind: "image", x: 570, y: 54 },
      { id: "asset_review", label: "Review image", kind: "approval", x: 710, y: 54 },
      { id: "asset_gate", label: "Image approved?", kind: "condition", x: 850, y: 54 },
      { id: "approved", label: "Approved image", kind: "end", x: 990, y: 10 },
      { id: "asset_changes", label: "Revise image", kind: "end", x: 990, y: 118 },
      { id: "brief_changes", label: "Revise brief", kind: "end", x: 570, y: 190 },
    ],
    edges: [
      { from: "knowledge", to: "draft" },
      { from: "draft", to: "brief_review" },
      { from: "brief_review", to: "brief_gate" },
      { from: "brief_gate", to: "generate", branch: "approved" },
      { from: "brief_gate", to: "brief_changes", branch: "changes" },
      { from: "generate", to: "asset_review" },
      { from: "asset_review", to: "asset_gate" },
      { from: "asset_gate", to: "approved", branch: "approved" },
      { from: "asset_gate", to: "asset_changes", branch: "changes" },
    ],
  },
  "opc-create-video-from-topic-v1": {
    nodes: [
      { id: "knowledge", label: "Video knowledge", kind: "knowledge", x: 10, y: 54 },
      { id: "draft", label: "Draft video brief", kind: "agent", x: 150, y: 54 },
      { id: "brief_review", label: "Review brief", kind: "approval", x: 290, y: 54 },
      { id: "brief_gate", label: "Brief approved?", kind: "condition", x: 430, y: 54 },
      { id: "generate", label: "Generate video", kind: "video", x: 570, y: 54 },
      { id: "asset_review", label: "Review video", kind: "approval", x: 710, y: 54 },
      { id: "asset_gate", label: "Video approved?", kind: "condition", x: 850, y: 54 },
      { id: "approved", label: "Approved video", kind: "end", x: 990, y: 10 },
      { id: "asset_changes", label: "Revise video", kind: "end", x: 990, y: 118 },
      { id: "brief_changes", label: "Revise brief", kind: "end", x: 570, y: 190 },
    ],
    edges: [
      { from: "knowledge", to: "draft" },
      { from: "draft", to: "brief_review" },
      { from: "brief_review", to: "brief_gate" },
      { from: "brief_gate", to: "generate", branch: "approved" },
      { from: "brief_gate", to: "brief_changes", branch: "changes" },
      { from: "generate", to: "asset_review" },
      { from: "asset_review", to: "asset_gate" },
      { from: "asset_gate", to: "approved", branch: "approved" },
      { from: "asset_gate", to: "asset_changes", branch: "changes" },
    ],
  },
};

const FEATURED_TEMPLATE_KEYS = [
  "opc-generate-topic-from-knowledge-v1",
  "opc-write-article-from-topic-v1",
  "opc-create-image-from-topic-v1",
  "opc-create-video-from-topic-v1",
] as const;

const KIND_LABEL: Record<PreviewNodeKind, string> = {
  knowledge: "K",
  tool: "WEB",
  agent: "AI",
  approval: "H",
  condition: "IF",
  image: "IMG",
  video: "VID",
  end: "END",
};

function edgePath(source: PreviewNode, target: PreviewNode) {
  const sourceX = source.x + NODE_WIDTH;
  const sourceY = source.y + NODE_HEIGHT / 2;
  const targetX = target.x;
  const targetY = target.y + NODE_HEIGHT / 2;
  const bend = Math.max(34, (targetX - sourceX) * 0.46);
  return `M ${sourceX} ${sourceY} C ${sourceX + bend} ${sourceY}, ${targetX - bend} ${targetY}, ${targetX} ${targetY}`;
}

function FlowScreenshot({ template }: { template: WorkflowTemplate }) {
  const graph = PREVIEW_GRAPHS[template.key] || PREVIEW_GRAPHS[FEATURED_TEMPLATE_KEYS[1]];
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  const patternId = `flow-template-grid-${template.key.replace(/[^a-z0-9]/gi, "-")}`;

  return (
    <div
      className="flow-template-screenshot"
      role="img"
      aria-label={`${template.name} workflow`}
    >
      <svg viewBox="0 0 1130 636" preserveAspectRatio="xMidYMid meet">
        <defs>
          <pattern id={patternId} width="24" height="24" patternUnits="userSpaceOnUse">
            <circle cx="2" cy="2" r="1.15" className="flow-template-grid-dot" />
          </pattern>
        </defs>
        <rect width="1130" height="636" className="flow-template-canvas" />
        <rect width="1130" height="636" fill={`url(#${patternId})`} />
        <g transform="translate(0 168)">
          <g className="flow-template-edges">
            {graph.edges.map((edge) => {
              const source = nodesById.get(edge.from);
              const target = nodesById.get(edge.to);
              if (!source || !target) return null;
              return (
                <path
                  key={`${edge.from}-${edge.to}`}
                  d={edgePath(source, target)}
                  className={edge.branch ? `is-${edge.branch}` : undefined}
                />
              );
            })}
          </g>
          <g className="flow-template-nodes">
            {graph.nodes.map((node) => (
              <g
                key={node.id}
                className={`flow-template-node flow-template-node--${node.kind}`}
                transform={`translate(${node.x} ${node.y})`}
              >
                <rect width={NODE_WIDTH} height={NODE_HEIGHT} rx="12" className="flow-template-node-card" />
                <rect x="10" y="10" width="34" height="34" rx="10" className="flow-template-node-icon" />
                <text x="27" y="31" textAnchor="middle" className="flow-template-node-kind">
                  {KIND_LABEL[node.kind]}
                </text>
                <text x="52" y="31" className="flow-template-node-label">
                  {node.label}
                </text>
                <circle cx="0" cy="27" r="4" className="flow-template-node-handle" />
                <circle cx="126" cy="27" r="4" className="flow-template-node-handle" />
              </g>
            ))}
          </g>
        </g>
      </svg>
    </div>
  );
}

function featuredTemplates(templates: WorkflowTemplate[]) {
  const preferred = FEATURED_TEMPLATE_KEYS.flatMap((key) => {
    const template = templates.find((candidate) => candidate.key === key);
    return template ? [template] : [];
  });
  const preferredIds = new Set(preferred.map((template) => template.id));
  return [
    ...preferred,
    ...templates.filter((template) => !preferredIds.has(template.id)),
  ].slice(0, 4);
}

function TemplatePreview({
  template,
  onClose,
  onUse,
  installing,
  disabled,
  installError,
}: {
  template: WorkflowTemplate | null;
  onClose: () => void;
  onUse: (template: WorkflowTemplate) => void;
  installing: boolean;
  disabled: boolean;
  installError: boolean;
}) {
  const installedWorkflowId = template?.installed_workflow_id || "";
  const isInstalled = Boolean(template?.installed || installedWorkflowId);

  return (
    <Modal
      open={Boolean(template)}
      onClose={onClose}
      title={
        template
          ? `${t("component.embedded_chat.flow_templates.quick_preview")} · ${template.name}`
          : t("component.embedded_chat.flow_templates.quick_preview")
      }
      className="flow-template-preview-modal"
      bodyClassName="flow-template-preview-body"
      maxWidth="980px"
      footer={
        template ? (
          <div className="flow-template-preview-footer">
            {installError && (
              <p className="flow-template-install-error" role="alert">
                {t("component.embedded_chat.flow_templates.install_error")}
              </p>
            )}
            <Button
              size="sm"
              variant={isInstalled ? "outline" : "primary"}
              loading={installing}
              disabled={disabled || (isInstalled && !installedWorkflowId)}
              onClick={() => onUse(template)}
            >
              {t(
                isInstalled
                  ? "component.embedded_chat.flow_templates.open_flow"
                  : "component.embedded_chat.flow_templates.install_template",
              )}
            </Button>
          </div>
        ) : null
      }
    >
      {template && (
        <figure className="flow-template-preview-figure">
          <FlowScreenshot template={template} />
        </figure>
      )}
    </Modal>
  );
}

export default function FlowTemplateSamples() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [previewTemplate, setPreviewTemplate] = useState<WorkflowTemplate | null>(null);
  const templates = useQuery<WorkflowTemplate[]>({
    queryKey: ["workflow-templates"],
    queryFn: () => api.workflows.templates(),
  });
  const openFlow = useCallback(
    (workflowId: string) => {
      const returnTo = `${location.pathname}${location.search}${location.hash}`;
      navigate(`/flows?workflow=${encodeURIComponent(workflowId)}`, {
        state: { returnTo },
      });
    },
    [location.hash, location.pathname, location.search, navigate],
  );
  const installation = useMutation({
    mutationFn: (template: WorkflowTemplate) =>
      api.workflows.installTemplate(template.id),
    onSuccess: (result: any) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-templates"] });
      const workflowId = String(result?.workflow?.id || "").trim();
      if (workflowId) openFlow(workflowId);
    },
  });
  const visibleTemplates = featuredTemplates(templates.data || []);
  const useTemplate = (template: WorkflowTemplate) => {
    installation.reset();
    const installedWorkflowId = template.installed_workflow_id || "";
    if (installedWorkflowId) {
      openFlow(installedWorkflowId);
      return;
    }
    installation.mutate(template);
  };

  if (templates.isLoading) {
    return (
      <div className="flow-template-state" role="status">
        <LoadingSpinner size={20} />
        <span>{t("component.embedded_chat.flow_templates.loading")}</span>
      </div>
    );
  }

  if (templates.isError) {
    return (
      <div className="flow-template-state flow-template-state--error">
        <EmptyState
          icon={<IconFlow size={22} />}
          title={t("component.embedded_chat.flow_templates.load_error_title")}
          description={t("component.embedded_chat.flow_templates.load_error_description")}
          action={(
            <Button
              size="sm"
              variant="outline"
              loading={templates.isFetching}
              onClick={() => void templates.refetch()}
            >
              {t("component.embedded_chat.flow_templates.retry")}
            </Button>
          )}
        />
      </div>
    );
  }

  if (visibleTemplates.length === 0) {
    return (
      <div className="flow-template-state">
        <EmptyState
          icon={<IconFlow size={22} />}
          title={t("component.embedded_chat.flow_templates.empty_title")}
          description={t("component.embedded_chat.flow_templates.empty_description")}
        />
      </div>
    );
  }

  return (
    <>
      <div
        className="workspace-sample-grid flow-template-grid"
        aria-label={t("component.embedded_chat.flow_templates.gallery_label")}
      >
        {visibleTemplates.map((template) => {
          const installing =
            installation.isPending && installation.variables?.id === template.id;
          return (
            <article key={template.id} className="workspace-sample-card flow-template-card">
              <strong>{template.name}</strong>
              <FlowScreenshot template={template} />
              <div className="flow-template-actions">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    installation.reset();
                    setPreviewTemplate(template);
                  }}
                >
                  {t("component.embedded_chat.flow_templates.quick_preview")}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  loading={installing}
                  disabled={installation.isPending && !installing}
                  onClick={() => useTemplate(template)}
                >
                  <IconSparkles size={14} />
                  {t("component.embedded_chat.flow_templates.remix")}
                </Button>
              </div>
            </article>
          );
        })}
      </div>
      {installation.isError && !previewTemplate && (
        <p className="flow-template-install-error" role="alert">
          {t("component.embedded_chat.flow_templates.install_error")}
        </p>
      )}
      <TemplatePreview
        template={previewTemplate}
        onClose={() => {
          installation.reset();
          setPreviewTemplate(null);
        }}
        onUse={useTemplate}
        installing={
          installation.isPending &&
          installation.variables?.id === previewTemplate?.id
        }
        disabled={
          installation.isPending &&
          installation.variables?.id !== previewTemplate?.id
        }
        installError={installation.isError}
      />
    </>
  );
}
