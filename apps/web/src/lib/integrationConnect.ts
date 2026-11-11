import { create } from "zustand";

/**
 * Connect an integration where the person is (a chat, a Workspace App) instead
 * of sending them to the Integrations page. One host (IntegrationConnectHost,
 * mounted beside the global DetailDrawer) renders the Integrations page's own
 * setup panel for the requested provider; this module is the shared entry.
 */

/** Dispatched on `window` with `{ provider }` when a provider becomes usable. */
export const INTEGRATION_CONNECTED_EVENT = "manor:integration-connected";
/** postMessage type the OAuth popup's landing page sends to its opener. */
export const OAUTH_POPUP_MESSAGE = "manor:integration-oauth";
/** Where the provider sends the popup back (see IntegrationOAuthDone). */
export const OAUTH_POPUP_RETURN_PATH = "/integrations/connected";

interface IntegrationConnectState {
  /** The provider being connected, while a connect session is open. */
  provider: string | null;
  /** Bumped per session so the host can tell two sessions for one provider apart. */
  session: number;
  /** The setup panel (drawer or form) is showing. */
  panelOpen: boolean;
  /** An OAuth popup is still out with the provider. */
  popupPending: boolean;
  open: (provider: string) => void;
  closePanel: () => void;
  setPopupPending: (pending: boolean) => void;
  end: () => void;
}

export const useIntegrationConnectStore = create<IntegrationConnectState>((set) => ({
  provider: null,
  session: 0,
  panelOpen: false,
  popupPending: false,
  open: (provider) => set((state) => ({ provider, session: state.session + 1, panelOpen: true, popupPending: false })),
  closePanel: () => set({ panelOpen: false }),
  setPopupPending: (popupPending) => set({ popupPending }),
  end: () => set({ provider: null, panelOpen: false, popupPending: false }),
}));

/** Open the provider's connect flow on the current page. */
export function openIntegrationConnect(provider: string): void {
  useIntegrationConnectStore.getState().open(provider);
}

function currentPath(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

let stopWatching: (() => void) | null = null;

/** Follow one popup until it reports back or is closed; either way the host re-checks the account. */
function watchPopup(popup: Window): void {
  stopWatching?.();
  const store = useIntegrationConnectStore.getState();
  store.setPopupPending(true);
  const finish = () => {
    window.removeEventListener("message", onMessage);
    window.clearInterval(timer);
    stopWatching = null;
    useIntegrationConnectStore.getState().setPopupPending(false);
  };
  const onMessage = (event: MessageEvent) => {
    if (event.origin !== window.location.origin || event.data?.type !== OAUTH_POPUP_MESSAGE) return;
    finish();
  };
  const timer = window.setInterval(() => {
    if (popup.closed) finish();
  }, 600);
  window.addEventListener("message", onMessage);
  stopWatching = finish;
}

/**
 * Open a Manor setup page (a Workspace's channels, an Agent's capabilities,
 * browser sessions) in a popup over this page, like OAuth: the person stays
 * where they are and `onClosed` runs once the popup closes, so the page can
 * look again. Returns false when the browser blocked the popup.
 */
export function openSetupInPopup(path: string, onClosed: () => void): boolean {
  const popup = window.open(path, "manor-integration-setup", "popup,width=960,height=760");
  if (!popup) return false;
  const timer = window.setInterval(() => {
    if (!popup.closed) return;
    window.clearInterval(timer);
    onClosed();
  }, 600);
  return true;
}

/**
 * Start a provider's OAuth in a popup that returns to this page's opener. A
 * blocked popup falls back to a full redirect that comes back to this same
 * page (the chat), never to the Integrations page.
 */
export async function startOAuthInPlace(serverKey: string, opts?: { connectionId?: string }): Promise<void> {
  // Opened synchronously, inside the click, so the browser allows it.
  const popup = window.open("", "manor-integration-oauth", "popup,width=520,height=720");
  // Mark the popup out before any await: the panel closes as it opens, and the
  // host must not end the connect session while the provider page is up.
  if (popup) useIntegrationConnectStore.getState().setPopupPending(true);
  try {
    // Loaded here so the light entry points (a chat link) do not pull in the API client.
    const { api } = await import("./api");
    const { authorize_url } = await api.integrations.oauthStart(serverKey, {
      connectionId: opts?.connectionId,
      returnTo: popup ? OAUTH_POPUP_RETURN_PATH : currentPath(),
    });
    if (!popup) {
      window.location.href = authorize_url;
      return;
    }
    popup.location.href = authorize_url;
    watchPopup(popup);
  } catch (error) {
    popup?.close();
    useIntegrationConnectStore.getState().setPopupPending(false);
    throw error;
  }
}
