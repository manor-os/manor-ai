// Compatibility API for connector metadata; all presentation data is shared.
import { resolveIntegrationBrand } from "./brands/catalog";
export { normalizeBrandKey } from "./brands/catalog";

export function resolveConnectorBrand(raw?: string | null) {
  return resolveIntegrationBrand(raw) || { key: "", color: "", icon: null };
}
