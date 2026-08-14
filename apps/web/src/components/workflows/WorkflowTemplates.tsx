import { useQuery } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { IconCheckCircle, IconFlow } from "../icons";
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
      title="Add a Flow template"
      maxWidth="860px"
      bodyClassName="workflow-template-dialog-body"
    >
      <div className="workflow-template-intro">
        <IconFlow size={18} />
        <div>
          <strong>Templates install as independent Flows</strong>
          <span>
            Each template keeps its catalogue ID. Installation creates your own Workflow ID,
            which you can connect to any Workspace, Task, or Automation.
          </span>
        </div>
      </div>

      {templates.isLoading ? (
        <div className="workflow-template-state">
          <LoadingSpinner size={20} />
          <span>Loading verified templates…</span>
        </div>
      ) : templates.isError ? (
        <div className="workflow-template-state is-error">
          Templates could not be loaded. Try again.
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
                    <span>{template.node_count} nodes · v{template.version}</span>
                  </div>
                  <span className="workflow-template-verified" title="The Flow graph passes platform validation">
                    <IconCheckCircle size={13} />
                    Graph verified
                  </span>
                </div>

                <p>{template.description}</p>

                <div className="workflow-template-requirements" aria-label="Setup requirements">
                  {template.requirements.slice(0, 3).map((requirement) => (
                    <span key={requirement}>{requirement}</span>
                  ))}
                </div>

                <div className="workflow-template-card-footer">
                  <code title={template.id}>{template.id}</code>
                  <Button
                    size="sm"
                    variant={template.installed ? "outline" : "primary"}
                    loading={installing}
                    onClick={() => onPick(template)}
                  >
                    {template.installed ? "Open Flow" : "Install"}
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
