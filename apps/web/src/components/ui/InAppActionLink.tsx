import { routeLinkLabel } from "./ChatActionCard";

/**
 * Where a card sends the person to act (a host-owned in-app `action_link`).
 * A destination that can be shown live in chat is shown there (the cloud
 * computer's screen, an add-on's offer); any other is a link to its page.
 */
export default function InAppActionLink({ link, testId }: { link: string; testId?: string }) {
  const anchor = (
    <a className="chat-hitl-action-link" href={link} data-testid={testId}>
      {routeLinkLabel(link)}
    </a>
  );
  return anchor;
}
