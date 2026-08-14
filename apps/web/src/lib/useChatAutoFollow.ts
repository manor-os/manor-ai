import { useCallback, useRef } from "react";
import type { UIEvent } from "react";

/**
 * Tracks whether a chat viewport should keep following the newest message.
 * Following stops as soon as the user scrolls away from the bottom and
 * resumes when they scroll back down; callers should also force it back on
 * for user-initiated jumps (sending a message, switching conversations).
 *
 * Kept as a ref so scroll tracking never re-renders the chat.
 */
export function useChatAutoFollow(thresholdPx = 60) {
  const autoFollowRef = useRef(true);

  const handleAutoFollowScroll = useCallback(
    (event: UIEvent<HTMLElement>) => {
      const el = event.currentTarget;
      autoFollowRef.current =
        el.scrollHeight - el.scrollTop - el.clientHeight < thresholdPx;
    },
    [thresholdPx],
  );

  return { autoFollowRef, handleAutoFollowScroll };
}
