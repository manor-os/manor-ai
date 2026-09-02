/** A setup link selects a catalog provider; it never starts authorization. */
export function integrationSetupHref(provider: string): string {
  return `/integrations?provider=${encodeURIComponent(provider)}`;
}

export function parseIntegrationSetupLink(href: string, origin: string): string | null {
  try {
    const url = new URL(href, origin);
    if (url.origin !== origin || url.username || url.password || url.pathname !== "/integrations") return null;
    const providers = url.searchParams.getAll("provider");
    const provider = providers[0] || "";
    return providers.length === 1 && /^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}$/.test(provider)
      ? provider
      : null;
  } catch {
    return null;
  }
}
