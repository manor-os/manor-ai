import type { ChatMessage } from "../lib/chatStream";
import AssistantMessageBlocks from "./AssistantMessageBlocks";
import ChatMarkdown from "./ChatMarkdown";
import ToolCallList from "./ui/ToolCallList";

/**
 * One assistant reply drawn as a chat bubble draws it: its steps and its words
 * in order, live while it streams. For surfaces that show a reply outside the
 * chat, such as an App entry's result.
 */
export default function AssistantReply({ message, streaming, keyPrefix }: {
  message: ChatMessage;
  streaming: boolean;
  keyPrefix: string;
}) {
  const subAgentRuns = message.sub_agent_events || [];
  if (message.assistant_blocks?.length) {
    return <AssistantMessageBlocks blocks={message.assistant_blocks} content={message.content} keyPrefix={keyPrefix}
      streaming={streaming} minimal part="all" subAgentRuns={subAgentRuns}
      sourceMessageId={message.id || ""} hitlRequests={message.hitl_requests} />;
  }
  return <>
    {message.tool_calls?.length ? <ToolCallList tools={message.tool_calls} keyPrefix={keyPrefix} subAgentRuns={subAgentRuns}
      streaming={streaming} stepsSummary /> : null}
    {message.content ? <ChatMarkdown content={message.content} /> : null}
  </>;
}
