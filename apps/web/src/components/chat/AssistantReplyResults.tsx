import { useMemo, type ComponentProps } from "react";
import { createdChatResourceReferences } from "../../lib/chatResourceReferences";
import type { ToolCall, WorkflowResultReference } from "../../lib/chatStream";
import { ChatMessageReferenceStrip } from "../ChatMessageDisplay";
import CreatedResourceCard from "../CreatedResourceCard";
import SimulationArtifactGallery from "../SimulationArtifactGallery";
import WorkflowResultCard from "../WorkflowResultCard";

type ReferenceStripProps = ComponentProps<typeof ChatMessageReferenceStrip>;

/**
 * What a reply hands over, in one order on every chat surface: its files as
 * themselves, then quiet lines saying what was created and which run made it.
 */
export default function AssistantReplyResults({
  references,
  sites,
  align,
  onOpenReference,
  returnTo,
  toolCalls,
  workflowResult,
  simulationArtifacts,
}: Pick<ReferenceStripProps, "references" | "sites" | "align" | "onOpenReference" | "returnTo"> & {
  toolCalls?: ToolCall[];
  workflowResult?: WorkflowResultReference | null;
  simulationArtifacts?: ComponentProps<typeof SimulationArtifactGallery>["artifacts"] | null;
}) {
  const createdResources = useMemo(() => createdChatResourceReferences(toolCalls), [toolCalls]);
  return <>
    <ChatMessageReferenceStrip references={references} sites={sites} align={align}
      onOpenReference={onOpenReference} inlineFileCards returnTo={returnTo} />
    {simulationArtifacts?.length ? <SimulationArtifactGallery artifacts={simulationArtifacts} /> : null}
    {createdResources.map((resource) => <CreatedResourceCard key={`${resource.kind}:${resource.id}`}
      resource={resource} returnTo={returnTo} />)}
    {workflowResult ? <WorkflowResultCard result={workflowResult} returnTo={returnTo} /> : null}
  </>;
}
