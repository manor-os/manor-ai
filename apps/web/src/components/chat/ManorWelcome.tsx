import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useQueries } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type WorkspaceAgentMapping } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { Workspace } from "../../lib/types";
import AgentAvatar from "../ui/AgentAvatar";
import { useConfigStore } from "../../stores/config";

/* The empty Manor conversation says one thing in large type: "Hand it to
 * ___." The blank cycles through your Workspaces; above it each Workspace's
 * AI employees wait in a little group, and the group being named hops. A new
 * account cycles through example Workspaces. */

const MAX_WORKSPACES = 4;
const FACES_PER_WORKSPACE = 2;
const CYCLE_MS = 3200;
const EXAMPLE_WORKSPACES = ["example_ws_1", "example_ws_2", "example_ws_3"] as const;

interface Face {
  key: string;
  name: string;
  seed: string;
  avatarUrl?: string | null;
}

interface Crew {
  key: string;
  name: string;
  faces: Face[];
}

function prefersReducedMotion() {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** A Workspace without mapped AI employees still shows its own two faces. */
function placeholderFaces(key: string, name: string): Face[] {
  return Array.from({ length: FACES_PER_WORKSPACE }, (_, i) => ({ key: `${key}:${i}`, name, seed: `${key}:${i}` }));
}

export default function ManorWelcome({
  firstRun,
  workspaces,
  onPrompt,
  moreOpen,
  onToggleMore,
}: {
  firstRun: boolean;
  workspaces: Workspace[];
  onPrompt: (text: string) => void;
  moreOpen: boolean;
  onToggleMore: () => void;
}) {
  let showMarketplace = false;
  const shownWorkspaces = useMemo(
    () => workspaces.filter((workspace) => workspace.status !== "archived").slice(0, MAX_WORKSPACES),
    [workspaces],
  );
  const mappingQueries = useQueries({
    queries: shownWorkspaces.map((workspace) => ({
      queryKey: ["workspace-agents", workspace.id],
      queryFn: () => api.workspaces.agents.list(workspace.id),
      staleTime: 60_000,
    })),
  });
  const mappings = mappingQueries.map((query) => (query.data || []) as WorkspaceAgentMapping[]);
  const mappingsKey = mappings.map((list) => list.map((m) => m.agent_id).join(",")).join("|");

  const crews = useMemo<Crew[]>(() => {
    if (!shownWorkspaces.length) {
      return EXAMPLE_WORKSPACES.map((key) => {
        const name = t(`component.manor_welcome.${key}`);
        return { key, name, faces: placeholderFaces(key, name) };
      });
    }
    return shownWorkspaces.map((workspace, index) => {
      const agents = (mappings[index] || [])
        .filter((mapping) => mapping.agent)
        .slice(0, FACES_PER_WORKSPACE)
        .map((mapping) => ({
          key: `${workspace.id}:${mapping.agent_id}`,
          name: mapping.agent!.name,
          seed: mapping.agent!.avatar_seed || mapping.agent!.id,
          avatarUrl: mapping.agent!.avatar_url,
        }));
      return {
        key: workspace.id,
        name: workspace.name,
        faces: agents.length ? agents : placeholderFaces(workspace.id, workspace.name),
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownWorkspaces, mappingsKey]);

  // `beat` counts every turn so the named crew hops again each time.
  const [beat, setBeat] = useState(0);
  useEffect(() => {
    setBeat(0);
    if (!crews.length || prefersReducedMotion()) return;
    const timer = window.setInterval(() => setBeat((value) => value + 1), CYCLE_MS);
    return () => window.clearInterval(timer);
  }, [crews]);
  const active = crews.length ? beat % crews.length : 0;
  const current = crews[active];

  // Each chip fills the composer with a sentence the person can finish.
  const chips = (firstRun
    ? ["food", "ecommerce", "creator", "local"]
    : ["progress", "report", "find_workspace"]
  ).map((key) => ({
    key,
    label: t(`component.manor_welcome.chip_${key}`),
    prompt: t(`component.manor_welcome.prompt_${key}`),
  }));

  return (
    <div className="manor-welcome">
      <div className="manor-welcome-crews" aria-hidden="true">
        {crews.map((crew, crewIndex) => {
          const on = crewIndex === active;
          return (
            <span
              key={on ? `${crew.key}:${beat}` : crew.key}
              className={`manor-welcome-crew ${on ? "is-active" : ""}`}
            >
              {crew.faces.map((face, faceIndex) => (
                <span
                  key={face.key}
                  className="manor-welcome-mochi"
                  style={{ "--i": faceIndex, "--bob": `${(crewIndex * 2 + faceIndex) * 0.37}s` } as CSSProperties}
                >
                  <AgentAvatar name={face.name} avatarUrl={face.avatarUrl} seed={face.seed} size={on ? 44 : 36} />
                </span>
              ))}
            </span>
          );
        })}
      </div>

      <h1 className="manor-welcome-title">
        <span>{t("component.manor_welcome.handoff_before")}</span>
        {/* "Hand it to X." has no space before the full stop; "交给 X 去办" does. */}
        <span className={`manor-welcome-slot${/^[.!?,;:。！？，]/.test(t("component.manor_welcome.handoff_after")) ? " manor-welcome-slot--end" : ""}`}>
          {/* A single Workspace keeps its name still; only its crew hops. */}
          <span key={`${current?.key}:${crews.length > 1 ? beat : 0}`} className="manor-welcome-slot-name">{current?.name}</span>
        </span>
        <span>{t("component.manor_welcome.handoff_after")}</span>
      </h1>
      <p className="manor-welcome-subtitle">
        {t(firstRun ? "component.manor_welcome.subtitle_first" : "component.manor_welcome.subtitle_returning")}
      </p>

      {firstRun && <p className="manor-welcome-question">{t("component.manor_welcome.question_first")}</p>}

      <div className="manor-welcome-chips">
        {chips.map((chip) => (
          <button key={chip.key} type="button" className="manor-welcome-chip" onClick={() => onPrompt(chip.prompt)}>
            {chip.label}
          </button>
        ))}
      </div>

      <div className="manor-welcome-links">
        {/* The marketplace is a cloud place. */}
        {showMarketplace && <>
          <Link to="/workspaces?view=marketplace" className="manor-welcome-link">
            {t("component.manor_welcome.browse_marketplace")}
          </Link>
          <span aria-hidden="true">·</span>
        </>}
        <button type="button" className="manor-welcome-link" aria-expanded={moreOpen} onClick={onToggleMore}>
          {t(moreOpen ? "component.manor_welcome.less" : "component.manor_welcome.more")}
        </button>
      </div>
    </div>
  );
}
