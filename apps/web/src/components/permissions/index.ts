/**
 * Permission-v1 global elements. Always import via this barrel so pages
 * pick up future additions automatically.
 *
 *   import { WatermarkLayer, PermissionBanner } from "../components/permissions";
 *
 * ─── Where to use what ───────────────────────────────────────────────────
 *
 * Cloud-drive principle: file lists and viewer headers stay clean.
 * Permission state surfaces in three places only:
 *
 *   1. <ShareDialog>             — Share / "Manage access" panel
 *   2. <FileDetailsPanel>        — right-side properties drawer
 *   3. Settings → Security       — admin-level overview
 *
 * USE on every list row / page header? **No.** Resist the urge to badge
 * everything. Google Drive shows only a "shared people" icon on the row
 * and tucks the rest into the share dialog — we mirror that.
 *
 *   ClassificationBadge ← Share dialog, file details panel, RAG citations
 *   VisibilityIcon      ← Share dialog "currently visible to" line
 *   WatermarkLayer      ← Always-on for confidential+ content (functional,
 *                          not decorative — protects content)
 *   PermissionBanner    ← Top of viewer when legal_hold / quarantine /
 *                          pii / no_access / client_view applies
 *                          (functional — explains why something is off)
 *
 * See docs/PERMISSIONS_UX_DESIGN_ZH.md §10 for the full P0/P1/P2 component
 * inventory.
 */
export { default as ClassificationBadge } from "./ClassificationBadge";
export { default as VisibilityIcon } from "./VisibilityIcon";
export { default as WatermarkLayer } from "./WatermarkLayer";
export { default as PermissionBanner } from "./PermissionBanner";
export type { PermissionBannerReason } from "./PermissionBanner";
export { default as ShareDialog } from "./ShareDialog";
export type { NewExternalShareConfig } from "./ShareDialog";
export { default as UploadOptionsDialog } from "./UploadOptionsDialog";
export type { UploadOptionsValue } from "./UploadOptionsDialog";
export { default as FolderPropertiesDialog } from "./FolderPropertiesDialog";
export { default as DocumentPropertiesDialog } from "./DocumentPropertiesDialog";
export { default as PeoplePicker } from "./PeoplePicker";
export type { StaffOption } from "./PeoplePicker";
export { default as PersonRow } from "./PersonRow";
export type { ShareRole } from "./PersonRow";
