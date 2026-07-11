/* Minimal type declarations for Google Picker API + GIS used by google-drive.ts */

declare namespace google {
  namespace accounts.oauth2 {
    interface TokenClient {
      callback: (response: any) => void;
      requestAccessToken(opts?: { prompt?: string }): void;
    }
    function initTokenClient(config: {
      client_id: string;
      scope: string;
      callback: (response: any) => void;
      error_callback?: (error: any) => void;
    }): TokenClient;
    function revoke(token: string, cb: () => void): void;
  }

  namespace picker {
    const Action: { PICKED: string; CANCEL: string };
    const ViewId: Record<string, any>;
    const Feature: { MULTISELECT_ENABLED: string };

    class DocsView {
      constructor(viewId?: any);
      setMimeTypes(mimeTypes: string): this;
    }
    class PickerBuilder {
      addView(view: any): this;
      setOAuthToken(token: string): this;
      setDeveloperKey(key: string): this;
      setCallback(cb: (data: any) => void): this;
      setTitle(title: string): this;
      enableFeature(feature: string): this;
      build(): { setVisible(v: boolean): void };
    }
  }
}

interface Window {
  gapi: {
    load(api: string, cb: () => void): void;
    client: {
      load(url: string): Promise<void>;
      drive: {
        files: {
          get(params: Record<string, any>): Promise<{ result: any }>;
          list(params: Record<string, any>): Promise<{ result: any }>;
        };
      };
    };
  };
  google: typeof google;
}
