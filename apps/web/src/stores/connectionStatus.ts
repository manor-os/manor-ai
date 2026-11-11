import { create } from "zustand";

/** Whether Manor's backend is reachable right now. Requests retry on their
 *  own, so this only drives the small dot on the sidebar avatar: never a toast. */
export const useConnectionStatus = create<{ reconnecting: boolean }>(() => ({
  reconnecting: false,
}));
