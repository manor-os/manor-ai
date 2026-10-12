/**
 * Google Drive Picker integration.
 *
 * Uses Google Picker API + Google Identity Services (GIS) to let users
 * select files from their Drive.  The access token is forwarded to the
 * backend so it can download the file server-side.
 */

const GOOGLE_CLIENT_ID =
  import.meta.env.VITE_GOOGLE_CLIENT_ID || (window as any).__MANOR_ENV__?.VITE_GOOGLE_CLIENT_ID || "";
const GOOGLE_API_KEY =
  import.meta.env.VITE_GOOGLE_DRIVE_API_KEY || (window as any).__MANOR_ENV__?.VITE_GOOGLE_DRIVE_API_KEY || "";

const SCOPES = [
  "https://www.googleapis.com/auth/drive.readonly",
  "https://www.googleapis.com/auth/drive.file",
].join(" ");

let gapiInited = false;
let gisInited = false;
let gapiInitPromise: Promise<void> | null = null;
let gisInitPromise: Promise<void> | null = null;
let tokenClient: google.accounts.oauth2.TokenClient | null = null;
let accessToken: string | null = null;

const GOOGLE_API_SCRIPT = "https://apis.google.com/js/api.js";
const GOOGLE_IDENTITY_SCRIPT = "https://accounts.google.com/gsi/client";
const GOOGLE_SCRIPT_TIMEOUT_MS = 15_000;
const scriptLoadPromises = new Map<string, Promise<void>>();

class GoogleDriveInitializationError extends Error {
  constructor() {
    super("Google Drive could not start. Check your network or content blocker, then try again.");
    this.name = "GoogleDriveInitializationError";
  }
}

// ---------------------------------------------------------------------------
// Script loaders
// ---------------------------------------------------------------------------

function loadScript(src: string, isReady: () => boolean): Promise<void> {
  if (isReady()) return Promise.resolve();

  const pending = scriptLoadPromises.get(src);
  if (pending) return pending;

  let trackedPromise: Promise<void>;
  const loadPromise = new Promise<void>((resolve, reject) => {
    let script = document.querySelector<HTMLScriptElement>(`script[src="${src}"]`);
    const created = !script;
    script ||= document.createElement("script");

    let settled = false;
    let timeoutId: number | undefined;
    const cleanup = () => {
      script.removeEventListener("load", handleLoad);
      script.removeEventListener("error", handleError);
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
    const finish = (error?: GoogleDriveInitializationError) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) {
        script.remove();
        reject(error);
        return;
      }
      script.dataset.manorGoogleDriveLoaded = "true";
      resolve();
    };
    const verifyReady = () => {
      finish(isReady() ? undefined : new GoogleDriveInitializationError());
    };
    function handleLoad() {
      verifyReady();
    }
    function handleError() {
      finish(new GoogleDriveInitializationError());
    }

    script.addEventListener("load", handleLoad, { once: true });
    script.addEventListener("error", handleError, { once: true });
    timeoutId = window.setTimeout(() => {
      finish(isReady() ? undefined : new GoogleDriveInitializationError());
    }, GOOGLE_SCRIPT_TIMEOUT_MS);

    if (created) {
      script.src = src;
      script.async = true;
      document.head.appendChild(script);
    } else if (script.dataset.manorGoogleDriveLoaded === "true" || isReady()) {
      verifyReady();
    }
  });

  trackedPromise = loadPromise.catch((error) => {
    if (scriptLoadPromises.get(src) === trackedPromise) {
      scriptLoadPromises.delete(src);
    }
    throw error;
  });
  scriptLoadPromises.set(src, trackedPromise);
  return trackedPromise;
}

async function initGapi() {
  if (gapiInited) return;
  if (!gapiInitPromise) {
    gapiInitPromise = (async () => {
      await loadScript(GOOGLE_API_SCRIPT, () => typeof window.gapi?.load === "function");
      await new Promise<void>((resolve, reject) => {
        const fail = () => reject(new GoogleDriveInitializationError());
        try {
          window.gapi.load("client:picker", {
            callback: () => {
              window.gapi.client.load(
                "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest",
              ).then(() => {
                if (!window.google?.picker) {
                  fail();
                  return;
                }
                resolve();
              }, fail);
            },
            onerror: fail,
            timeout: GOOGLE_SCRIPT_TIMEOUT_MS,
            ontimeout: fail,
          });
        } catch {
          fail();
        }
      });
      gapiInited = true;
    })().catch((error) => {
      gapiInitPromise = null;
      throw error;
    });
  }
  await gapiInitPromise;
}

async function initGis() {
  if (gisInited) return;
  if (!gisInitPromise) {
    gisInitPromise = loadScript(
      GOOGLE_IDENTITY_SCRIPT,
      () => typeof window.google?.accounts?.oauth2?.initTokenClient === "function",
    ).then(() => {
      gisInited = true;
    }).catch((error) => {
      gisInitPromise = null;
      throw error;
    });
  }
  await gisInitPromise;
}

// ---------------------------------------------------------------------------
// Token handling
// ---------------------------------------------------------------------------

function requestAccessToken(): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!tokenClient) {
      tokenClient = window.google.accounts.oauth2.initTokenClient({
        client_id: GOOGLE_CLIENT_ID,
        scope: SCOPES,
        callback: () => {},
        error_callback: (err: any) => reject(err),
      });
    }

    tokenClient!.callback = (resp: any) => {
      if (resp.error) {
        reject(resp);
        return;
      }
      accessToken = resp.access_token;
      resolve(accessToken!);
    };
    (tokenClient as any).error_callback = (err: any) => reject(err);

    tokenClient!.requestAccessToken({
      prompt: accessToken ? "" : "consent",
    });
  });
}

// ---------------------------------------------------------------------------
// File details
// ---------------------------------------------------------------------------

async function getFileDetails(fileId: string) {
  const resp = await window.gapi.client.drive.files.get({
    fileId,
    fields:
      "id, name, mimeType, size, modifiedTime, webContentLink",
  });
  return resp.result as {
    id: string;
    name: string;
    mimeType: string;
    size?: string;
    modifiedTime?: string;
    webContentLink?: string;
  };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export interface GoogleDriveFile {
  id: string;
  name: string;
  mimeType: string;
  size: number;
  modifiedTime?: string;
  downloadUrl?: string;
  accessToken: string;
}

export function isConfigured(): boolean {
  return (
    !!GOOGLE_CLIENT_ID &&
    GOOGLE_CLIENT_ID !== "YOUR_CLIENT_ID" &&
    !!GOOGLE_API_KEY &&
    GOOGLE_API_KEY !== "YOUR_API_KEY"
  );
}

/**
 * Open the Google Drive file picker.
 * Returns null if the user cancels or auth fails.
 */
export async function pickFile(): Promise<GoogleDriveFile | null> {
  await Promise.all([initGapi(), initGis()]);

  try {
    await requestAccessToken();
  } catch {
    return null;
  }

  return new Promise((resolve) => {
    const view = new window.google.picker.DocsView(
      window.google.picker.ViewId.DOCS,
    );
    view.setMimeTypes([
      "application/pdf",
      "application/vnd.google-apps.document",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "application/msword",
      "text/plain",
      "text/csv",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "application/vnd.ms-excel",
      "application/vnd.google-apps.spreadsheet",
    ].join(","));

    const picker = new window.google.picker.PickerBuilder()
      .addView(view)
      .setOAuthToken(accessToken!)
      .setDeveloperKey(GOOGLE_API_KEY)
      .setCallback(async (data: any) => {
        if (data.action === window.google.picker.Action.PICKED) {
          const doc = data.docs[0];
          try {
            const details = await getFileDetails(doc.id);
            resolve({
              id: details.id,
              name: details.name,
              mimeType: details.mimeType,
              size: Number(details.size) || 0,
              modifiedTime: details.modifiedTime,
              downloadUrl: details.webContentLink || undefined,
              accessToken: accessToken!,
            });
          } catch {
            resolve({
              id: doc.id,
              name: doc.name,
              mimeType: doc.mimeType,
              size: doc.sizeBytes || 0,
              accessToken: accessToken!,
            });
          }
        } else if (data.action === window.google.picker.Action.CANCEL) {
          resolve(null);
        }
      })
      .setTitle("Select a file from Google Drive")
      .build();
    picker.setVisible(true);
  });
}

/**
 * Get current access token (after a successful pick).
 */
export function getToken(): string | null {
  return accessToken;
}

/**
 * Revoke access token.
 */
export function signOut() {
  if (accessToken) {
    window.google.accounts.oauth2.revoke(accessToken, () => {});
    accessToken = null;
  }
}
