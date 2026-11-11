import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import IconTile from "../ui/IconTile";
import LoadingSpinner from "../ui/LoadingSpinner";
import Modal from "../ui/Modal";
import { NodeIcon, TYPE_META } from "./WorkflowCanvas";

export interface WorkflowTemplate {
  id: string;
  key: string;
  name: string;
  description: string;
  icon: string;
  version: string;
  trigger_type: string;
  category: string;
  tags: string[];
  requirements: string[];
  source_blueprint_id: string;
  verification_status: "graph_verified";
  node_count: number;
  dependency_ids: string[];
  installed: boolean;
  installed_workflow_id?: string | null;
}

export default function WorkflowTemplates({
  open,
  onClose,
  onPick,
  installingId,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (template: WorkflowTemplate) => void;
  installingId?: string | null;
}) {
  const templates = useQuery({
    queryKey: ["workflow-templates"],
    queryFn: () => api.workflows.templates(),
    enabled: open,
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("component.workflow_templates.title")}
      maxWidth="860px"
      bodyClassName="workflow-template-dialog-body"
    >
      {templates.isLoading ? (
        <div className="workflow-template-state">
          <LoadingSpinner size={20} />
          <span className="sr-only">{t("component.workflow_templates.loading")}</span>
        </div>
      ) : templates.isError ? (
        <div className="workflow-template-state is-error">
          {t("component.workflow_templates.load_error")}
        </div>
      ) : (
        <div className="workflow-template-grid">
          {(templates.data || []).map((template: WorkflowTemplate) => {
            const meta = TYPE_META[template.icon] || { color: "var(--accent)" };
            const installing = installingId === template.id;
            return (
              <article className="workflow-template-card" key={template.id}>
                <div className="workflow-template-card-heading">
                  <IconTile color={meta.color} size={38}>
                    <NodeIcon type={template.icon} size={19} />
                  </IconTile>
                  <div>
                    <strong>{template.name}</strong>
                    <span>{t("component.workflow_templates.steps", { count: template.node_count })}</span>
                  </div>
                </div>

                <p>{template.description}</p>

                <div className="workflow-template-requirements" aria-label={t("ui.setup_requirements")}>
                  {template.requirements.slice(0, 3).map((requirement) => (
                    <span key={requirement}>{requirement}</span>
                  ))}
                </div>

                {/* A grid of choices has no single main action: every button is neutral. */}
                <div className="workflow-template-card-footer">
                  <Button
                    size="sm"
                    variant="outline"
                    loading={installing}
                    onClick={() => onPick(template)}
                  >
                    {template.installed ? t("component.workflow_templates.open") : t("component.workflow_templates.add")}
                  </Button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </Modal>
  );
}
