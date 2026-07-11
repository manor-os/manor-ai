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
let tokenClient: google.accounts.oauth2.TokenClient | null = null;
let accessToken: string | null = null;

// ---------------------------------------------------------------------------
// Script loaders
// ---------------------------------------------------------------------------

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) {
      resolve();
      return;
    }
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = reject;
    document.head.appendChild(s);
  });
}

async function initGapi() {
  if (gapiInited) return;
  await loadScript("https://apis.google.com/js/api.js");
  await new Promise<void>((resolve) => {
    window.gapi.load("client:picker", async () => {
      await window.gapi.client.load(
        "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest",
      );
      gapiInited = true;
      resolve();
    });
  });
}

async function initGis() {
  if (gisInited) return;
  await loadScript("https://accounts.google.com/gsi/client");
  gisInited = true;
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
