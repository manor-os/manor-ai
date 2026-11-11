import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { AI_EDIT_DELETE_QUEUE_PREFIX, EDITOR_LIVE_CHAT_EVENT } from "../lib/editorLiveChat";
import { OPEN_FLOATING_CHAT_EVENT } from "../lib/selectionActions";

const FloatingChat = lazy(() => import("./FloatingChat"));

const WAKE_EVENTS = [OPEN_FLOATING_CHAT_EVENT, EDITOR_LIVE_CHAT_EVENT];

function hasQueuedAiEditDeletes(): boolean {
  try {
    return Object.keys(window.sessionStorage).some((key) => key.startsWith(AI_EDIT_DELETE_QUEUE_PREFIX));
  } catch {
    return false;
  }
}

/** Runs after FloatingChat's own effects, so its listeners hear what was missed. */
function ReplayMissedEvents({ onReplay }: { onReplay: () => void }) {
  useEffect(onReplay, [onReplay]);
  return null;
}

/**
 * The floating chat is closed on almost every visit, and closed it only waits
 * for these events. Its code loads when something first asks for it.
 */
export default function FloatingChatHost({ showLauncher }: { showLauncher: boolean }) {
  const [wanted, setWanted] = useState(() => showLauncher || hasQueuedAiEditDeletes());
  const missedRef = useRef<CustomEvent[]>([]);
  const listeningRef = useRef(true);

  useEffect(() => {
    if (showLauncher) setWanted(true);
  }, [showLauncher]);

  useEffect(() => {
    const remember = (event: Event) => {
      if (!listeningRef.current) return;
      missedRef.current.push(event as CustomEvent);
      setWanted(true);
    };
    for (const type of WAKE_EVENTS) window.addEventListener(type, remember);
    return () => {
      for (const type of WAKE_EVENTS) window.removeEventListener(type, remember);
    };
  }, []);

  const replayRef = useRef(() => {
    listeningRef.current = false;
    for (const event of missedRef.current.splice(0)) {
      window.dispatchEvent(new CustomEvent(event.type, { detail: event.detail }));
    }
  });

  if (!wanted) return null;
  return (
    <Suspense fallback={null}>
      <FloatingChat showLauncher={showLauncher} />
      <ReplayMissedEvents onReplay={replayRef.current} />
    </Suspense>
  );
}
