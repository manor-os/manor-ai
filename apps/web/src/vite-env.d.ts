/// <reference types="vite/client" />

declare const __APP_VERSION__: string;

declare module "@novnc/novnc" {
  interface RFBCredentials {
    username?: string;
    password?: string;
    target?: string;
  }

  interface RFBOptions {
    credentials?: RFBCredentials;
    repeaterID?: string;
    shared?: boolean;
    wsProtocols?: string[];
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: RFBOptions);
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    background: string;
    qualityLevel: number;
    compressionLevel: number;
    showDotCursor: boolean;
    disconnect(): void;
  }
}
