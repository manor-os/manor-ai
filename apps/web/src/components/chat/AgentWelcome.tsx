import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import AgentAvatar from "../ui/AgentAvatar";

const MAX_SKILLS = 4;

/* An empty chat with one AI employee is about that employee: who you are
 * talking to and what they already know how to do. */
export default function AgentWelcome({
  agentId,
  name,
  avatarUrl,
  onPrompt,
}: {
  agentId: string;
  name: string;
  avatarUrl?: string;
  onPrompt: (text: string) => void;
}) {
  const { data: skills } = useQuery({
    queryKey: ["agent-skills", agentId],
    queryFn: () => api.skills.listAgentBindings(agentId),
    staleTime: 60_000,
  });
  return (
    <div className="chat-empty-welcome agent-chat-welcome">
      <AgentAvatar name={name} avatarUrl={avatarUrl} seed={agentId} size={64} />
      <h1 className="chat-empty-welcome-title">{t("ui.agent_welcome_title", { name })}</h1>
      {(skills?.length ?? 0) > 0 && (
        <div className="manor-welcome-chips">
          {skills!.slice(0, MAX_SKILLS).map((skill: { id: string; name: string; display_name?: string | null; description?: string | null }) => (
            <button
              key={skill.id}
              type="button"
              className="manor-welcome-chip"
              title={skill.description || undefined}
              onClick={() => onPrompt(t("ui.agent_welcome_prompt", { skill: skill.display_name || skill.name }))}
            >
              {skill.display_name || skill.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
