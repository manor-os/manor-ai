/**
 * generalAccessCopy — pure copy-selection logic for the ShareDialog
 * "General access" section.
 *
 * Why this exists: the dialog used to claim "Only people in the list above
 * can access" whenever no link share was active. That statement only
 * described *external link* sharing — actual internal access is governed by
 * the resource's `visibility` field (RFC docs/PERMISSIONS_DESIGN_ZH.md §13.1:
 * visibility and classification/sharing are orthogonal dimensions). On the
 * backend (packages/core/services/document_access.py::user_can_read_document)
 * `entity`, `public`, and NULL visibility all mean every entity member can
 * read the item — and find it via search/RAG. So for the common
 * entity-visible case the old copy was simply false.
 *
 * The section now always renders two truthful lines:
 *   1. internal — who inside the organization can access (from visibility)
 *   2. link     — the state of link sharing (from the share mode)
 *
 * Kept DOM-free so it can be unit-tested via
 * apps/web/scripts/share-dialog-general-access.test.mjs.
 */
import type { Visibility } from "../../lib/types";

export type GeneralAccessMode = "restricted" | "anyone_link" | "domain";

export type GeneralAccessTone = "neutral" | "warning" | "error";

export interface GeneralAccessLine {
  /** i18n key resolved by the caller via t(key, params). */
  key: string;
  params?: Record<string, string>;
  tone: GeneralAccessTone;
}

export interface GeneralAccessCopy {
  /** Who inside the organization can access — derived from visibility. */
  internal: GeneralAccessLine;
  /** State of link sharing — derived from mode/classification. */
  link: GeneralAccessLine;
}

/**
 * Mirror of the backend fallback in user_can_read_document:
 * `visibility or Visibility.ENTITY` — a missing/unknown value behaves as
 * entity-wide, so the UI must describe it that way too.
 */
export function effectiveVisibility(visibility?: string | null): Visibility {
  if (visibility === "private" || visibility === "workspace" || visibility === "public") {
    return visibility;
  }
  return "entity";
}

export function generalAccessCopy({
  mode,
  visibility,
  classification,
  entityDomain,
  needsApproval = false,
}: {
  mode: GeneralAccessMode;
  visibility?: string | null;
  classification?: string | null;
  entityDomain?: string;
  needsApproval?: boolean;
}): GeneralAccessCopy {
  const vis = effectiveVisibility(visibility);

  const internal: GeneralAccessLine =
    vis === "private"
      ? { key: "permissions.share.desc.internal_private", tone: "neutral" }
      : vis === "workspace"
        ? { key: "permissions.share.desc.internal_workspace", tone: "neutral" }
        : // entity + public + backend NULL-fallback: the whole organization
          // can read this item and retrieve it via search/AI. Warning tone —
          // this is the state users most often misread as "restricted".
          { key: "permissions.share.desc.internal_entity", tone: "warning" };

  let link: GeneralAccessLine;
  if (classification === "restricted") {
    // Restricted docs can never be link-shared (invariant RFC §13.14).
    link = { key: "permissions.share.desc.restricted_doc", tone: "error" };
  } else if (mode === "anyone_link") {
    link = {
      key: needsApproval
        ? "permissions.share.desc.anyone_link_approval"
        : "permissions.share.desc.anyone_link",
      tone: "neutral",
    };
  } else if (mode === "domain") {
    link = {
      key: needsApproval
        ? "permissions.share.desc.domain_approval"
        : "permissions.share.desc.domain",
      params: { domain: entityDomain ?? "" },
      tone: "neutral",
    };
  } else {
    link = { key: "permissions.share.desc.link_off", tone: "neutral" };
  }

  return { internal, link };
}
