import ChatMarkdown from "../../components/ChatMarkdown";
import { useNavigate } from "react-router-dom";
import AgentAvatar from "../../components/ui/AgentAvatar";
import Button from "../../components/ui/Button";
import Dropdown from "../../components/ui/Dropdown";
import SkillIcon from "../../components/ui/SkillIcon";
import { t } from "../../lib/i18n";
import { getSkillDescription, skillNeedsCredentials } from "./skillTypes";

/** The selected skill beside the library list: what it does, what it uses, who can do it. */
export function SkillPane({ skill, editable, onInvoke, onEdit, onDetails, onCredential, onDelete }: {
  skill: any;
  editable: boolean;
  onInvoke: (skill: any) => void;
  onEdit: (skill: any) => void;
  onDetails: (skill: any) => void;
  onCredential: (skill: any) => void;
  onDelete: (skillId: string) => void;
}) {
  const navigate = useNavigate();
  const description = getSkillDescription(skill);
  const tools: string[] = Array.isArray(skill.tools) ? skill.tools : [];
  const steps = String(skill.system_prompt || "").trim();
  const employees = new Map<string, any>();
  for (const binding of Array.isArray(skill.bindings) ? skill.bindings : []) {
    if (binding?.agent_id) employees.set(binding.agent_id, binding);
  }
  const moreItems = [
    { key: "details", label: t("page.skill_card.view_usage_and_examples") },
    ...(skillNeedsCredentials(skill) ? [{ key: "credentials", label: t("page.skill_card.configure_credentials") }] : []),
    ...(editable ? [{ key: "delete", label: t("action.delete"), danger: true }] : []),
  ];
  return (
    <div className="skill-pane">
      <div className="skill-pane-header">
        <SkillIcon skill={skill} size={40} />
        <h2>{skill.name}</h2>
        <div className="skill-pane-actions">
          <Button variant="outline" size="sm" onClick={() => onInvoke(skill)}>{t("page.skills.try_it")}</Button>
          {editable && <Button variant="outline" size="sm" onClick={() => onEdit(skill)}>{t("action.edit")}</Button>}
          <Dropdown
            align="right"
            trigger={<Button variant="ghost" size="sm" ariaLabel={t("nav.more")}>⋯</Button>}
            items={moreItems}
            onSelect={(key: string) => {
              if (key === "details") onDetails(skill);
              if (key === "credentials") onCredential(skill);
              if (key === "delete") onDelete(skill.id);
            }}
          />
        </div>
      </div>
      {description && <p className="skill-pane-description">{description}</p>}
      {steps && (
        <section>
          <h3>{t("page.skills.pane_steps")}</h3>
          <div className="skill-pane-steps"><ChatMarkdown content={steps} /></div>
        </section>
      )}
      {tools.length > 0 && (
        <section>
          <h3>{t("page.skills.pane_tools")}</h3>
          <p>{tools.join(" · ")}</p>
        </section>
      )}
      <section>
        <h3>{t("page.skills.pane_employees")}</h3>
        {employees.size === 0
          ? <p className="skill-pane-muted">{t("page.skills.pane_no_employees")}</p>
          : [...employees.entries()].map(([agentId, binding]) => (
            <button key={agentId} type="button" className="skill-pane-employee" onClick={() => navigate(`/settings?tab=agents&agent=${encodeURIComponent(agentId)}`)}>
              <AgentAvatar name={binding.agent_name || agentId} seed={agentId} size={24} />
              <span>{binding.agent_name || agentId}</span>
            </button>
          ))}
      </section>
    </div>
  );
}
