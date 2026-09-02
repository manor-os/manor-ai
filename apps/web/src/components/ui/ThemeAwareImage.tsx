import { useSyncExternalStore, type ImgHTMLAttributes } from "react";

type ThemeAwareImageProps = Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & {
  src: string;
  darkSrc?: string | null;
};

export type ResolvedDocumentTheme = "white" | "dark";

const themeListeners = new Set<() => void>();
let themeObserver: MutationObserver | null = null;

function currentDocumentTheme(): ResolvedDocumentTheme {
  if (typeof document === "undefined") return "white";
  return document.documentElement.dataset.theme === "dark" ? "dark" : "white";
}

function subscribeToDocumentTheme(listener: () => void) {
  if (typeof document === "undefined" || typeof MutationObserver === "undefined") {
    return () => undefined;
  }

  themeListeners.add(listener);
  if (!themeObserver) {
    themeObserver = new MutationObserver(() => {
      themeListeners.forEach((notify) => notify());
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
  }

  return () => {
    themeListeners.delete(listener);
    if (themeListeners.size === 0) {
      themeObserver?.disconnect();
      themeObserver = null;
    }
  };
}

export function useResolvedDocumentTheme(): ResolvedDocumentTheme {
  return useSyncExternalStore(
    subscribeToDocumentTheme,
    currentDocumentTheme,
    () => "white",
  );
}

/** Return the paired dark cover only for Manor's generated blueprint assets. */
export function generatedBlueprintDarkCoverUrl(src: string): string | null {
  const match = src.match(/^(.*\/assets\/blueprints\/generated\/)([^/?#]+)\.webp([?#].*)?$/);
  if (!match || match[2].endsWith("-dark")) return null;
  return `${match[1]}${match[2]}-dark.webp${match[3] ?? ""}`;
}

/** Render a single image request and follow Manor's resolved data-theme value. */
export default function ThemeAwareImage({ src, darkSrc, ...props }: ThemeAwareImageProps) {
  const theme = useResolvedDocumentTheme();

  return <img {...props} src={theme === "dark" && darkSrc ? darkSrc : src} />;
}
