import { useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  chatRouteReferenceLabel,
  parseChatRouteReference,
  preserveReturnToInHistory,
} from "../lib/chatRouteReferences";
import { IconList } from "./icons";

export default function InlineTaskReferenceCard({
  reference,
  label,
  returnTo,
  compact = false,
}: {
  reference: string;
  label?: string;
  returnTo?: string;
  compact?: boolean;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const route = useMemo(() => parseChatRouteReference(reference), [reference]);
  const currentReturnTo = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const displayName = label?.trim() || (route ? chatRouteReferenceLabel(route) : "Open task");

  if (!route || route.kind !== "task") return null;

  return (
    <button
      type="button"
      className={`inline-file-reference-card inline-task-reference-card${compact ? " inline-file-reference-card--compact" : ""}`}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        preserveReturnToInHistory(currentReturnTo);
        navigate(route.path, { state: { returnTo: currentReturnTo, chatReturnTo: currentReturnTo } });
      }}
      title={route.path}
    >
      <span className="inline-file-reference-card__icon inline-task-reference-card__icon"><IconList size={12} /></span>
      <span className="inline-file-reference-card__name">{displayName}</span>
    </button>
  );
}
