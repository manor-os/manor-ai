import { useEffect, useState } from "react";

const STARTUP_AT = Date.now();

/**
 * True once the app has been up for `delayMs`. Secondary reads (badges, account
 * and connection status) gate on this so they do not compete with the data the
 * first screen needs; a few seconds of delay is invisible on them. Module-level
 * start time means remounts never re-delay.
 */
export function useAfterStartup(delayMs = 4_000): boolean {
  const remaining = () => Math.max(0, STARTUP_AT + delayMs - Date.now());
  const [ready, setReady] = useState(() => remaining() === 0);
  useEffect(() => {
    if (ready) return;
    const timer = setTimeout(() => setReady(true), remaining());
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, delayMs]);
  return ready;
}
