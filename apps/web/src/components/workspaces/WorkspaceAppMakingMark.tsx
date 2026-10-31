import type { CSSProperties } from "react";
import { t } from "../../lib/i18n";
import { titleHues } from "./workspaceAppRunDisplay";

/** The middle of a card being made: how far it is, or a pulse until that is known. */
export default function MakingMark({ percent }: { percent?: number }) {
  if (typeof percent !== "number") return <span className="workspace-app-data__making-dot" aria-hidden="true" />;
  return <span className="workspace-app-data__making-percent mono" role="progressbar" aria-valuemin={0} aria-valuemax={100}
    aria-valuenow={percent} aria-label={t("component.workspace_app_view.data_making_progress", { percent })}
    style={{ "--making-progress": `${percent * 3.6}deg` } as CSSProperties}>
    {percent}%
  </span>;
}

/** The square shown while something is being made: its colours drift and its progress ring sits in the middle. */
export function MakingCover({ title, percent }: { title: string; percent?: number }) {
  const [hue, accent] = titleHues(title);
  return <span className="workspace-app-data__gallery-tile" data-kind="pending" data-making
    style={{ "--making-hue": hue, "--making-accent": accent } as CSSProperties}>
    <MakingMark percent={percent} />
  </span>;
}
