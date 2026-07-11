import { useState, useRef, useCallback, useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import { useUpgradeStore } from "../stores/upgrade";
import { relativeTime, formatFileSize, getVectorStatusBadge, VectorStatus, isVectorInProgress } from "../lib/format";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import StatusBadge from "../components/ui/StatusBadge";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import { SkeletonTable } from "../components/ui/Skeleton";
import EmptyState from "../components/ui/EmptyState";
import Modal from "../components/ui/Modal";

import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Button from "../components/ui/Button";
import Select from "../components/ui/Select";
import Dropdown from "../components/ui/Dropdown";
import { IconChevronLeft, IconChevronRight, IconPlus, IconUpload, IconList, IconDashboard, IconFolder, IconTrash, IconDocument, IconEdit, IconInfo, IconEye, IconLink, IconDownload, IconClose, IconRefresh, IconStar, IconExternalLink, IconText, IconWorkspace, IconShare, IconFlow } from "../components/icons";
import SmartToolbar from "../components/ui/SmartToolbar";
import ContextMenu, { useContextMenu } from "../components/ui/ContextMenu";
import type { MenuItem } from "../components/ui/ContextMenu";
import CardStatusOverlay, { cardStatusClass, parseIndexingProgress } from "../components/ui/CardStatusOverlay";
import { WikiLinkedText, type WikiLinkInfo } from "../components/WikiLinkedText";
import WikiMapModal, { type WikiMapPage } from "../components/WikiMapModal";
import { pickFile, isConfigured as isGoogleDriveConfigured } from "../lib/google-drive";
import { UploadOptionsDialog, FolderPropertiesDialog, DocumentPropertiesDialog, ShareDialog, ClassificationBadge, VisibilityIcon } from "../components/permissions";
import type { NewExternalShareConfig } from "../components/permissions";
import type { Document, DocumentFolderInfo, DocumentGrant, DocumentShare, UserSummary } from "../lib/types";
import type { UploadOptionsValue } from "../components/permissions";
import {
  getKnowledgeBrowseParams,
  getKnowledgeDocumentsForView,
  type KnowledgeFileTypeFilter,
  type KnowledgeLibrarySection,
  type KnowledgeSortKey,
} from "../lib/knowledgeLayout";
import { useAuthStore } from "../stores/auth";
import {
  canDeleteDocument,
  canEditDocument,
  canManageDocumentMetadata,
  canManageFolder,
  canShareFolder,
  canShareDocument,
  hasPermission,
  isEntityAdmin,
} from "../lib/permissions";
import { isCodeLikeFile } from "../lib/codeFiles";

import { t } from "../lib/i18n";

type WikiMapLink = NonNullable<WikiMapPage["links"]>[number];

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeWikiPagePath(path: string | null | undefined): string {
  return String(path || "")
    .replace(/\\/g, "/")
    .trim()
    .replace(/^\/+/, "")
    .replace(/\/+/g, "/")
    .replace(/^(?:\.\/)+/, "");
}

function uniqueWikiLinks(links: WikiMapLink[] = []): WikiMapLink[] {
  const seen = new Set<string>();
  const out: WikiMapLink[] = [];
  for (const link of links) {
    const key = [
      normalizeWikiPagePath(link.resolved_path),
      link.document_id || "",
      String(link.target || "").trim(),
      String(link.display || "").trim(),
    ].join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(link);
  }
  return out;
}

function uniqueWikiBacklinks(backlinks: NonNullable<WikiMapPage["backlinks"]> = []): NonNullable<WikiMapPage["backlinks"]> {
  const seen = new Set<string>();
  const out: NonNullable<WikiMapPage["backlinks"]> = [];
  for (const backlink of backlinks) {
    const key = `${normalizeWikiPagePath(backlink.source_path)}|${backlink.source_title || ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(backlink);
  }
  return out;
}

function isExternalFileDrag(e: React.DragEvent): boolean {
  return Array.from(e.dataTransfer.types || []).includes("Files");
}

function dedupeWikiPages(rawPages: WikiMapPage[]): WikiMapPage[] {
  const byIdentity = new Map<string, WikiMapPage>();
  for (const rawPage of rawPages) {
    const path = normalizeWikiPagePath(rawPage.path);
    const identity = rawPage.document_id ? `doc:${rawPage.document_id}` : `path:${path}`;
    const page: WikiMapPage = { ...rawPage, path };
    const existing = byIdentity.get(identity);
    if (!existing) {
      byIdentity.set(identity, {
        ...page,
        links: uniqueWikiLinks(page.links || []),
        backlinks: uniqueWikiBacklinks(page.backlinks || []),
      });
      continue;
    }
    existing.links = uniqueWikiLinks([...(existing.links || []), ...(page.links || [])]);
    existing.backlinks = uniqueWikiBacklinks([...(existing.backlinks || []), ...(page.backlinks || [])]);
    existing.document_id = existing.document_id || page.document_id;
    existing.document_name = existing.document_name || page.document_name;
    existing.title = existing.title || page.title;
    existing.path = existing.path || page.path;
  }
  return Array.from(byIdentity.values()).sort((a, b) =>
    String(a.title || a.path).localeCompare(String(b.title || b.path))
  );
}
/* ── Scoped styles ─────────────────────────────────── */

const STYLES = `
.kb-sidebar-link {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border-radius: 12px;
  border: none;
  cursor: pointer;
  font-size: 14px;
  font-weight: 400;
  transition: all 0.15s;
  background: transparent;
  color: #44403c;
  text-align: left;
}
.kb-sidebar-link:hover {
  background: rgba(245,245,244,0.8);
}
.kb-sidebar-link.active {
  background: linear-gradient(135deg, rgba(79,125,117,0.12), rgba(90,142,166,0.12));
  color: #436b65;
}
.kb-sidebar-link.disabled {
  color: #d6d3d1;
  cursor: not-allowed;
}
.kb-sidebar-link.disabled:hover {
  background: transparent;
}

.kb-sidebar-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 0;
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #a8a29e;
}

.kb-collapse-btn {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: none;
  background: transparent;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #a8a29e;
  transition: all 0.2s;
}
.kb-collapse-btn:hover {
  background: #f5f5f4;
  color: #57534e;
}

.kb-tree-node {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 8px;
  cursor: pointer;
  font-weight: 500;
  color: #57534e;
  transition: all 0.12s;
  min-height: 30px;
}
.kb-tree-node:hover {
  background: rgba(245,245,244,0.8);
}
.kb-tree-node.active {
  background: rgba(79,125,117,0.08);
  color: #436b65;
  font-weight: 600;
}
.kb-tree-node.drag-over {
  background: rgba(79,125,117,0.12);
  outline: 2px solid rgba(79,125,117,0.4);
  outline-offset: -2px;
}
.kb-tree-toggle {
  width: 16px;
  height: 16px;
  border: none;
  background: transparent;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #a8a29e;
  padding: 0;
  flex-shrink: 0;
}

.kb-new-group-sidebar {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px dashed rgba(79,125,117,0.3);
  background: transparent;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: #4f7d75;
  transition: all 0.15s;
  margin-top: 4px;
}
.kb-new-group-sidebar:hover {
  background: rgba(79,125,117,0.04);
}

.kb-expand-sidebar {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  border: 1px solid rgba(231,229,228,0.6);
  background: rgba(255,255,255,0.6);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #78716c;
  transition: all 0.2s;
}
.kb-expand-sidebar:hover {
  background: #f5f5f4;
}

.wiki-linked-text {
  white-space: pre-wrap;
}
.wiki-inline-link {
  display: inline-flex;
  align-items: center;
  border: 1px solid rgba(79, 125, 117, 0.22);
  border-radius: 999px;
  background: rgba(242, 246, 245, 0.72);
  color: #436b65;
  padding: 0 7px;
  margin: 0 1px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
  transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;
}
.wiki-inline-link:hover {
  background: rgba(229, 238, 235, 0.72);
  border-color: rgba(79, 125, 117, 0.36);
  color: #395a54;
}
.wiki-inline-link-missing {
  border-color: rgba(168, 162, 158, 0.34);
  background: rgba(250, 250, 249, 0.78);
  color: #78716c;
  border-style: dashed;
}
.wiki-inline-link-missing:hover {
  background: rgba(245, 245, 244, 0.95);
  color: #57534e;
}

.kb-wiki-map-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-left: auto;
  border-radius: 999px;
  border: 1px solid rgba(79, 125, 117, 0.22);
  background: rgba(242, 246, 245, 0.62);
  color: #436b65;
  height: 28px;
  padding: 0 11px;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  transition: background 0.16s ease, border-color 0.16s ease, transform 0.16s ease;
}
.kb-wiki-map-pill:hover {
  background: rgba(229, 238, 235, 0.72);
  border-color: rgba(79, 125, 117, 0.34);
  transform: translateY(-1px);
}

.kb-breadcrumb-item {
  padding: 4px 8px;
  border-radius: 6px;
  transition: background 0.15s;
  cursor: pointer;
}
.kb-breadcrumb-item:hover {
  background: #f5f5f4;
}

.kb-view-btn {
  width: 32px;
  height: 32px;
  border-radius: 6px;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}

.kb-doc-name-link {
  font-size: 14px;
  font-weight: 600;
  color: #292524;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 0;
  text-align: left;
  display: block;
  max-width: 100%;
  transition: color 0.15s;
}
.kb-doc-name-link:hover {
  color: #4f7d75;
}

.kb-delete-btn {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: none;
  background: transparent;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #d6d3d1;
  transition: all 0.2s;
}
.kb-delete-btn:hover {
  color: #d65f59;
  background: #f8f0ef;
}

.kb-stat-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  border-radius: 16px;
  background: rgba(255,255,255,0.7);
  border: 1px solid rgba(231,229,228,0.5);
  min-width: 0;
}

.kb-folder-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  border-radius: 16px;
  background: rgba(255,255,255,0.7);
  border: 1px solid rgba(231,229,228,0.5);
  cursor: pointer;
  transition: all 0.15s;
}
.kb-folder-card:hover {
  background: rgba(255,255,255,0.9);
  border-color: rgba(79,125,117,0.25);
  box-shadow: 0 4px 12px rgba(79,125,117,0.06);
}

.kb-section-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin: 0 0 10px;
}
.kb-section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  color: #292524;
  font-size: 13px;
  font-weight: 800;
  line-height: 1.3;
}
.kb-section-title svg {
  color: #a8a29e;
}
.kb-section-count {
  color: #a8a29e;
  font-size: 11px;
  font-weight: 700;
  line-height: 1.3;
}
.kb-section-empty {
  margin: 0;
  color: #a8a29e;
  font-size: 12px;
  font-weight: 600;
}

.kb-file-card {
  display: flex;
  flex-direction: column;
  border-radius: 16px;
  background: rgba(255,255,255,0.7);
  border: 1px solid rgba(231,229,228,0.5);
  cursor: pointer;
  transition: all 0.15s;
  position: relative;
  overflow: hidden;
}
.kb-file-card:hover {
  background: rgba(255,255,255,0.9);
  border-color: rgba(231,229,228,0.8);
  box-shadow: 0 4px 16px rgba(0,0,0,0.04);
}
.kb-file-card-body {
  padding: 12px 16px 14px;
}

.kb-video-preview {
  width: 100%;
  height: 100%;
  background: linear-gradient(135deg, #fdf2f8 0%, #fafaf9 100%);
}
.kb-video-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  color: #c96a98;
}
.kb-video-placeholder svg {
  opacity: .78;
}
.kb-video-placeholder span {
  color: #78716c;
  font-size: 11px;
  font-weight: 800;
}

.kb-inline-folder-input {
  font-size: 13px;
  font-weight: 600;
  color: #292524;
  border: 1.5px solid #4f7d75;
  border-radius: 8px;
  padding: 4px 8px;
  outline: none;
  background: white;
  width: 100%;
}
.kb-inline-folder-input:focus {
  box-shadow: 0 0 0 3px rgba(79,125,117,0.1);
}

.kb-new-dropdown {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  min-width: 220px;
  padding: 6px;
  border-radius: 14px;
  background: rgba(255,255,255,0.95);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(231,229,228,0.7);
  box-shadow: 0 12px 40px rgba(0,0,0,0.1), 0 4px 12px rgba(0,0,0,0.05);
  z-index: 50;
  animation: kb-dropdown-in 0.15s ease-out;
}
@keyframes kb-dropdown-in {
  from { opacity: 0; transform: translateY(-4px) scale(0.97); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
.kb-new-dropdown-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 12px;
  border-radius: 10px;
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: #44403c;
  transition: all 0.12s;
  text-align: left;
}
.kb-new-dropdown-item:hover {
  background: rgba(245,245,244,0.9);
  color: #436b65;
}
.kb-new-dropdown-item.disabled {
  color: #a8a29e;
  cursor: not-allowed;
}
.kb-new-dropdown-item.disabled:hover {
  background: transparent;
  color: #a8a29e;
}
.kb-new-dropdown-divider {
  height: 1px;
  background: rgba(231,229,228,0.6);
  margin: 4px 8px;
}

.kb-file-type-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 14px 8px;
  border-radius: 12px;
  border: 2px solid rgba(231,229,228,0.5);
  background: rgba(255,255,255,0.6);
  cursor: pointer;
  transition: all 0.15s;
  min-width: 0;
}
.kb-file-type-card:hover {
  border-color: rgba(79,125,117,0.3);
  background: rgba(255,255,255,0.9);
}
.kb-file-type-card.selected {
  border-color: #4f7d75;
  background: rgba(79,125,117,0.04);
  box-shadow: 0 0 0 3px rgba(79,125,117,0.1);
}

.kb-quick-look-overlay {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 50%;
  min-width: 400px;
  max-width: 700px;
  z-index: 60;
  background: rgba(255,255,255,0.97);
  backdrop-filter: blur(24px);
  border-left: 1px solid rgba(231,229,228,0.7);
  box-shadow: -12px 0 40px rgba(0,0,0,0.08);
  display: flex;
  flex-direction: column;
  animation: kb-slide-in-right 0.25s ease-out;
}
@keyframes kb-slide-in-right {
  from { transform: translateX(100%); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
.kb-quick-look-backdrop {
  position: fixed;
  inset: 0;
  z-index: 59;
  background: rgba(0,0,0,0.15);
  animation: kb-fade-in 0.2s ease-out;
}
@keyframes kb-fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

.kb-trash-restore-btn {
  padding: 4px 12px;
  border-radius: 8px;
  border: 1px solid rgba(79,125,117,0.3);
  background: rgba(79,125,117,0.06);
  color: #4f7d75;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.kb-trash-restore-btn:hover {
  background: rgba(79,125,117,0.12);
}
.kb-trash-permdelete-btn {
  padding: 4px 12px;
  border-radius: 8px;
  border: 1px solid rgba(214,95,89,0.2);
  background: transparent;
  color: #d65f59;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.kb-trash-permdelete-btn:hover {
  background: rgba(214,95,89,0.06);
}

html[data-theme="dark"] .kb-sidebar-link {
  color: rgba(255,255,255,0.72);
}
html[data-theme="dark"] .kb-sidebar-link:hover,
html[data-theme="dark"] .kb-tree-node:hover,
html[data-theme="dark"] .kb-new-dropdown-item:hover {
  background: rgba(255,255,255,0.07);
  color: #ffffff;
}
html[data-theme="dark"] .kb-sidebar-link.active,
html[data-theme="dark"] .kb-tree-node.active {
  background: rgba(125,218,205,0.14);
  color: var(--accent);
}
html[data-theme="dark"] .kb-sidebar-toggle,
html[data-theme="dark"] .kb-tree-toggle,
html[data-theme="dark"] .kb-section-count,
html[data-theme="dark"] .kb-section-empty {
  color: rgba(255,255,255,0.45);
}
html[data-theme="dark"] .kb-tree-node {
  color: rgba(255,255,255,0.68);
}
html[data-theme="dark"] .kb-expand-sidebar,
html[data-theme="dark"] .kb-folder-card,
html[data-theme="dark"] .kb-file-card,
html[data-theme="dark"] .kb-stat-card {
  background: rgba(255,255,255,0.045);
  border-color: rgba(255,255,255,0.11);
  color: rgba(255,255,255,0.78);
  box-shadow: none;
}
html[data-theme="dark"] .kb-folder-card:hover,
html[data-theme="dark"] .kb-file-card:hover,
html[data-theme="dark"] .kb-expand-sidebar:hover {
  background: rgba(255,255,255,0.075);
  border-color: rgba(125,218,205,0.28);
  box-shadow: 0 14px 30px rgba(0,0,0,0.22);
}
html[data-theme="dark"] .kb-folder-card p,
html[data-theme="dark"] .kb-file-card p,
html[data-theme="dark"] .kb-doc-name-link,
html[data-theme="dark"] .kb-section-title {
  color: #ffffff;
}
html[data-theme="dark"] .kb-folder-card span,
html[data-theme="dark"] .kb-file-card span {
  color: rgba(255,255,255,0.58);
}
html[data-theme="dark"] .kb-file-card-body {
  background: rgba(255,255,255,0.035);
}
html[data-theme="dark"] .knowledge-page .glass-table thead th {
  background: rgba(255,255,255,0.035);
  border-bottom-color: rgba(255,255,255,0.08);
  color: rgba(255,255,255,0.42);
}
html[data-theme="dark"] .knowledge-page .glass-table tbody td {
  background: rgba(255,255,255,0.035);
  border-top-color: rgba(255,255,255,0.075);
  border-bottom-color: rgba(255,255,255,0.075);
  color: rgba(255,255,255,0.76);
}
html[data-theme="dark"] .knowledge-page .glass-table tbody td:first-child {
  border-left-color: rgba(255,255,255,0.075);
  border-top-left-radius: 12px;
  border-bottom-left-radius: 12px;
}
html[data-theme="dark"] .knowledge-page .glass-table tbody td:last-child {
  border-right-color: rgba(255,255,255,0.075);
  border-top-right-radius: 12px;
  border-bottom-right-radius: 12px;
}
html[data-theme="dark"] .knowledge-page .glass-table tbody tr:hover td {
  background: rgba(255,255,255,0.065);
  border-color: rgba(255,255,255,0.11);
}
html[data-theme="dark"] .knowledge-page .glass-table tbody tr[class*="bg-manor-50"] td {
  background: rgba(125,218,205,0.12);
  border-color: rgba(125,218,205,0.24);
}
html[data-theme="dark"] .knowledge-page .glass-table tbody tr.drag-over td {
  background: rgba(125,218,205,0.14);
  border-color: rgba(125,218,205,0.36);
}
html[data-theme="dark"] .kb-video-preview {
  background: rgba(255,255,255,0.035);
}
html[data-theme="dark"] .kb-video-placeholder {
  color: rgba(125,218,205,0.7);
}
html[data-theme="dark"] .kb-video-placeholder span {
  color: rgba(255,255,255,0.56);
}
html[data-theme="dark"] .kb-breadcrumb-item:hover {
  background: rgba(255,255,255,0.07);
}
html[data-theme="dark"] .kb-wiki-map-pill,
html[data-theme="dark"] .wiki-inline-link {
  background: rgba(125,218,205,0.12);
  border-color: rgba(125,218,205,0.24);
  color: var(--accent);
}
html[data-theme="dark"] .kb-wiki-map-pill:hover,
html[data-theme="dark"] .wiki-inline-link:hover {
  background: rgba(125,218,205,0.18);
  border-color: rgba(125,218,205,0.36);
  color: #ffffff;
}
html[data-theme="dark"] .wiki-inline-link-missing {
  background: rgba(255,255,255,0.055);
  border-color: rgba(255,255,255,0.16);
  color: rgba(255,255,255,0.64);
}
html[data-theme="dark"] .kb-new-dropdown,
html[data-theme="dark"] .kb-quick-look-overlay {
  background: rgba(17,19,18,0.98);
  border-color: rgba(255,255,255,0.12);
  box-shadow: 0 22px 54px rgba(0,0,0,0.42);
}
html[data-theme="dark"] .kb-quick-look-backdrop {
  background: var(--modal-overlay-bg);
}
html[data-theme="dark"] .kb-new-dropdown-item {
  color: rgba(255,255,255,0.78);
}
html[data-theme="dark"] .kb-new-dropdown-divider {
  background: rgba(255,255,255,0.1);
}
html[data-theme="dark"] .kb-file-type-card {
  background: rgba(255,255,255,0.045);
  border-color: rgba(255,255,255,0.12);
  color: rgba(255,255,255,0.78);
}
html[data-theme="dark"] .kb-file-type-card:hover,
html[data-theme="dark"] .kb-file-type-card.selected {
  background: rgba(125,218,205,0.12);
  border-color: rgba(125,218,205,0.34);
  color: #ffffff;
}
html[data-theme="dark"] .kb-inline-folder-input {
  background: rgba(255,255,255,0.07);
  border-color: rgba(125,218,205,0.42);
  color: #ffffff;
}
html[data-theme="dark"] .kb-delete-btn {
  color: rgba(255,255,255,0.46);
}
html[data-theme="dark"] .kb-delete-btn:hover {
  color: #fca5a5;
  background: rgba(214,95,89,0.14);
}
html[data-theme="dark"] .kb-trash-restore-btn {
  background: rgba(125,218,205,0.12);
  border-color: rgba(125,218,205,0.24);
  color: var(--accent);
}
html[data-theme="dark"] .kb-trash-permdelete-btn {
  border-color: rgba(248,113,113,0.24);
  color: #fca5a5;
}

.kb-folder-card.drag-over {
  border-color: #4f7d75 !important;
  background: rgba(79,125,117,0.06) !important;
  box-shadow: 0 0 0 3px rgba(79,125,117,0.12), 0 4px 12px rgba(79,125,117,0.08) !important;
}
.kb-file-card.dragging,
.kb-folder-card.dragging {
  opacity: 0.4;
}
.kb-breadcrumb-item.drag-over {
  background: rgba(79,125,117,0.12) !important;
  outline: 2px solid #4f7d75;
  outline-offset: -2px;
  border-radius: 6px;
}
tr.drag-over > td {
  background: rgba(79,125,117,0.06) !important;
}
tr.dragging {
  opacity: 0.4;
}
.kb-drop-zone-active {
  background: rgba(79,125,117,0.02) !important;
}

.kb-empty-folder-shell {
  min-height: min(58vh, 520px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 34px 16px 64px;
}
.kb-empty-folder-panel {
  width: min(100%, 620px);
  border: 1px dashed rgba(168,162,158,0.38);
  border-radius: 18px;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.82), rgba(250,250,249,0.74)),
    radial-gradient(circle at 50% 0%, rgba(79,125,117,0.08), transparent 46%);
  padding: 30px 28px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  transition: var(--card-hover-transition);
}
.kb-empty-folder-panel.can-upload {
  cursor: pointer;
}
.kb-empty-folder-panel.can-upload:hover {
  border-color: var(--card-hover-border);
  background: var(--card-hover-bg);
  box-shadow: var(--card-hover-shadow);
  transform: var(--card-hover-transform);
}
.kb-empty-folder-panel.is-drag-over {
  border-color: rgba(79,125,117,0.35);
  background: var(--card-hover-bg);
  box-shadow: var(--card-hover-shadow);
  transform: var(--card-hover-transform);
}
.kb-empty-folder-visual {
  position: relative;
  width: 158px;
  height: 112px;
  margin-bottom: 16px;
  color: #527a72;
}
.kb-empty-folder-sketch {
  width: 100%;
  height: 100%;
  overflow: visible;
}
.kb-empty-folder-sketch .line {
  fill: none;
  stroke: currentColor;
  stroke-width: 2.2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.kb-empty-folder-sketch .thin {
  fill: none;
  stroke: rgba(82,122,114,0.54);
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.kb-empty-folder-sketch .dash {
  fill: none;
  stroke: rgba(82,122,114,0.34);
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-dasharray: 3 7;
}
.kb-empty-folder-sketch .paper {
  fill: rgba(255,255,255,0.74);
  stroke: rgba(82,122,114,0.34);
  stroke-width: 1.4;
}
.kb-empty-folder-sketch .folder-fill {
  fill: rgba(251,191,36,0.16);
}
.kb-empty-folder-sketch .folder-tab {
  fill: rgba(251,191,36,0.22);
}
.kb-empty-folder-sketch .upload-fill {
  fill: rgba(255,255,255,0.86);
  stroke: rgba(82,122,114,0.28);
  stroke-width: 1.3;
}
.kb-empty-folder-panel.is-drag-over .kb-empty-folder-visual {
  color: #436b65;
}
.kb-empty-folder-title {
  margin: 0;
  color: #292524;
  font-size: 18px;
  font-weight: 820;
  letter-spacing: 0;
}
.kb-empty-folder-copy {
  max-width: 420px;
  margin: 8px 0 0;
  color: #78716c;
  font-size: 13px;
  line-height: 1.55;
}
.kb-empty-folder-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 20px;
}
.kb-empty-folder-action {
  height: 34px;
  border-radius: 9px;
  border: 1px solid rgba(231,229,228,0.84);
  background: rgba(255,255,255,0.82);
  color: #44403c;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 760;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
}
.kb-empty-folder-action:hover {
  background: #fff;
  border-color: rgba(79,125,117,0.24);
  color: #436b65;
}
.kb-empty-folder-action.primary {
  border-color: rgba(79,125,117,0.26);
  background: rgba(79,125,117,0.08);
  color: #436b65;
}
html[data-theme="dark"] .kb-empty-folder-panel {
  border-color: rgba(255,255,255,0.14);
  background:
    linear-gradient(180deg, rgba(22,25,23,0.96), rgba(18,20,19,0.94)),
    radial-gradient(circle at 50% 0%, rgba(127,208,196,0.1), transparent 48%);
}
html[data-theme="dark"] .kb-empty-folder-panel.can-upload:hover,
html[data-theme="dark"] .kb-empty-folder-panel.is-drag-over {
  border-color: rgba(127,208,196,0.32);
  background: rgba(29,33,31,0.98);
}
html[data-theme="dark"] .kb-empty-folder-visual {
  color: rgba(127,208,196,0.72);
}
html[data-theme="dark"] .kb-empty-folder-sketch .thin {
  stroke: rgba(127,208,196,0.38);
}
html[data-theme="dark"] .kb-empty-folder-sketch .dash {
  stroke: rgba(127,208,196,0.28);
}
html[data-theme="dark"] .kb-empty-folder-sketch .paper,
html[data-theme="dark"] .kb-empty-folder-sketch .upload-fill {
  fill: rgba(255,255,255,0.08);
  stroke: rgba(127,208,196,0.3);
}
html[data-theme="dark"] .kb-empty-folder-sketch .folder-fill {
  fill: rgba(127,208,196,0.12);
}
html[data-theme="dark"] .kb-empty-folder-sketch .folder-tab {
  fill: rgba(127,208,196,0.18);
}
html[data-theme="dark"] .kb-empty-folder-title {
  color: var(--text-strong);
}
html[data-theme="dark"] .kb-empty-folder-copy {
  color: var(--text-muted);
}
html[data-theme="dark"] .kb-empty-folder-action {
  border-color: rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.07);
  color: var(--text-default);
}
html[data-theme="dark"] .kb-empty-folder-action:hover {
  border-color: rgba(127,208,196,0.26);
  background: rgba(255,255,255,0.1);
  color: #ffffff;
}
html[data-theme="dark"] .kb-empty-folder-action.primary {
  border-color: rgba(127,208,196,0.32);
  background: rgba(127,208,196,0.14);
  color: #ffffff;
}
.kb-batch-bar {
  position: fixed;
  bottom: calc(max(18px, env(safe-area-inset-bottom) + 18px));
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 12px;
  width: max-content;
  max-width: min(calc(100vw - 32px), 760px);
  padding: 12px 20px;
  border-radius: 16px;
  background: rgba(28,25,23,0.92);
  backdrop-filter: blur(12px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.2);
  z-index: 120;
  color: white;
  font-size: 13px;
  font-weight: 600;
}
.kb-batch-bar button {
  padding: 6px 14px;
  border-radius: 8px;
  border: none;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.kb-batch-btn-danger {
  background: rgba(214,95,89,0.2);
  color: #ddafac;
}
.kb-batch-btn-danger:hover {
  background: rgba(214,95,89,0.35);
}
.kb-batch-btn-primary {
  background: rgba(79,125,117,0.25);
  color: #d6d3d1;
}
.kb-batch-btn-primary:hover {
  background: rgba(79,125,117,0.4);
}
.kb-batch-btn-ghost {
  background: rgba(255,255,255,0.1);
  color: #a8a29e;
}
.kb-batch-btn-ghost:hover {
  background: rgba(255,255,255,0.2);
  color: white;
}
@media (max-width: 640px) {
  .kb-batch-bar {
    left: 16px;
    right: 16px;
    transform: none;
    width: auto;
    align-items: stretch;
  }
  .kb-batch-bar button {
    flex: 1 1 auto;
  }
}
.kb-select-checkbox {
  width: 18px;
  height: 18px;
  border-radius: 5px;
  border: 2px solid rgba(168,162,158,0.5);
  background: white;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
  flex-shrink: 0;
}
.kb-select-checkbox.checked {
  background: #4f7d75;
  border-color: #4f7d75;
}
`;

/* ── Constants ─────────────────────────────────────── */

const EDITABLE_EXTENSIONS = new Set([
  "md", "markdown", "txt", "log", "csv",
  "py", "js", "ts", "tsx", "json", "yaml", "yml", "sql", "sh", "css", "html",
  "jsx", "xml", "toml", "ini", "cfg", "env",
  "docx", "xlsx", "pptx", "ppt", "diagram",
]);

const VIEWABLE_EXTENSIONS = new Set([
  "pdf", "doc", "docx", "xlsx", "xls", "pptx", "ppt",
  "wps", "et", "dps",
  "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico",
  "mp4", "webm", "ogg", "mov",
  "mp3", "wav", "aac", "flac", "m4a",
]);

const FILE_TYPE_ICONS: Record<string, { icon: string; color: string; bg: string }> = {
  pdf: { icon: "PDF", color: "#c14a44", bg: "#f8f0ef" },
  docx: { icon: "DOC", color: "#4869ac", bg: "#f3f6fa" },
  doc: { icon: "DOC", color: "#4869ac", bg: "#f3f6fa" },
  xlsx: { icon: "XLS", color: "#437f6b", bg: "#f1f6f3" },
  xls: { icon: "XLS", color: "#437f6b", bg: "#f1f6f3" },
  txt: { icon: "TXT", color: "#57534e", bg: "#f5f5f4" },
  csv: { icon: "CSV", color: "#b27c34", bg: "#faf7ef" },
  pptx: { icon: "PPT", color: "#b66a3c", bg: "#f9f4ec" },
  ppt: { icon: "PPT", color: "#b66a3c", bg: "#f9f4ec" },
  wps: { icon: "WPS", color: "#4869ac", bg: "#f3f6fa" },
  et: { icon: "ET", color: "#437f6b", bg: "#f1f6f3" },
  dps: { icon: "DPS", color: "#b66a3c", bg: "#f9f4ec" },
  md: { icon: "MD", color: "#6f4ba8", bg: "#f7f4fa" },
  diagram: { icon: "DIA", color: "#57534e", bg: "#f5f5f4" },
  "diagram.json": { icon: "DIA", color: "#57534e", bg: "#f5f5f4" },
  json: { icon: "JSON", color: "#a16207", bg: "#fefce8" },
  html: { icon: "HTML", color: "#b66a3c", bg: "#f9f4ec" },
  png: { icon: "PNG", color: "#a07fc0", bg: "#f7f4fa" },
  jpg: { icon: "JPG", color: "#a07fc0", bg: "#f7f4fa" },
  jpeg: { icon: "JPG", color: "#a07fc0", bg: "#f7f4fa" },
  gif: { icon: "GIF", color: "#a07fc0", bg: "#f7f4fa" },
  webp: { icon: "WEBP", color: "#a07fc0", bg: "#f7f4fa" },
  svg: { icon: "SVG", color: "#6f4ba8", bg: "#f7f4fa" },
  mp4: { icon: "MP4", color: "#c96a98", bg: "#fdf2f8" },
  webm: { icon: "WEBM", color: "#c96a98", bg: "#fdf2f8" },
  mov: { icon: "MOV", color: "#c96a98", bg: "#fdf2f8" },
  mp3: { icon: "MP3", color: "#5e9098", bg: "#f1f6f5" },
  wav: { icon: "WAV", color: "#5e9098", bg: "#f1f6f5" },
  aac: { icon: "AAC", color: "#5e9098", bg: "#f1f6f5" },
  flac: { icon: "FLAC", color: "#5e9098", bg: "#f1f6f5" },
  m4a: { icon: "M4A", color: "#5e9098", bg: "#f1f6f5" },
  py: { icon: "PY", color: "#4869ac", bg: "#f3f6fa" },
  js: { icon: "JS", color: "#ca8a04", bg: "#fefce8" },
  ts: { icon: "TS", color: "#4869ac", bg: "#f3f6fa" },
  sql: { icon: "SQL", color: "#4f7e87", bg: "#f1f6f5" },
  yaml: { icon: "YML", color: "#65a30d", bg: "#f7fee7" },
  yml: { icon: "YML", color: "#65a30d", bg: "#f7fee7" },
  zip: { icon: "ZIP", color: "#78716c", bg: "#f5f5f4" },
};

type LibrarySection = KnowledgeLibrarySection;
type FileTypeFilter = KnowledgeFileTypeFilter;
type SortKey = KnowledgeSortKey;
type ViewMode = "table" | "grid";
type FolderPathEntry = { id: string; name: string };

const LIBRARY_SECTION_VALUES: LibrarySection[] = ["all", "recent", "favorites", "trash"];
const FILE_TYPE_FILTER_VALUES: FileTypeFilter[] = ["all", "pdf", "docx", "xlsx", "txt", "csv", "md", "json", "html", "png", "jpg", "mp4", "mp3", "py", "js", "sql", "yaml"];
const SORT_KEY_VALUES: SortKey[] = ["name", "date", "size"];
const VIEW_MODE_VALUES: ViewMode[] = ["table", "grid"];

function pickQueryValue<T extends string>(value: string | null, allowed: T[], fallback: T): T {
  return value && allowed.includes(value as T) ? (value as T) : fallback;
}

const SIDEBAR_SECTIONS: { key: LibrarySection; label: string; icon: string }[] = [
  { key: "all", label: t("page.knowledge.all_files"), icon: "M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" },
  { key: "recent", label: t("page.knowledge.recent"), icon: "M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" },
  { key: "favorites", label: t("page.knowledge.favorites"), icon: "M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" },
];

const FILE_TYPE_FILTERS: { key: FileTypeFilter; label: string; badge?: string; color?: string; bg?: string }[] = [
  { key: "all", label: t("page.knowledge.all_types") },
  { key: "pdf", label: t("page.knowledge.pdf"), badge: "PDF", color: "#c14a44", bg: "#f8f0ef" },
  { key: "docx", label: t("page.knowledge.word"), badge: "DOC", color: "#4869ac", bg: "#f3f6fa" },
  { key: "md", label: t("page.skill_form.markdown"), badge: "MD", color: "#6d6fb2", bg: "#f1f3f9" },
  { key: "json", label: t("page.skill_form.json"), badge: "JSON", color: "#437f6b", bg: "#f1f6f3" },
  { key: "html", label: t("page.skill_form.html"), badge: "HTML", color: "#b66a3c", bg: "#f9f4ec" },
  { key: "png", label: t("page.knowledge.images"), badge: "IMG", color: "#a07fc0", bg: "#f7f4fa" },
  { key: "mp4", label: t("page.account.video"), badge: "VID", color: "#c96a98", bg: "#fdf2f8" },
  { key: "mp3", label: t("page.knowledge.audio"), badge: "AUD", color: "#5e9098", bg: "#f1f6f5" },
  { key: "py", label: t("page.knowledge.python"), badge: "PY", color: "#4869ac", bg: "#f3f6fa" },
  { key: "js", label: t("page.knowledge.javascript"), badge: "JS", color: "#ca8a04", bg: "#fefce8" },
  { key: "sql", label: t("page.knowledge.sql"), badge: "SQL", color: "#4f7e87", bg: "#f1f6f5" },
  { key: "yaml", label: t("page.knowledge.yaml"), badge: "YML", color: "#65a30d", bg: "#f7fee7" },
  { key: "xlsx", label: t("page.knowledge.excel"), badge: "XLS", color: "#437f6b", bg: "#f1f6f3" },
  { key: "txt", label: t("page.skill_form.text"), badge: "TXT", color: "#57534e", bg: "#f5f5f4" },
  { key: "csv", label: t("page.knowledge.csv"), badge: "CSV", color: "#b27c34", bg: "#faf7ef" },
];

function getFileTypeInfo(name: string, fileType?: string) {
  if (name.toLowerCase().endsWith(".diagram.json")) {
    return FILE_TYPE_ICONS["diagram.json"];
  }
  const ext = (fileType || name?.split(".").pop() || "").toLowerCase();
  return FILE_TYPE_ICONS[ext] || { icon: ext.toUpperCase().slice(0, 4) || "?", color: "#57534e", bg: "#f5f5f4" };
}

const IMAGE_EXTENSIONS = new Set(["image", "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);
const VIDEO_EXTENSIONS = new Set(["video", "mp4", "webm", "ogg", "mov", "avi", "mkv"]);
const AUDIO_EXTENSIONS = new Set(["audio", "mp3", "wav", "ogg", "aac", "flac", "m4a", "wma"]);
const PRESENTATION_EXTENSIONS = new Set(["presentation", "ppt", "pptx"]);
const MEDIA_EXTENSIONS = new Set([...IMAGE_EXTENSIONS, ...VIDEO_EXTENSIONS, ...AUDIO_EXTENSIONS]);
type KnowledgePreviewType = "image" | "video" | "audio" | "presentation";

function isVideoFile(name: string, fileType?: string | null): boolean {
  const ext = (fileType || name?.split(".").pop() || "").toLowerCase();
  return VIDEO_EXTENSIONS.has(ext);
}

function isVideoProjectFile(name: string): boolean {
  return name.toLowerCase().endsWith(".video-edit.json");
}

const brokenMediaPreviewDocIds = new Set<string>();
const brokenVideoThumbnailDocIds = new Set<string>();

function shouldRequestVideoThumbnail(doc: { id: string; file_size?: number | null }): boolean {
  if (brokenVideoThumbnailDocIds.has(doc.id)) return false;
  if (typeof doc.file_size === "number" && doc.file_size > 0 && doc.file_size < 8 * 1024) return false;
  return true;
}

function documentThumbnailCacheVersion(doc: {
  created_at?: string | null;
  file_size?: number | null;
  status?: string | null;
  updated_at?: string | null;
  vector_status?: string | null;
}): string {
  return [
    doc.updated_at || doc.created_at || "",
    doc.file_size ?? "",
    doc.vector_status || "",
    doc.status || "",
  ].join(":");
}

async function loadKnowledgePreviewThumbnail(
  doc: {
    created_at?: string | null;
    file_size?: number | null;
    id: string;
    status?: string | null;
    updated_at?: string | null;
    vector_status?: string | null;
  },
  type: KnowledgePreviewType,
): Promise<string> {
  const version = documentThumbnailCacheVersion(doc);
  if (type === "image") return api.documents.imageThumbnail(doc.id, { cache: true, version });
  if (type === "video") return api.documents.videoThumbnail(doc.id, { cache: true, version });
  if (type === "presentation") return api.documents.presentationThumbnail(doc.id, { cache: true, version });
  throw new Error("Preview thumbnail unavailable");
}

function getMediaPreviewUrl(doc: { entity_id: string; fs_path?: string | null; name: string; file_type?: string | null; mime_type?: string | null; vector_status?: string | null; status?: string | null }): { type: KnowledgePreviewType; url: string } | null {
  const ext = (doc.file_type || doc.name?.split(".").pop() || "").toLowerCase();
  const mime = (doc.mime_type || "").toLowerCase();
  if (doc.vector_status === VectorStatus.FAILED || doc.status === "failed") return null;
  const url = "";
  if (IMAGE_EXTENSIONS.has(ext)) return { type: "image", url };
  if (VIDEO_EXTENSIONS.has(ext)) return { type: "video", url };
  if (PRESENTATION_EXTENSIONS.has(ext) || mime.includes("presentation")) return { type: "presentation", url };
  if (AUDIO_EXTENSIONS.has(ext)) return { type: "audio", url };
  return null;
}

function DocumentMediaPreview({ doc, media }: { doc: any; media: { type: KnowledgePreviewType; url: string } }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [isVisible, setIsVisible] = useState(false);
  const [previewRequested, setPreviewRequested] = useState(false);
  const [thumbnailUrl, setThumbnailUrl] = useState<string | null>(null);
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);
  const [previewFailed, setPreviewFailed] = useState(() => brokenMediaPreviewDocIds.has(doc.id));

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return undefined;
    if (typeof IntersectionObserver === "undefined") {
      setIsVisible(true);
      return undefined;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setDisplayUrl(null);
    setThumbnailUrl(null);
    setPreviewRequested(false);
    setPreviewFailed(brokenMediaPreviewDocIds.has(doc.id));
  }, [doc.id, media.type]);

  useEffect(() => {
    if (!isVisible || media.type === "audio") return undefined;
    if (media.type === "video" && !shouldRequestVideoThumbnail(doc)) return undefined;
    let cancelled = false;
    loadKnowledgePreviewThumbnail(doc, media.type)
      .then((url) => {
        if (!cancelled) setThumbnailUrl(url);
      })
      .catch(() => {
        if (media.type === "video") brokenVideoThumbnailDocIds.add(doc.id);
        if (!cancelled) setThumbnailUrl(null);
      });
    return () => {
      cancelled = true;
    };
  }, [doc, doc.id, doc.file_size, isVisible, media.type]);

  useEffect(() => {
    if (media.type !== "video" || !isVisible || !previewRequested || previewFailed) return undefined;
    let cancelled = false;
    let objectUrl: string | null = null;
    api.documents.download(doc.id)
      .then((url) => {
        objectUrl = url;
        if (!cancelled) setDisplayUrl(url);
      })
      .catch(() => {
        brokenMediaPreviewDocIds.add(doc.id);
        if (!cancelled) {
          setDisplayUrl(null);
          setPreviewFailed(true);
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl?.startsWith("blob:")) URL.revokeObjectURL(objectUrl);
    };
  }, [doc.id, isVisible, media.type, previewFailed, previewRequested]);

  if (media.type === "image" || media.type === "presentation") {
    return (
      <div ref={containerRef} className="w-full h-full bg-stone-100">
        {thumbnailUrl ? (
          <img src={thumbnailUrl} alt={doc.name} loading="lazy" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        ) : media.type === "presentation" ? (
          <div className="kb-video-placeholder">
            <span>Presentation preview</span>
          </div>
        ) : (
          null
        )}
      </div>
    );
  }
  if (media.type === "video") {
    return (
      <div
        ref={containerRef}
        className="kb-video-preview"
        onMouseEnter={() => setPreviewRequested(true)}
        onFocus={() => setPreviewRequested(true)}
      >
        {displayUrl ? (
          <video src={`${displayUrl}#t=0.5`} muted preload="metadata" playsInline style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        ) : thumbnailUrl ? (
          <img src={thumbnailUrl} alt={doc.name} loading="lazy" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        ) : (
          <div className="kb-video-placeholder">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M8 5.14v13.72a1 1 0 0 0 1.52.85l10.96-6.86a1 1 0 0 0 0-1.7L9.52 4.29A1 1 0 0 0 8 5.14Z" />
            </svg>
            <span>{previewRequested ? "Loading preview" : "Video preview"}</span>
          </div>
        )}
      </div>
    );
  }
  return (
    <div ref={containerRef} className="w-full h-full flex flex-col items-center justify-center gap-2 bg-gradient-to-br from-violet-50 to-indigo-50">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-violet-400"><path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" /></svg>
      <span className="text-[11px] font-bold text-violet-400">Audio</span>
    </div>
  );
}

function documentExtension(input: string | { name?: string | null; file_type?: string | null; mime_type?: string | null }): string {
  if (typeof input === "string") return (input.split(".").pop() || "").toLowerCase();
  const nameExt = (input.name || "").split(".").pop()?.toLowerCase() || "";
  if (nameExt && nameExt !== input.name?.toLowerCase()) return nameExt;
  const fileType = (input.file_type || "").toLowerCase();
  if (fileType.includes("/")) {
    const subtype = fileType.split("/").pop() || "";
    return subtype === "svg+xml" ? "svg" : subtype.replace(/^x-/, "");
  }
  if (fileType) return fileType;
  const mime = (input.mime_type || "").split(";")[0].trim().toLowerCase();
  if (mime.includes("/")) {
    const subtype = mime.split("/").pop() || "";
    return subtype === "svg+xml" ? "svg" : subtype.replace(/^x-/, "");
  }
  return nameExt;
}

function isEditable(input: string | { name?: string | null; file_type?: string | null; mime_type?: string | null }): boolean {
  const ext = documentExtension(input);
  return EDITABLE_EXTENSIONS.has(ext) || isCodeLikeFile(input);
}

function isViewable(input: string | { name?: string | null; file_type?: string | null; mime_type?: string | null }): boolean {
  const ext = documentExtension(input);
  return VIEWABLE_EXTENSIONS.has(ext) || isCodeLikeFile(input) || ext === "image" || ext === "video" || ext === "audio";
}

function buildFolderPath(folder: any, allFolders: any[]): FolderPathEntry[] {
  const path: FolderPathEntry[] = [];
  const seen = new Set<string>();
  let current = folder;
  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    path.unshift({ id: current.id, name: current.name });
    current = allFolders.find((p: any) => p.id === current.parent_id);
  }
  return path;
}

function sameFolderPath(a: FolderPathEntry[], b: FolderPathEntry[]): boolean {
  return a.length === b.length && a.every((entry, i) => entry.id === b[i].id && entry.name === b[i].name);
}

function normalizeKnowledgeFolderId(folderId: string | null | undefined): string | null {
  return folderId && folderId !== "root" ? folderId : null;
}

/* ── Sidebar section toggle ────────────────────────── */

function SidebarSection({
  title,
  defaultOpen = true,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <button onClick={() => setOpen(!open)} className="kb-sidebar-toggle">
        <svg
          width="10" height="10" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round"
          className="transition-transform duration-200"
          style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)" }}
        >
          <path d="M9 5l7 7-7 7" />
        </svg>
        {title}
      </button>
      {open && <div className="flex flex-col gap-0.5 mt-1">{children}</div>}
    </div>
  );
}

/* ── Sidebar link ──────────────────────────────────── */

function SidebarLink({
  label,
  icon,
  isActive,
  count,
  disabled,
  tooltip,
  onClick,
}: {
  label: string;
  icon: string;
  isActive?: boolean;
  count?: number;
  disabled?: boolean;
  tooltip?: string;
  onClick?: () => void;
}) {
  return (
    <div className={`relative ${tooltip ? "group" : ""}`}>
      <button
        onClick={disabled ? undefined : onClick}
        className={`kb-sidebar-link ${isActive ? "active" : ""} ${disabled ? "disabled" : ""}`}
      >
        <svg
          width="16" height="16" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round"
          className="flex-shrink-0"
        >
          <path d={icon} />
        </svg>
        <span className="flex-1 text-left">{label}</span>
        {count !== undefined && (
          <span className="min-w-[26px] h-[26px] flex items-center justify-center rounded-full bg-stone-100/95 text-xs font-semibold text-stone-500">
            {count}
          </span>
        )}
      </button>
      {tooltip && (
        <div className="absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 px-2.5 py-1 rounded-lg bg-stone-800 text-white text-[11px] font-medium whitespace-nowrap opacity-0 pointer-events-none transition-opacity group-hover:opacity-100">
          {tooltip}
        </div>
      )}
    </div>
  );
}

/* ── Folder tree node ──────────────────────────────── */

function FolderTreeNode({
  folder,
  allFolders,
  depth,
  currentFolderId,
  onSelect,
  onDragOver,
  onDrop,
  dragOverTarget,
}: {
  folder: any;
  allFolders: any[];
  depth: number;
  currentFolderId: string | null;
  onSelect: (folder: any) => void;
  onDragOver?: (e: React.DragEvent, folderId: string) => void;
  onDrop?: (e: React.DragEvent, folderId: string) => void;
  dragOverTarget?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const children = allFolders.filter((f: any) => f.parent_id === folder.id);
  const hasChildren = children.length > 0;
  const isActive = currentFolderId === folder.id;
  const isDragOver = dragOverTarget === folder.id;

  return (
    <div>
      <div
        style={{ paddingLeft: depth * 16 }}
        className={`kb-tree-node${isActive ? " active" : ""}${isDragOver ? " drag-over" : ""}`}
        onClick={() => onSelect(folder)}
        onDragOver={onDragOver ? (e) => onDragOver(e, folder.id) : undefined}
        onDrop={onDrop ? (e) => onDrop(e, folder.id) : undefined}
      >
        {hasChildren ? (
          <button
            className="kb-tree-toggle"
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
          >
            <IconChevronRight size={12} style={{ transform: expanded ? "rotate(90deg)" : "none", transition: "transform 0.15s" }} />
          </button>
        ) : (
          <span style={{ width: 16 }} />
        )}
        <IconFolder size={14} className={isActive ? "text-manor-600" : "text-amber-500"} />
        <span className="truncate text-[13px]">{folder.name}</span>
        {folder.document_count > 0 && (
          <span className="text-[10px] text-stone-400 ml-auto">{folder.document_count}</span>
        )}
      </div>
      {expanded && children.map((child: any) => (
        <FolderTreeNode
          key={child.id}
          folder={child}
          allFolders={allFolders}
          depth={depth + 1}
          currentFolderId={currentFolderId}
          onSelect={onSelect}
          onDragOver={onDragOver}
          onDrop={onDrop}
          dragOverTarget={dragOverTarget}
        />
      ))}
    </div>
  );
}

/* ── File type filter list with auto-collapse ──────── */

const FILE_TYPE_VISIBLE_COUNT = 5; // "All Types" + first 4 types shown by default

function FileTypeFilterList({ fileTypeFilter, setFileTypeFilter }: {
  fileTypeFilter: FileTypeFilter;
  setFileTypeFilter: (f: FileTypeFilter) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? FILE_TYPE_FILTERS : FILE_TYPE_FILTERS.slice(0, FILE_TYPE_VISIBLE_COUNT);
  const hasMore = FILE_TYPE_FILTERS.length > FILE_TYPE_VISIBLE_COUNT;

  // Auto-expand if active filter is in the hidden portion
  const activeInHidden = !expanded && FILE_TYPE_FILTERS.findIndex((f) => f.key === fileTypeFilter) >= FILE_TYPE_VISIBLE_COUNT;

  const shown = activeInHidden
    ? [...FILE_TYPE_FILTERS.slice(0, FILE_TYPE_VISIBLE_COUNT), FILE_TYPE_FILTERS.find((f) => f.key === fileTypeFilter)!]
    : visible;

  return (
    <div className="overflow-y-auto flex-1">
      {shown.map((ft) => (
        <button
          key={ft.key}
          onClick={() => setFileTypeFilter(ft.key)}
          className={`kb-sidebar-link ${fileTypeFilter === ft.key ? "active" : ""}`}
        >
          {ft.badge ? (
            <span
              className="flex-shrink-0 w-[28px] h-[18px] rounded-[4px] flex items-center justify-center text-[9px] font-extrabold tracking-wide"
              style={{ background: ft.bg, color: ft.color }}
            >
              {ft.badge}
            </span>
          ) : (
            <svg
              width="16" height="16" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round"
              className="flex-shrink-0"
            >
              <path d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
            </svg>
          )}
          <span className="flex-1 text-left">{ft.label}</span>
        </button>
      ))}
      {hasMore && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="kb-sidebar-link text-stone-400 hover:text-stone-600"
        >
          <span className="flex-shrink-0 w-[28px] h-[18px] flex items-center justify-center text-[11px] font-bold tracking-widest">
            ...
          </span>
          <span className="flex-1 text-left text-[11px]">
            {expanded ? t("chat.show_less") : `${FILE_TYPE_FILTERS.length - FILE_TYPE_VISIBLE_COUNT} more`}
          </span>
        </button>
      )}
    </div>
  );
}


/* ── Quick Look image/file preview ─────────────────── */

function QuickLookPreview({ doc }: { doc: any }) {
  const ext = (doc.file_type || doc.name?.split(".").pop() || "").toLowerCase();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!MEDIA_EXTENSIONS.has(ext)) return;
    // For video/audio, prefer streaming URL (no full blob download)
    if (VIDEO_EXTENSIONS.has(ext) || AUDIO_EXTENSIONS.has(ext)) {
      const streamUrl = api.documents.streamUrl(doc);
      if (streamUrl) { setBlobUrl(streamUrl); return; }
    }
    let revoked = false;
    api.documents.download(doc.id).then((url) => {
      if (!revoked) setBlobUrl(url);
    }).catch(() => setError(true));
    return () => { revoked = true; if (blobUrl) URL.revokeObjectURL(blobUrl); };
  }, [doc.id, ext]);

  if (MEDIA_EXTENSIONS.has(ext)) {
    if (error) return <p className="text-sm text-stone-500 py-12 text-center">{t("page.knowledge.failed_to_load_preview")}</p>;
    if (!blobUrl) return <div className="flex justify-center py-12"><LoadingSpinner /></div>;

    if (VIDEO_EXTENSIONS.has(ext)) {
      return (
        <div className="flex justify-center py-4">
          <video
            src={blobUrl}
            controls
            className="max-w-full max-h-[50vh] rounded-lg border border-stone-200/50"
          />
        </div>
      );
    }

    if (AUDIO_EXTENSIONS.has(ext)) {
      return (
        <div className="flex flex-col items-center gap-4 py-8">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-violet-400"><path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" /></svg>
          <audio src={blobUrl} controls className="w-full max-w-md" />
          <p className="text-xs text-stone-400">{doc.name}</p>
        </div>
      );
    }

    return (
      <div className="flex justify-center py-4">
        <img
          src={blobUrl}
          alt={doc.name}
          className="max-w-full max-h-[50vh] rounded-lg border border-stone-200/50 object-contain"
        />
      </div>
    );
  }

  if (ext === "pdf") {
    return <p className="text-sm text-stone-500 py-12 text-center">{t("page.knowledge.preview_not_available_for_pdf_files_use_the_down")}</p>;
  }

  return <p className="text-sm text-stone-500 py-12 text-center">{t("page.knowledge.preview_not_available_for_this_file_type")}</p>;
}


/* ── Workspace Picker (shows which workspaces doc is already in) ── */

function WorkspacePickerContent({ doc, workspaces, onSelect }: { doc: any; workspaces: any[]; onSelect: (ws: any) => void }) {
  const { data: linkedWorkspaceIds = [] } = useQuery({
    queryKey: ["doc-workspaces", doc.id],
    queryFn: () => api.documents.getWorkspaces(doc.id),
    staleTime: 10_000,
  });

  if (workspaces.length === 0) {
    return <p className="text-sm text-stone-400 py-4 text-center">{t("page.knowledge.no_workspaces_available")}</p>;
  }

  return (
    <div className="flex flex-col gap-1 max-h-[320px] overflow-y-auto">
      {workspaces.map((ws: any) => {
        const isLinked = linkedWorkspaceIds.includes(ws.id);
        return (
          <button
            key={ws.id}
            disabled={isLinked}
            className={`flex items-center gap-3 w-full px-3 py-2.5 rounded-lg text-left transition-colors ${isLinked ? "opacity-50 cursor-default bg-stone-50" : "hover:bg-stone-50 cursor-pointer"}`}
            onClick={() => !isLinked && onSelect(ws)}
          >
            <IconWorkspace size={16} className={isLinked ? "text-stone-400" : "text-manor-500"} />
            <span className="text-sm font-medium text-stone-700 truncate flex-1">{ws.name}</span>
            {isLinked && <span className="text-xs text-stone-400 font-medium">{t("page.knowledge.added")}</span>}
          </button>
        );
      })}
    </div>
  );
}


const DOCUMENT_POLL_INTERVAL_MS = 5_000;
const DOCUMENT_QUEUED_POLL_INTERVAL_MS = 15_000;

function documentListPollInterval(value: unknown): number | false {
  const docs = Array.isArray((value as any)?.items) ? (value as any).items : [];
  const inProgressDocs = docs.filter((d: any) => isVectorInProgress(String(d?.vector_status || "")));
  if (inProgressDocs.length === 0) return false;
  return inProgressDocs.some((d: any) => d.vector_status === VectorStatus.PROCESSING || d.vector_status === VectorStatus.GENERATING)
    ? DOCUMENT_POLL_INTERVAL_MS
    : DOCUMENT_QUEUED_POLL_INTERVAL_MS;
}


/* ── Main component ────────────────────────────────── */

export default function Knowledge() {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const currentUser = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const filePickerFolderIdRef = useRef<string | null | undefined>(undefined);
  const inlineFolderRef = useRef<HTMLInputElement>(null);

  const [search, setSearch] = useState(searchParams.get("q") || "");
  const [dragOver, setDragOver] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true);
  const [librarySection, setLibrarySection] = useState<LibrarySection>(() => pickQueryValue(searchParams.get("section"), LIBRARY_SECTION_VALUES, "all"));
  const [fileTypeFilter, setFileTypeFilter] = useState<FileTypeFilter>(() => pickQueryValue(searchParams.get("type"), FILE_TYPE_FILTER_VALUES, "all"));
  const [sortKey, setSortKey] = useState<SortKey>(() => pickQueryValue(searchParams.get("sort"), SORT_KEY_VALUES, "date"));
  const [viewMode, setViewMode] = useState<ViewMode>(() => pickQueryValue(searchParams.get("view"), VIEW_MODE_VALUES, "grid"));
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(() => searchParams.get("workspace_id"));
  const canUploadKnowledge = Boolean(
    currentUser
    && (
      hasPermission(currentUser.role, "docs.upload")
      || currentUser.permissions?.includes("docs.upload")
    ),
  );
  const canManageAllDocuments = isEntityAdmin(currentUser);
  const canEditDoc = useCallback((doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined) => (
    canEditDocument(currentUser, doc)
  ), [currentUser]);
  const canShareDoc = useCallback((doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined) => (
    canShareDocument(currentUser, doc)
  ), [currentUser]);
  const canManageDocMetadata = useCallback((doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined) => (
    canManageDocumentMetadata(currentUser, doc)
  ), [currentUser]);
  const canDeleteDoc = useCallback((doc: Pick<Document, "owner_id" | "created_by" | "current_user_capabilities"> | null | undefined) => (
    canDeleteDocument(currentUser, doc)
  ), [currentUser]);
  const canManageFolderItem = useCallback((folder: Pick<DocumentFolderInfo, "owner_id"> | null | undefined) => (
    canManageFolder(currentUser, folder)
  ), [currentUser]);
  const canShareFolderItem = useCallback((folder: Pick<DocumentFolderInfo, "owner_id" | "current_user_capabilities"> | null | undefined) => (
    canShareFolder(currentUser, folder)
  ), [currentUser]);

  // Create Blank Document modal
  const [showCreateBlankModal, setShowCreateBlankModal] = useState(false);
  const [blankDocName, setBlankDocName] = useState("");
  const [blankDocType, setBlankDocType] = useState("md");

  // Import from URL modal
  const [showImportUrlModal, setShowImportUrlModal] = useState(false);
  const [importUrl, setImportUrl] = useState("");
  const [importUrlName, setImportUrlName] = useState("");

  // AI Draft modal
  const [showAiDraftModal, setShowAiDraftModal] = useState(false);
  const [aiDraftPrompt, setAiDraftPrompt] = useState("");
  const [aiDraftName, setAiDraftName] = useState("");
  const [aiDraftType, setAiDraftType] = useState("md");

  // Quick Look drawer
  const [quickLookDoc, setQuickLookDoc] = useState<any | null>(null);
  const [showWikiMap, setShowWikiMap] = useState(false);

  // Trash view is activated via librarySection === "trash"

  // Folder navigation state
  const [folderPath, setFolderPath] = useState<FolderPathEntry[]>([]);
  const routeFolderId = selectedWorkspaceId ? null : normalizeKnowledgeFolderId(searchParams.get("folder_id"));
  const currentFolderId = selectedWorkspaceId ? null : (routeFolderId || folderPath[folderPath.length - 1]?.id || null);
  const knowledgeReturnTo = useMemo(() => `${location.pathname}${location.search}`, [location.pathname, location.search]);
  const knowledgeReturnState = useMemo(() => ({ knowledgeReturnTo }), [knowledgeReturnTo]);

  const updateKnowledgeUrl = useCallback(
    (
      updates: {
        folderId?: string | null;
        workspaceId?: string | null;
        section?: LibrarySection;
        type?: FileTypeFilter;
        sort?: SortKey;
        view?: ViewMode;
        search?: string | null;
      },
      replace = false,
    ) => {
      const next = new URLSearchParams(searchParams);
      const setOrDelete = (key: string, value: string | null | undefined, defaultValue?: string) => {
        if (value == null || value === "" || value === defaultValue) next.delete(key);
        else next.set(key, value);
      };

      if (updates.folderId !== undefined) setOrDelete("folder_id", updates.folderId);
      if (updates.workspaceId !== undefined) setOrDelete("workspace_id", updates.workspaceId);
      if (updates.section !== undefined) setOrDelete("section", updates.section, "all");
      if (updates.type !== undefined) setOrDelete("type", updates.type, "all");
      if (updates.sort !== undefined) setOrDelete("sort", updates.sort, "date");
      if (updates.view !== undefined) setOrDelete("view", updates.view, "grid");
      if (updates.search !== undefined) setOrDelete("q", updates.search);
      setSearchParams(next, { replace });
    },
    [searchParams, setSearchParams],
  );

  const navigateToDocument = useCallback(
    (doc: any, mode: "edit" | "view" | "default" = "default") => {
      const canEdit = isEditable(doc) && canEditDoc(doc);
      const path = mode === "view" && isViewable(doc)
        ? `/viewer/${doc.id}`
        : mode === "edit" && canEdit
          ? `/editor/${doc.id}`
          : canEdit
            ? `/editor/${doc.id}`
            : isViewable(doc)
              ? `/viewer/${doc.id}`
              : null;
      if (path) navigate(path, { state: knowledgeReturnState });
    },
    [canEditDoc, knowledgeReturnState, navigate],
  );

  const openDocument = useCallback((doc: any) => {
    if (isViewable(doc)) {
      navigateToDocument(doc, "view");
      return;
    }
    if (isEditable(doc) && canEditDoc(doc)) {
      navigateToDocument(doc, "edit");
      return;
    }
    api.documents.download(doc.id).then((url) => {
      const a = document.createElement("a");
      a.href = url;
      a.download = doc.name;
      a.click();
      URL.revokeObjectURL(url);
    });
  }, [canEditDoc, navigateToDocument]);

  // Inline folder creation
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");

  // Rename dialog
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string; type: "file" | "folder" } | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // Batch selection
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Get Info dialog
  const [infoTarget, setInfoTarget] = useState<any | null>(null);
  const [workspacePickerDoc, setWorkspacePickerDoc] = useState<any | null>(null);
  const [movePickerDoc, setMovePickerDoc] = useState<any | null>(null);

  // Drag and drop for moving files/folders between folders
  const [draggingItem, setDraggingItem] = useState<{ id: string; type: "file" | "folder"; name: string } | null>(null);
  const [uploadingFiles, setUploadingFiles] = useState<string[]>([]);
  const [dragOverTarget, setDragOverTarget] = useState<string | null>(null);

  // Context menu
  const contextMenu = useContextMenu();
  const searchTerm = search.trim();
  const isSearching = searchTerm.length > 0;

  const { data, isLoading } = useQuery({
    queryKey: ["documents-browse", searchTerm, currentFolderId, selectedWorkspaceId, librarySection],
    queryFn: () => api.documents.browse(getKnowledgeBrowseParams({
      currentFolderId,
      librarySection,
      searchTerm,
      selectedWorkspaceId,
    })),
    staleTime: 60_000,
    refetchInterval: (query) => documentListPollInterval(query.state.data),
  });

  const { data: workspaces = [] } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
    staleTime: 60_000,
  });

  const { data: folders = [] } = useQuery({
    queryKey: ["folder-tree"],
    queryFn: () => api.folders.tree(),
    staleTime: 60_000,
  });
  const invalidateDocumentBrowse = useCallback(() => {
    return queryClient.invalidateQueries({ queryKey: ["documents-browse"] });
  }, [queryClient]);
  const invalidateDocumentBrowseAndFolderTree = useCallback(() => {
    return Promise.all([
      queryClient.invalidateQueries({ queryKey: ["documents-browse"] }),
      queryClient.invalidateQueries({ queryKey: ["folder-tree"] }),
    ]);
  }, [queryClient]);
  const canWriteFolderId = useCallback((folderId: string | null | undefined) => {
    if (!folderId) return true;
    const folder = (folders as any[]).find((f: any) => f.id === folderId);
    return canManageFolderItem(folder);
  }, [canManageFolderItem, folders]);

  const { data: wikiIndex } = useQuery({
    queryKey: ["fs-wiki-index"],
    queryFn: () => api.fs.wikiIndex(),
    staleTime: 60_000,
  });

  useEffect(() => {
    const nextSearch = searchParams.get("q") || "";
    const nextSection = pickQueryValue(searchParams.get("section"), LIBRARY_SECTION_VALUES, "all");
    const nextType = pickQueryValue(searchParams.get("type"), FILE_TYPE_FILTER_VALUES, "all");
    const nextSort = pickQueryValue(searchParams.get("sort"), SORT_KEY_VALUES, "date");
    const nextView = pickQueryValue(searchParams.get("view"), VIEW_MODE_VALUES, "grid");
    const nextWorkspaceId = searchParams.get("workspace_id");

    if (nextSearch !== search) setSearch(nextSearch);
    if (nextSection !== librarySection) setLibrarySection(nextSection);
    if (nextType !== fileTypeFilter) setFileTypeFilter(nextType);
    if (nextSort !== sortKey) setSortKey(nextSort);
    if (nextView !== viewMode) setViewMode(nextView);
    if (nextWorkspaceId !== selectedWorkspaceId) {
      setSelectedWorkspaceId(nextWorkspaceId);
      if (nextWorkspaceId) setFolderPath([]);
    }
  }, [fileTypeFilter, librarySection, search, searchParams, selectedWorkspaceId, sortKey, viewMode]);

  useEffect(() => {
    if (selectedWorkspaceId) return;
    const folderId = normalizeKnowledgeFolderId(searchParams.get("folder_id"));
    if (!folderId) {
      if (folderPath.length > 0) setFolderPath([]);
      return;
    }

    const allFolders = folders as any[];
    if (allFolders.length === 0) return;
    const folder = allFolders.find((f: any) => f.id === folderId);
    if (!folder) {
      updateKnowledgeUrl({ folderId: null }, true);
      setFolderPath([]);
      return;
    }

    const nextPath = buildFolderPath(folder, allFolders);
    setFolderPath((prev) => (sameFolderPath(prev, nextPath) ? prev : nextPath));
  }, [folderPath.length, folders, searchParams, selectedWorkspaceId, updateKnowledgeUrl]);

  const createFolderMutation = useMutation({
    mutationFn: (d: { name: string; parent_id?: string }) => api.folders.create(d),
    onSuccess: () => {
      invalidateDocumentBrowseAndFolderTree();
      toast.success(t("page.knowledge.folder_created"));
      setCreatingFolder(false);
      setNewFolderName("");
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_create_folder")); },
  });

  const deleteFolderMutation = useMutation({
    mutationFn: (id: string) => api.folders.delete(id),
    onSuccess: () => {
      invalidateDocumentBrowseAndFolderTree();
      toast.success(t("page.knowledge.folder_deleted"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_delete_folder")); },
  });

  const renameFolderMutation = useMutation({
    mutationFn: (d: { id: string; name: string }) => api.folders.rename(d.id, d.name),
    onSuccess: () => {
      invalidateDocumentBrowseAndFolderTree();
      toast.success(t("page.knowledge.folder_renamed"));
      setRenameTarget(null);
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_rename_folder")); },
  });

  const renameDocMutation = useMutation({
    mutationFn: (d: { id: string; name: string }) => api.documents.rename(d.id, d.name),
    onSuccess: () => {
      invalidateDocumentBrowse();
      toast.success(t("page.knowledge.document_renamed"));
      setRenameTarget(null);
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_rename_document")); },
  });

  const uploadMutation = useMutation({
    mutationFn: ({ files, folderId, options }: {
      files: File[];
      folderId?: string | null;
      options?: UploadOptionsValue;
    }) => {
      setUploadingFiles(files.map((f) => f.name));
      const opts = options
        ? {
            visibility: options.visibility,
            classification: options.classification,
            client_visible: options.client_visible,
          }
        : undefined;
      return Promise.all(files.map((f) => api.documents.upload(f, folderId, opts)));
    },
    onSuccess: (results) => {
      setUploadingFiles([]);
      invalidateDocumentBrowseAndFolderTree();
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      // DLP feedback: if server auto-upgraded classification due to PII, tell the user
      const piiUpgraded = results.filter((d) => d?.pii_detected && d?.classification === "confidential");
      if (piiUpgraded.length > 0) {
        toast.success(
          piiUpgraded.length === 1
            ? t("page.knowledge.pii_upgrade_single", { name: piiUpgraded[0].name })
            : t("page.knowledge.pii_upgrade_multi", { count: piiUpgraded.length }),
        );
      } else {
        toast.success(results.length > 1 ? `${results.length} ${t("page.knowledge.documents_uploaded")}` : t("page.knowledge.document_uploaded"));
      }
    },
    onError: (err: any) => {
      setUploadingFiles([]);
      const msg = err?.status === 413 ? t("page.knowledge.file_too_large_max_500mb") : t("page.knowledge.upload_failed");
      toast.error(msg);
    },
  });

  // ── Upload-options wizard (cloud-drive style: ask before upload) ──
  const [pendingUploadFiles, setPendingUploadFiles] = useState<File[]>([]);
  const [pendingUploadFolderId, setPendingUploadFolderId] = useState<string | null>(null);
  // Folder Properties dialog (Phase B). Holds the target folder; null = closed.
  const [folderPropsTarget, setFolderPropsTarget] = useState<DocumentFolderInfo | null>(null);
  // Folder ShareDialog target — opens the existing Phase A dialog scoped
  // to a folder. Handlers route to api.folderPermissions.* instead of
  // api.docPermissions.*. Null = closed.
  const [folderShareTarget, setFolderShareTarget] = useState<DocumentFolderInfo | null>(null);
  // Document ShareDialog target — same dialog, scoped to a single document.
  // Lets users manage access from the Knowledge list without first opening
  // the file viewer. Null = closed.
  const [docShareTarget, setDocShareTarget] = useState<Document | null>(null);
  // Document properties dialog target — change visibility / classification /
  // client_visible on a single file (mirrors folder properties).
  const [docPropsTarget, setDocPropsTarget] = useState<Document | null>(null);
  const uploadDialogOpen = pendingUploadFiles.length > 0;

  const [googleDriveLoading, setGoogleDriveLoading] = useState(false);

  const handleGoogleDrivePick = useCallback(async () => {
    if (!canUploadKnowledge) return;
    if (!canWriteFolderId(currentFolderId)) return;
    if (!isGoogleDriveConfigured()) {
      toast.error(t("page.knowledge.google_drive_is_not_configured_set_vite_google_c"));
      return;
    }
    setGoogleDriveLoading(true);
    try {
      const file = await pickFile();
      if (!file) return; // user cancelled
      const doc = await api.documents.uploadFromGoogleDrive({
        file_id: file.id,
        name: file.name,
        mime_type: file.mimeType,
        file_size: file.size,
        modified_time: file.modifiedTime,
        access_token: file.accessToken,
        folder_id: currentFolderId,
      });
      invalidateDocumentBrowseAndFolderTree();
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      toast.success(`"${doc.name}" ${t("page.knowledge.added_from_google_drive")}`);
    } catch (err: any) {
      toast.error(err?.message || t("page.knowledge.failed_to_add_from_google_drive"));
    } finally {
      setGoogleDriveLoading(false);
    }
  }, [canUploadKnowledge, canWriteFolderId, currentFolderId, invalidateDocumentBrowseAndFolderTree, queryClient, toast]);

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.documents.delete(id),
    onSuccess: () => {
      invalidateDocumentBrowseAndFolderTree();
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      toast.success(t("page.knowledge.document_deleted"));
    },
  });

  const moveMutation = useMutation({
    mutationFn: (d: { id: string; folder_id: string | null }) => api.documents.move(d.id, d.folder_id),
    onSuccess: () => {
      invalidateDocumentBrowseAndFolderTree();
      toast.success(t("page.knowledge.document_moved"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_move_document")); },
  });

  const moveFolderMutation = useMutation({
    mutationFn: (d: { id: string; parent_id: string | null }) => api.folders.move(d.id, d.parent_id),
    onSuccess: () => {
      invalidateDocumentBrowseAndFolderTree();
      toast.success(t("page.knowledge.folder_moved"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_move_folder")); },
  });

  // ── Drag-and-drop helpers for moving files/folders ──
  const handleDragStartItem = useCallback((e: React.DragEvent, item: { id: string; type: "file" | "folder"; name: string }) => {
    setDraggingItem(item);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", item.id);
  }, []);

  const handleDragEndItem = useCallback(() => {
    setDraggingItem(null);
    setDragOverTarget(null);
  }, []);

  const handleDropOnFolder = useCallback((e: React.DragEvent, targetFolderId: string | null) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOverTarget(null);
    if (!draggingItem) return;
    // Don't drop a folder onto itself
    if (draggingItem.type === "folder" && draggingItem.id === targetFolderId) return;
    // Don't drop into the same folder it's already in
    if (draggingItem.type === "file") {
      const doc = ((data?.items || []) as any[]).find((d: any) => d.id === draggingItem.id);
      if (!doc || !canManageDocMetadata(doc)) return;
      moveMutation.mutate({ id: draggingItem.id, folder_id: targetFolderId });
    }
    // For folders: also call move (files only per spec, but we support it)
    if (draggingItem.type === "folder") {
      const folder = (folders as any[]).find((f: any) => f.id === draggingItem.id);
      if (!folder || !canManageFolderItem(folder)) return;
      moveFolderMutation.mutate({ id: draggingItem.id, parent_id: targetFolderId });
    }
    setDraggingItem(null);
  }, [canManageDocMetadata, canManageFolderItem, data?.items, draggingItem, folders, moveMutation, moveFolderMutation]);

  const handleDragOverFolder = useCallback((e: React.DragEvent, folderId: string | null) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverTarget(folderId ?? "__root__");
  }, []);

  const handleDragLeaveFolder = useCallback((e: React.DragEvent) => {
    // Only clear if actually leaving the element (not entering a child)
    const related = e.relatedTarget as Node | null;
    if (related && (e.currentTarget as Node).contains(related)) return;
    setDragOverTarget(null);
  }, []);

  // Create blank document
  const createBlankMutation = useMutation({
    mutationFn: (d: { name: string; file_type?: string }) => api.documents.createBlank(d),
    onSuccess: (doc: any) => {
      invalidateDocumentBrowse();
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      toast.success(t("page.knowledge.blank_document_created"));
      setShowCreateBlankModal(false);
      setBlankDocName("");
      setBlankDocType("md");
      if (isEditable(doc)) {
        navigate(`/editor/${doc.id}`, { state: knowledgeReturnState });
      }
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_create_document")); },
  });

  // Import from URL
  const importFromUrlMutation = useMutation({
    mutationFn: (d: { url: string; name?: string }) => api.documents.createFromUrl(d),
    onSuccess: () => {
      invalidateDocumentBrowse();
      toast.success(t("page.knowledge.document_imported_from_url"));
      setShowImportUrlModal(false);
      setImportUrl("");
      setImportUrlName("");
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_import_from_url")); },
  });

  // AI Draft
  const aiDraftMutation = useMutation({
    mutationFn: (d: { prompt: string; file_type?: string; name?: string }) => api.documents.aiDraft(d),
    onSuccess: () => {
      invalidateDocumentBrowse();
      toast.success(t("page.knowledge.ai_draft_is_being_generated"));
      setShowAiDraftModal(false);
      setAiDraftPrompt("");
      setAiDraftName("");
      setAiDraftType("md");
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_create_ai_draft")); },
  });

  // Favorites for documents
  const { data: docFavorites = [] } = useQuery({
    queryKey: ["favorites", "document"],
    queryFn: () => api.favorites.list("document"),
    staleTime: 60_000,
  });
  const favoriteDocIds = useMemo(() => new Set((docFavorites as any[]).map((f: any) => f.resource_id)), [docFavorites]);

  // Favorite toggle
  const favoriteMutation = useMutation({
    mutationFn: (docId: string) => api.favorites.toggle("document", docId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["favorites", "document"] });
    },
  });

  // Reindex / cancel index
  const reindexMutation = useMutation({
    mutationFn: (docId: string) => api.documents.reindexOne(docId),
    onSuccess: () => {
      invalidateDocumentBrowse();
      toast.success(t("page.knowledge.re_indexing_started"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_start_re_indexing")); },
  });
  const cancelIndexMutation = useMutation({
    mutationFn: (docId: string) => api.documents.cancelIndex(docId),
    onSuccess: () => {
      invalidateDocumentBrowse();
      toast.success(t("page.knowledge.indexing_cancelled"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_cancel_indexing")); },
  });

  // Trash operations
  const { data: trashData = [], isLoading: isLoadingTrash } = useQuery({
    queryKey: ["documents-trash"],
    queryFn: () => api.documents.listTrash(),
    staleTime: 60_000,
  });

  const trashMutation = useMutation({
    mutationFn: ({ id }: { id: string; silent?: boolean }) => api.documents.trash(id),
    onSuccess: (_result, { silent = false }) => {
      invalidateDocumentBrowseAndFolderTree();
      queryClient.invalidateQueries({ queryKey: ["documents-trash"] });
      if (!silent) toast.success(t("page.knowledge.moved_to_trash"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_move_to_trash")); },
  });

  const restoreMutation = useMutation({
    mutationFn: (id: string) => api.documents.restore(id),
    onSuccess: () => {
      invalidateDocumentBrowseAndFolderTree();
      queryClient.invalidateQueries({ queryKey: ["documents-trash"] });
      toast.success(t("page.knowledge.document_restored"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_restore_document")); },
  });

  const emptyTrashMutation = useMutation({
    mutationFn: () => api.documents.emptyTrash(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents-trash"] });
      toast.success(t("page.knowledge.trash_emptied"));
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_empty_trash")); },
  });

  const addToWorkspaceMutation = useMutation({
    mutationFn: async ({ docId, workspace }: { docId: string; workspace: any }) => {
      // Add user-selected documents to a workspace RAG collection. Generated
      // artifacts are registered as Documents separately through provenance metadata.
      const groups = await api.workspaces.documents(workspace.id);
      let groupId: string | undefined = (
        groups.find((g: any) => g.is_default_collection)
        || groups.find((g: any) => !g.is_workspace_file_bucket)
      )?.id;
      if (!groupId) {
        const newGroup = await api.workspaces.knowledge.createGroup(workspace.id, {
          name: t("page.workspace_detail.workspace_knowledge"),
          kind: "workspace_collection",
          purpose: t("page.knowledge.general_workspace_knowledge_available"),
        });
        groupId = newGroup.id;
      }
      if (!groupId) throw new Error(t("page.knowledge.no_workspace_knowledge_collection_available"));
      await api.workspaces.knowledge.addDocuments(workspace.id, groupId, [docId]);
    },
    onSuccess: (_, { docId, workspace }) => {
      queryClient.invalidateQueries({ queryKey: ["doc-workspaces", docId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-doc-ids", workspace.id] });
      queryClient.invalidateQueries({ queryKey: ["workspace-documents", workspace.id] });
      toast.success(`${t("page.knowledge.added_to_workspace")} "${workspace.name}"`);
    },
    onError: () => { toast.error(t("page.knowledge.failed_to_add_document_to_workspace")); },
  });

  // Quick Look content
  const { data: quickLookContent } = useQuery({
    queryKey: ["document-content", quickLookDoc?.id],
    queryFn: () => api.documents.getContent(quickLookDoc!.id),
    enabled: !!quickLookDoc && isEditable(quickLookDoc),
  });

  const isQuickLookMarkdown = Boolean(
    quickLookDoc
    && ["md", "markdown"].includes(String(quickLookDoc.file_type || quickLookDoc.name?.split(".").pop() || "").toLowerCase()),
  );

  const { data: quickLookWikiLinks } = useQuery({
    queryKey: ["fs-wiki-links", quickLookDoc?.fs_path],
    queryFn: () => api.fs.wikiLinks(quickLookDoc!.fs_path!),
    enabled: Boolean(quickLookDoc?.fs_path && isQuickLookMarkdown),
  });

  const openWikiDocument = useCallback(
    (link: WikiLinkInfo, target: string) => {
      if (link.document_id) {
        navigate(`/editor/${link.document_id}`, { state: knowledgeReturnState });
        setQuickLookDoc(null);
        return;
      }
      setSearch(target);
      updateKnowledgeUrl({ search: target, section: "all" }, true);
    },
    [knowledgeReturnState, navigate, updateKnowledgeUrl],
  );

  // Plan storage limit (entity-wide). When at/over the limit we block every
  // "add to knowledge" action up front and show the upgrade overlay, matching
  // the backend's 402 enforcement.
  const storageOver = useMemo(() => {
    const limit = data?.storage_limit_mb;
    const used = data?.storage_used_mb;
    return limit != null && used != null && used >= limit;
  }, [data?.storage_limit_mb, data?.storage_used_mb]);

  const guardStorage = useCallback(() => {
    if (!storageOver) return false;
    useUpgradeStore.getState().show({
      message: t("page.knowledge.storage_full"),
      limit: data?.storage_limit_mb ?? null,
      current: data?.storage_used_mb ?? null,
      plan: "current",
      kind: "storage",
    });
    return true;
  }, [storageOver, data?.storage_limit_mb, data?.storage_used_mb]);

  const handleFiles = useCallback(
    (files: FileList | File[] | null, folderId: string | null = currentFolderId) => {
      if (!canUploadKnowledge) return;
      if (guardStorage()) return;
      if (!canWriteFolderId(folderId)) return;
      if (!files || files.length === 0) return;
      // Stage the files; the dialog opens automatically and drives the upload.
      setPendingUploadFiles(Array.from(files));
      setPendingUploadFolderId(folderId);
    },
    [canUploadKnowledge, canWriteFolderId, currentFolderId, guardStorage],
  );

  const openFilePicker = useCallback((folderId: string | null = currentFolderId) => {
    if (!canUploadKnowledge) return;
    if (guardStorage()) return;
    if (!canWriteFolderId(folderId)) return;
    filePickerFolderIdRef.current = folderId;
    fileInputRef.current?.click();
  }, [canUploadKnowledge, canWriteFolderId, currentFolderId, guardStorage]);

  const handleDrop = useCallback(
    (e: React.DragEvent, folderId: string | null = currentFolderId) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(false);
      setDragOverTarget(null);
      handleFiles(e.dataTransfer.files, folderId);
    },
    [currentFolderId, handleFiles],
  );

  // Focus inline folder input
  useEffect(() => {
    if (creatingFolder && inlineFolderRef.current) {
      inlineFolderRef.current.focus();
    }
  }, [creatingFolder]);


  const newMenuItems = [
    {
      key: "new-folder",
      label: t("page.knowledge.new_folder"),
      icon: <IconFolder size={16} className="text-amber-500" />,
      disabled: !canUploadKnowledge,
    },
    {
      key: "new-wiki-page",
      label: t("page.knowledge.new_wiki_page"),
      icon: <IconLink size={16} className="text-manor-500" />,
      disabled: !canUploadKnowledge,
    },
    {
      key: "upload-file",
      label: t("page.knowledge.upload_file"),
      icon: <IconUpload size={16} className="text-sky-500" />,
      disabled: !canUploadKnowledge,
    },
    {
      key: "new-blank-document",
      label: t("page.knowledge.new_blank_document"),
      icon: <IconDocument size={16} className="text-manor-500" />,
      disabled: !canUploadKnowledge,
    },
    {
      key: "ai-draft",
      label: t("page.knowledge.ai_draft"),
      icon: <IconEdit size={16} className="text-purple-500" />,
      disabled: !canUploadKnowledge,
    },
    {
      key: "import-url",
      label: t("page.knowledge.import_from_url"),
      icon: <IconLink size={16} className="text-violet-500" />,
      disabled: !canUploadKnowledge,
    },
    {
      key: "google-drive",
      label: googleDriveLoading ? t("component.chat_input_footer.connecting") : t("page.knowledge.google_drive"),
      icon: (
        <svg width={16} height={16} viewBox="0 0 87.3 78" xmlns="http://www.w3.org/2000/svg">
          <path d="M6.6 66.85l3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8H1.2c0 1.55.4 3.1 1.2 4.5l4.2 9.35z" fill="#0066DA"/>
          <path d="M43.65 25.15L29.9 1.35C28.55 2.15 27.4 3.25 26.6 4.65L1.2 48.5c-.8 1.4-1.2 2.95-1.2 4.5h27.5l16.15-27.85z" fill="#00AC47"/>
          <path d="M73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5H59.8l6.85 11.85 6.9 11.95z" fill="#EA4335"/>
          <path d="M43.65 25.15L57.4 1.35C56.05.55 54.5 0 52.85 0H34.45c-1.65 0-3.2.55-4.55 1.35l13.75 23.8z" fill="#00832D"/>
          <path d="M59.8 53H27.5L13.75 76.8c1.35.8 2.9 1.2 4.55 1.2h36.7c1.65 0 3.2-.45 4.55-1.2L59.8 53z" fill="#2684FC"/>
          <path d="M73.4 26.5l-12.7-22c-.8-1.4-1.95-2.5-3.3-3.3L43.65 25.15 59.8 53h27.5c0-1.55-.4-3.1-1.2-4.5L73.4 26.5z" fill="#FFBA00"/>
        </svg>
      ),
      disabled: googleDriveLoading || !canUploadKnowledge,
    },
  ];

  const foldersAtLevel = useMemo(() => {
    if (selectedWorkspaceId) return [];
    return data?.folders || [];
  }, [data?.folders, selectedWorkspaceId]);

  const folderPathLabelForDocument = useCallback((doc: Pick<Document, "folder_id">) => {
    const folderId = doc.folder_id || null;
    if (!folderId) return t("page.knowledge.all_files");
    const folder = (folders as any[]).find((f: any) => f.id === folderId);
    if (!folder) return "";
    return buildFolderPath(folder, folders as any[]).map((entry) => entry.name).join(" / ");
  }, [folders]);

  // Derived: documents
  const allDocuments = useMemo(() => getKnowledgeDocumentsForView({
    documents: data?.documents || data?.items || [],
    currentFolderId,
    favoriteDocIds,
    fileTypeFilter,
    isSearching,
    librarySection,
    selectedWorkspaceId,
    sortKey,
  }), [data?.documents, data?.items, currentFolderId, favoriteDocIds, fileTypeFilter, isSearching, librarySection, selectedWorkspaceId, sortKey]);

  const wikiPages = useMemo<WikiMapPage[]>(() => (
    dedupeWikiPages(Array.isArray((wikiIndex as any)?.pages) ? (wikiIndex as any).pages : [])
  ), [wikiIndex]);
  const wikiMissingLinks = useMemo(() => (
    Array.isArray((wikiIndex as any)?.missing_links) ? (wikiIndex as any).missing_links : []
  ), [wikiIndex]);

  // Storage total — use the backend's recursive total (files in this location
  // plus every nested subfolder), falling back to summing the current page for
  // older API responses that don't return it.
  const totalStorage = useMemo(
    () =>
      data?.total_size ??
      (data?.items || []).reduce((sum, doc) => sum + (doc.file_size || 0), 0),
    [data?.total_size, data?.items],
  );
  const totalFiles = data?.total_files ?? data?.total ?? 0;

  // Counts
  const folderCount = foldersAtLevel.length;
  const fileCount = allDocuments.length;
  const currentFolderWritable = canWriteFolderId(currentFolderId);
  const canAddToCurrentFolder = canUploadKnowledge && currentFolderWritable;

  // Navigate into a folder. Rebuild the full breadcrumb from the folder's
  // ancestor chain — search results can surface folders from any depth, not
  // just children of the current level.
  const enterFolder = (folder: any) => {
    setFolderPath(buildFolderPath((folders as any[]).find((f: any) => f.id === folder.id) ?? folder, folders as any[]));
    setSelectedWorkspaceId(null);
    setLibrarySection("all");
    if (search) setSearch("");
    updateKnowledgeUrl({ folderId: folder.id, workspaceId: null, section: "all", search: null });
  };

  // Navigate breadcrumb
  const navigateBreadcrumb = (index: number) => {
    if (index < 0) {
      setFolderPath([]);
      updateKnowledgeUrl({ folderId: null });
    } else {
      const nextPath = folderPath.slice(0, index + 1);
      setFolderPath(nextPath);
      updateKnowledgeUrl({ folderId: nextPath[nextPath.length - 1]?.id || null });
    }
  };

  const handleFileTypeFilter = useCallback((type: FileTypeFilter) => {
    setFileTypeFilter(type);
    updateKnowledgeUrl({ type });
  }, [updateKnowledgeUrl]);

  const handleSortChange = useCallback((sort: SortKey) => {
    setSortKey(sort);
    updateKnowledgeUrl({ sort });
  }, [updateKnowledgeUrl]);

  const handleViewModeChange = useCallback((view: ViewMode) => {
    setViewMode(view);
    updateKnowledgeUrl({ view });
  }, [updateKnowledgeUrl]);

  const handleSearchChange = useCallback((value: string) => {
    setSearch(value);
    updateKnowledgeUrl({ search: value }, true);
  }, [updateKnowledgeUrl]);

  const startCreateWikiPage = useCallback((name = "") => {
    const cleanName = name.trim().replace(/\.md$/i, "");
    const leafName = cleanName.split("/").filter(Boolean).pop() || cleanName || "Untitled Wiki Page";
    setBlankDocName(leafName);
    setBlankDocType("md");
    setShowCreateBlankModal(true);
  }, []);

  const openWikiPage = useCallback(
    (page: WikiMapPage) => {
      if (page?.document_id) {
        navigate(`/editor/${page.document_id}`, { state: knowledgeReturnState });
        return;
      }
      const q = page?.title || page?.path || "";
      if (q) handleSearchChange(q);
    },
    [handleSearchChange, knowledgeReturnState, navigate],
  );

  const connectWikiPagesMutation = useMutation({
    mutationFn: async ({ source, target }: { source: WikiMapPage; target: WikiMapPage }) => {
      if (!source.document_id) throw new Error(t("page.knowledge.wiki_map_source_missing_document"));
      if (source.path === target.path) return { already: true };
      const alreadyLinked = (source.links || []).some((link) =>
        link.resolved_path === target.path || (target.document_id && link.document_id === target.document_id)
      );
      if (alreadyLinked) return { already: true };

      const rawTitle = String(target.title || target.document_name || target.path || "").replace(/\.md$/i, "");
      const safeTitle = rawTitle.replace(/[\[\]\r\n]/g, " ").trim();
      if (!safeTitle) throw new Error(t("page.knowledge.wiki_map_target_missing_title"));

      const current = await api.documents.getContent(source.document_id);
      const content = current?.content || "";
      const nextContent = `${content.trimEnd()}${content.trim() ? "\n\n" : ""}[[${safeTitle}]]\n`;
      await api.documents.saveContent(source.document_id, nextContent);
      return { already: false, title: safeTitle, sourcePath: source.path };
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      if (result.sourcePath) queryClient.invalidateQueries({ queryKey: ["fs-wiki-links", result.sourcePath] });
      if (result.already) toast.info(t("page.knowledge.wiki_map_already_linked"));
      else toast.success(
        t("page.knowledge.wiki_map_link_created"),
        result.title ? `${t("page.knowledge.wiki_map_added_link_prefix")} [[${result.title}]] ${t("page.knowledge.wiki_map_added_link_suffix")}` : undefined,
      );
    },
    onError: (err: any) => {
      toast.error(t("page.knowledge.wiki_map_failed_to_create_link"), err?.message || t("page.knowledge.wiki_map_try_again"));
    },
  });

  const removeWikiLinkMutation = useMutation({
    mutationFn: async ({ source, link }: { source: WikiMapPage; link: WikiMapLink }) => {
      if (!source.document_id) throw new Error(t("page.knowledge.wiki_map_source_missing_document"));
      const target = String(link.target || "").trim();
      if (!target) throw new Error(t("page.knowledge.wiki_map_remove_link_missing_target"));

      const current = await api.documents.getContent(source.document_id);
      const content = current?.content || "";
      const wikiLinkPattern = new RegExp(`\\[\\[\\s*${escapeRegExp(target)}\\s*(?:\\|[^\\]]*)?\\]\\]`);
      if (!wikiLinkPattern.test(content)) throw new Error(t("page.knowledge.wiki_map_remove_link_not_found"));

      const nextContent = content
        .replace(wikiLinkPattern, "")
        .replace(/[ \t]+\n/g, "\n")
        .replace(/\n{3,}/g, "\n\n");
      await api.documents.saveContent(source.document_id, nextContent);
      return { sourcePath: source.path, title: target };
    },
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] });
      if (result.sourcePath) queryClient.invalidateQueries({ queryKey: ["fs-wiki-links", result.sourcePath] });
      toast.success(t("page.knowledge.wiki_map_link_removed"), `[[${result.title}]]`);
    },
    onError: (err: any) => {
      toast.error(t("page.knowledge.wiki_map_failed_to_remove_link"), err?.message || t("page.knowledge.wiki_map_try_again"));
    },
  });

  const handleConnectWikiPages = useCallback(
    (source: WikiMapPage, target: WikiMapPage) => {
      connectWikiPagesMutation.mutate({ source, target });
    },
    [connectWikiPagesMutation],
  );

  const handleRemoveWikiLink = useCallback(
    (source: WikiMapPage, link: WikiMapLink) => {
      const label = String(link.target || "").trim();
      if (window.confirm(`${t("page.knowledge.wiki_map_confirm_remove_link")} [[${label}]]?`)) {
        removeWikiLinkMutation.mutate({ source, link });
      }
    },
    [removeWikiLinkMutation],
  );

  // Start inline folder creation
  const startCreateFolder = () => {
    if (!canUploadKnowledge) return;
    if (!canWriteFolderId(currentFolderId)) return;
    setCreatingFolder(true);
    setNewFolderName(t("page.knowledge.untitled_folder"));
  };

  const commitCreateFolder = () => {
    if (!canUploadKnowledge || !canWriteFolderId(currentFolderId)) {
      setCreatingFolder(false);
      setNewFolderName("");
      return;
    }
    const name = newFolderName.trim();
    if (!name) {
      setCreatingFolder(false);
      setNewFolderName("");
      return;
    }
    createFolderMutation.mutate({ name, parent_id: currentFolderId || undefined });
  };

  // Context menu builders
  const buildFileContextMenu = (doc: any): MenuItem[] => {
    const hasMoveTargets = doc.folder_id || (folders as any[]).some((f: any) => f.id !== doc.folder_id);
    const canEdit = canEditDoc(doc);
    const canShare = canShareDoc(doc);
    const canManageMetadata = canManageDocMetadata(doc);
    const canDelete = canDeleteDoc(doc);
    const canAddToWorkspace = canManageAllDocuments && canManageMetadata;

    const indexItem: MenuItem = isVectorInProgress(doc.vector_status)
      ? { label: t("page.knowledge.cancel_indexing"), icon: <IconClose size={14} />, onClick: () => cancelIndexMutation.mutate(doc.id) }
      : { label: doc.vector_status === VectorStatus.READY || doc.vector_status === VectorStatus.INDEXED ? t("page.knowledge.re_index") : t("page.knowledge.index_now"), icon: <IconRefresh size={14} />, onClick: () => reindexMutation.mutate(doc.id) };

    const items: MenuItem[] = [
      {
        label: isViewable(doc) ? t("page.knowledge.view") : (isEditable(doc) && canEdit) ? t("page.knowledge.open_in_editor") : t("page.knowledge.download"),
        icon: isViewable(doc) ? <IconDocument size={14} /> : (isEditable(doc) && canEdit) ? <IconExternalLink size={14} /> : <IconDocument size={14} />,
        onClick: () => openDocument(doc),
      },
      ...(isViewable(doc) && isEditable(doc) && canEdit ? [{
        label: t("page.knowledge.open_in_editor"),
        icon: <IconExternalLink size={14} />,
        onClick: () => navigateToDocument(doc, "edit"),
      } as MenuItem] : []),
      ...((isVideoFile(doc.name, doc.file_type) || isVideoProjectFile(doc.name)) && canEdit ? [{
        label: "Open in video editor",
        icon: <IconExternalLink size={14} />,
        onClick: () => navigate(`/video-editor/${doc.id}`, { state: knowledgeReturnState }),
      } as MenuItem] : []),
      { label: t("page.knowledge.quick_look"), icon: <IconEye size={14} />, onClick: () => setQuickLookDoc(doc) },
      { label: favoriteDocIds.has(doc.id) ? t("page.knowledge.remove_from_favorites") : t("page.knowledge.add_to_favorites"), icon: <IconStar size={14} />, onClick: () => favoriteMutation.mutate(doc.id) },
      { label: t("page.knowledge.get_info"), icon: <IconInfo size={14} />, onClick: () => setInfoTarget(doc) },
      { label: t("page.knowledge.download"), icon: <IconDownload size={14} />, onClick: () => { api.documents.download(doc.id).then((url) => { const a = document.createElement("a"); a.href = url; a.download = doc.name; a.click(); URL.revokeObjectURL(url); }); } },
    ];
    const managedItems: MenuItem[] = [];
    if (canManageMetadata) {
      managedItems.push(
        { label: t("page.knowledge.rename"), icon: <IconText size={14} />, onClick: () => { setRenameTarget({ id: doc.id, name: doc.name, type: "file" }); setRenameValue(doc.name); } },
        { label: "", divider: true },
      );
      if (canAddToWorkspace) {
        managedItems.push({ label: t("page.knowledge.add_to_workspace"), icon: <IconWorkspace size={14} />, onClick: () => setWorkspacePickerDoc(doc) });
      }
      managedItems.push(indexItem);
      if (hasMoveTargets) {
        managedItems.push({ label: t("page.knowledge.move_to_folder_2"), icon: <IconFolder size={14} />, onClick: () => setMovePickerDoc(doc) });
      }
    }
    if (canShare || canManageMetadata || canDelete) {
      managedItems.push({ label: "", divider: true });
    }
    if (canShare) {
      managedItems.push({ label: t("page.knowledge.share_file"), icon: <IconLink size={14} />, onClick: () => setDocShareTarget(doc) });
    }
    if (canManageMetadata) {
      managedItems.push({ label: t("page.knowledge.file_properties"), icon: <IconEdit size={14} />, onClick: () => setDocPropsTarget(doc) });
    }
    if (!managedItems.length && !canDelete) return items;
    return [
      ...items.slice(0, -2),
      ...managedItems,
      ...items.slice(-2),
      ...(canDelete ? [{ label: t("page.knowledge.move_to_trash"), icon: <IconTrash size={14} />, danger: true, onClick: () => trashMutation.mutate({ id: doc.id }) } as MenuItem] : []),
    ];
  };

  const buildFolderContextMenu = (folder: any): MenuItem[] => {
    const canManage = canManageFolderItem(folder);
    const canShare = canShareFolderItem(folder);
    const items: MenuItem[] = [
      {
        label: t("page.notifications.open"),
        icon: <IconFolder size={14} />,
        onClick: () => enterFolder(folder),
      },
    ];
    if (!canManage && !canShare) return items;
    const managedItems: MenuItem[] = [];
    if (canManage) {
      managedItems.push(
        {
          label: t("page.knowledge.rename"),
          icon: <IconText size={14} />,
          onClick: () => {
            setRenameTarget({ id: folder.id, name: folder.name, type: "folder" });
            setRenameValue(folder.name);
          },
        },
        {
          label: t("page.knowledge.folder_properties"),
          icon: <IconInfo size={14} />,
          onClick: () => setFolderPropsTarget(folder),
        },
      );
    }
    if (canShare) {
      managedItems.push({
        label: t("page.knowledge.share_folder"),
        icon: <IconShare size={14} />,
        onClick: () => setFolderShareTarget(folder),
      });
    }
    if (canManage) {
      managedItems.push(
        { label: "", divider: true },
        {
          label: t("page.knowledge.move_to_trash"),
          icon: <IconTrash size={14} />,
          danger: true,
          onClick: () => deleteFolderMutation.mutate(folder.id),
        },
      );
    }
    return [
      ...items,
      ...managedItems,
    ];
  };

  const buildEmptySpaceContextMenu = (): MenuItem[] => [
    ...(canUploadKnowledge ? [
      {
        label: t("page.knowledge.new_folder"),
        icon: <IconFolder size={14} />,
        onClick: startCreateFolder,
      },
      {
        label: t("page.knowledge.upload_file"),
        icon: <IconUpload size={14} />,
        onClick: () => openFilePicker(),
      },
      { label: "", divider: true },
    ] as MenuItem[] : []),
    {
      label: t("page.knowledge.sort_by_name"),
      icon: <IconList size={14} />,
      onClick: () => handleSortChange("name"),
    },
    {
      label: t("page.knowledge.sort_by_date"),
      icon: <IconList size={14} />,
      onClick: () => handleSortChange("date"),
    },
  ];

  // ── Batch selection helpers ──
  const toggleSelectMode = useCallback(() => {
    setSelectMode((prev) => {
      if (prev) setSelectedIds(new Set());
      return !prev;
    });
  }, []);

  const toggleSelection = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const allSelectableIds = useMemo(() => {
    const ids: string[] = [];
    foldersAtLevel.forEach((f: any) => {
      if (canManageFolderItem(f)) ids.push(f.id);
    });
    allDocuments.forEach((d: any) => {
      if (canManageDocMetadata(d) || canDeleteDoc(d)) ids.push(d.id);
    });
    return ids;
  }, [allDocuments, canDeleteDoc, canManageDocMetadata, canManageFolderItem, foldersAtLevel]);

  const selectAll = useCallback(() => {
    setSelectedIds(new Set(allSelectableIds));
  }, [allSelectableIds]);

  const deselectAll = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const handleBatchTrash = useCallback(async () => {
    const selected = Array.from(selectedIds);
    const docIds = selected.filter((id) => allDocuments.some((d: any) => d.id === id && canDeleteDoc(d)));
    const folderIds = selected.filter((id) => foldersAtLevel.some((f: any) => f.id === id && canManageFolderItem(f)));
    if (docIds.length === 0 && folderIds.length === 0) {
      setSelectedIds(new Set());
      setSelectMode(false);
      return;
    }

    const results = await Promise.allSettled([
      ...docIds.map((id) => trashMutation.mutateAsync({ id, silent: true })),
      ...folderIds.map((id) => deleteFolderMutation.mutateAsync(id)),
    ]);

    const failed = results.filter((r) => r.status === "rejected").length;
    if (failed > 0) {
      toast.error(t("page.knowledge.failed_to_move_selected_to_trash").replace("{count}", String(failed)));
    }

    setSelectedIds(new Set());
    setSelectMode(false);
  }, [canDeleteDoc, canManageFolderItem, selectedIds, allDocuments, foldersAtLevel, trashMutation, deleteFolderMutation, toast]);

  const handleBatchMove = useCallback(async (targetFolderId: string | null) => {
    const selected = Array.from(selectedIds);
    const docIds = selected.filter((id) => allDocuments.some((d: any) => d.id === id && canManageDocMetadata(d)));
    const folderIds = selected.filter((id) =>
      foldersAtLevel.some((f: any) => f.id === id && canManageFolderItem(f)) && id !== targetFolderId
    );
    if (docIds.length === 0 && folderIds.length === 0) {
      setSelectedIds(new Set());
      setSelectMode(false);
      return;
    }
    await Promise.allSettled([
      ...docIds.map((id) => moveMutation.mutateAsync({ id, folder_id: targetFolderId })),
      ...folderIds.map((id) => moveFolderMutation.mutateAsync({ id, parent_id: targetFolderId })),
    ]);
    setSelectedIds(new Set());
    setSelectMode(false);
  }, [canManageDocMetadata, canManageFolderItem, selectedIds, allDocuments, foldersAtLevel, moveMutation, moveFolderMutation]);

  // Breadcrumbs
  const breadcrumbs = useMemo(() => [
    { label: t("page.knowledge.all_files"), onClick: () => navigateBreadcrumb(-1) },
    ...folderPath.map((fp, i) => ({
      label: fp.name,
      onClick: () => navigateBreadcrumb(i),
    })),
  ], [folderPath]);

  const batchActionBar = selectedIds.size > 0 ? (
    <div className="kb-batch-bar">
      <span>{selectedIds.size} {t("page.onboarding.selected")}</span>
      <div style={{ width: 1, height: 20, background: "rgba(255,255,255,0.15)" }} />
      <Select
        value=""
        onChange={(v) => {
          if (v) {
            handleBatchMove(v === "__root__" ? null : v);
          }
        }}
        options={[
          { value: "__root__", label: t("page.knowledge.root_no_folder") },
          ...(foldersAtLevel as any[])
            .filter((f: any) => !selectedIds.has(f.id) && canManageFolderItem(f))
            .map((f: any) => ({ value: f.id, label: f.name })),
        ]}
        placeholder={t("page.knowledge.move_to_folder")}
        style={{ minWidth: 160 }}
      />
      <button className="kb-batch-btn-danger" onClick={handleBatchTrash}>{t("page.knowledge.move_to_trash")}</button>
      <button className="kb-batch-btn-ghost" onClick={() => { setSelectedIds(new Set()); setSelectMode(false); }}>{t("action.cancel")}</button>
    </div>
  ) : null;

  return (
    <div className="knowledge-page flex min-h-[calc(100vh-11rem)] w-full gap-0 overflow-hidden">
      <style>{STYLES}</style>

      {/* ── Left Sidebar ─────────────────────────── */}
      {!sidebarCollapsed && (
        <div
          className="w-[260px] flex-shrink-0 pr-4 flex flex-col overflow-y-auto h-full"
          style={{ borderRight: "1px solid var(--manor-border)" }}
        >
          {/* Sidebar header */}
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-[14px] font-normal text-stone-700 tracking-normal normal-case leading-5 m-0">{t("page.knowledge.library")}</h2>
            <button onClick={() => setSidebarCollapsed(true)} className="kb-collapse-btn">
              <IconChevronLeft size={14} />
            </button>
          </div>

          {/* Library sections */}
          <SidebarSection title={t("page.knowledge.library")} defaultOpen={false}>
            {SIDEBAR_SECTIONS.map((section) => (
              <SidebarLink
                key={section.key}
                label={section.label}
                icon={section.icon}
                isActive={librarySection === section.key && folderPath.length === 0}
                count={section.key === "all" && data ? data.total : undefined}
                onClick={() => {
                  setLibrarySection(section.key);
                  setSelectedWorkspaceId(null);
                  setFolderPath([]);
                  updateKnowledgeUrl({ section: section.key, folderId: null, workspaceId: null });
                }}
              />
            ))}
            <SidebarLink
              label={t("page.workspaces.trash")}
              icon="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"
              isActive={librarySection === "trash"}
              count={Array.isArray(trashData) ? trashData.length : undefined}
              onClick={() => {
                setLibrarySection("trash");
                setSelectedWorkspaceId(null);
                setFolderPath([]);
                updateKnowledgeUrl({ section: "trash", folderId: null, workspaceId: null });
              }}
            />
          </SidebarSection>

          {/* Folders */}
          <div className="mt-4">
            <SidebarSection title={t("page.knowledge.folders")}>
              <SidebarLink
                label={t("page.knowledge.all_files")}
                icon="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"
                isActive={folderPath.length === 0}
                onClick={() => {
                  setFolderPath([]);
                  setLibrarySection("all");
                  setSelectedWorkspaceId(null);
                  updateKnowledgeUrl({ folderId: null, workspaceId: null, section: "all" });
                }}
              />
              {(folders as any[]).filter((f: any) => !f.parent_id).map((folder: any) => (
                <FolderTreeNode
                  key={folder.id}
                  folder={folder}
                  allFolders={folders as any[]}
                  depth={0}
                  currentFolderId={currentFolderId}
                  onSelect={(f) => {
                    const path = buildFolderPath(f, folders as any[]);
                    setLibrarySection("all");
                    setSelectedWorkspaceId(null);
                    setFolderPath(path);
                    updateKnowledgeUrl({ folderId: f.id, workspaceId: null, section: "all" });
                  }}
                  onDragOver={(e, folderId) => handleDragOverFolder(e, folderId)}
                  onDrop={(e, folderId) => handleDropOnFolder(e, folderId)}
                  dragOverTarget={dragOverTarget}
                />
              ))}
              {canUploadKnowledge && (
                <button onClick={startCreateFolder} className="kb-new-group-sidebar">
                  <IconPlus size={14} />
                  {t("page.knowledge.new_folder")}
                </button>
              )}
            </SidebarSection>
          </div>

          {/* Workspace filter */}
          {(workspaces as any[]).length > 0 && (
            <div className="mt-4">
              <SidebarSection title={t("page.knowledge.workspace")}>
                <SidebarLink
                  label={t("page.knowledge.all_workspaces")}
                  icon="M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75c.621 0 1.125.504 1.125 1.125V21"
                  isActive={!selectedWorkspaceId}
                  onClick={() => {
                    setSelectedWorkspaceId(null);
                    updateKnowledgeUrl({ workspaceId: null });
                  }}
                />
                {(workspaces as any[]).map((ws: any) => (
                  <SidebarLink
                    key={ws.id}
                    label={ws.name}
                    icon="M2.25 21h19.5m-18-18v18m10.5-18v18m6-13.5V21M6.75 6.75h.75m-.75 3h.75m-.75 3h.75m3-6h.75m-.75 3h.75m-.75 3h.75M6.75 21v-3.375c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21M3 3h12m-.75 4.5H21m-3.75 3H21m-3.75 3H21"
                    isActive={selectedWorkspaceId === ws.id}
                    onClick={() => {
                      setSelectedWorkspaceId(ws.id);
                      setFolderPath([]);
                      updateKnowledgeUrl({ workspaceId: ws.id, folderId: null });
                    }}
                  />
                ))}
              </SidebarSection>
            </div>
          )}

          {/* Sources */}
          {canUploadKnowledge && (
            <div className="mt-4">
              <SidebarSection title={t("page.knowledge.sources")}>
                <SidebarLink
                  label={t("action.upload")}
                  icon="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"
                  onClick={() => openFilePicker()}
                />
                <SidebarLink
                  label={t("page.knowledge.google_drive")}
                  icon="M2.25 15a4.5 4.5 0 004.5 4.5H18a3.75 3.75 0 001.332-7.257 3 3 0 00-3.758-3.848 5.25 5.25 0 00-10.233 2.33A4.502 4.502 0 002.25 15z"
                  onClick={() => handleGoogleDrivePick()}
                />
              </SidebarSection>
            </div>
          )}

          {/* File Types */}
          <div className="mt-4 flex-1 overflow-hidden flex flex-col">
            <SidebarSection title={t("page.knowledge.file_types")}>
              <FileTypeFilterList fileTypeFilter={fileTypeFilter} setFileTypeFilter={handleFileTypeFilter} />
            </SidebarSection>
          </div>

        </div>
      )}

      {/* ── Main Content ─────────────────────────── */}
      <div className="flex-1 min-w-0 flex flex-col px-4 py-0 overflow-hidden">
        {/* Header */}
        <PageHeader
          title={t("page.knowledge.knowledge_base")}
          subtitle={data ? `${totalFiles} files · ${formatFileSize(totalStorage)}` : undefined}
          compactControls={false}
          toolbar={librarySection !== "trash" ? (
              <div className="flex w-full flex-wrap items-center gap-2 lg:w-auto lg:justify-end">
                <SmartToolbar
                  searchValue={search}
                  onSearchChange={handleSearchChange}
                  searchPlaceholder={t("page.knowledge.search_documents")}
                  className="w-full min-w-0 sm:w-56 sm:min-w-[200px]"
                />
                <Select
                  value={sortKey}
                  onChange={(v) => handleSortChange(v as SortKey)}
                  options={[
                    { value: "date", label: t("page.knowledge.sort_date") },
                    { value: "name", label: t("page.knowledge.sort_name") },
                    { value: "size", label: t("page.knowledge.sort_size") },
                  ]}
                  style={{ width: 130, flex: "0 0 130px" }}
                />
                <div className="flex flex-none gap-0.5 p-0.5 rounded-lg border border-stone-200 bg-white">
                  {(["table", "grid"] as ViewMode[]).map((m) => (
                    <button
                      key={m}
                      onClick={() => handleViewModeChange(m)}
                      className={`kb-view-btn ${viewMode === m ? "bg-stone-950 text-white" : "bg-transparent text-stone-400"}`}
                    >
                      {m === "table" ? (
                        <IconList size={14} />
                      ) : (
                        <IconDashboard size={14} />
                      )}
                    </button>
                  ))}
                </div>
                <button
                  onClick={toggleSelectMode}
                  className={`h-9 px-3 rounded-lg border text-xs font-semibold transition-all ${selectMode ? "border-stone-950 bg-stone-950 text-white" : "border-stone-200 bg-white text-stone-500 hover:text-stone-900"}`}
                >
                  {selectMode ? t("page.team_people.done") : t("page.knowledge.select")}
                </button>
                {selectMode && (
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      onClick={selectedIds.size === allSelectableIds.length ? deselectAll : selectAll}
                      className="text-xs font-semibold text-manor-600 hover:text-manor-700 transition-colors bg-transparent border-none cursor-pointer"
                    >
                      {selectedIds.size === allSelectableIds.length ? t("page.knowledge.deselect_all") : t("page.knowledge.select_all")}
                    </button>
                    {selectedIds.size > 0 && (
                      <span className="text-xs text-stone-400">{selectedIds.size} {t("page.onboarding.selected")}</span>
                    )}
                  </div>
                )}
              </div>
          ) : undefined}
          actions={
            <div className="flex items-center gap-2">
              {sidebarCollapsed && (
                <button onClick={() => setSidebarCollapsed(false)} className="kb-expand-sidebar">
                  <IconList size={18} />
                </button>
              )}
              {canUploadKnowledge && (
                <Dropdown
                  align="right"
                  trigger={
                    <PageHeaderAddButton label={t("page.knowledge.add_item")} />
                  }
                  items={newMenuItems}
                  onSelect={(key) => {
                    // Folder creation doesn't consume storage; everything else
                    // adds to the knowledge base, so block it when over quota.
                    if (key !== "new-folder" && guardStorage()) return;
                    switch (key) {
                      case "new-folder":
                        startCreateFolder();
                        break;
                      case "new-wiki-page":
                        startCreateWikiPage();
                        break;
                      case "upload-file":
                        openFilePicker();
                        break;
                      case "new-blank-document":
                        setShowCreateBlankModal(true);
                        break;
                      case "ai-draft":
                        setShowAiDraftModal(true);
                        break;
                      case "import-url":
                        setShowImportUrlModal(true);
                        break;
                      case "google-drive":
                        handleGoogleDrivePick();
                        break;
                      default:
                        break;
                    }
                  }}
                />
              )}
            </div>
          }
        />

        {librarySection !== "trash" && (
          <div className="flex min-w-0 flex-wrap items-center gap-1 px-4 pb-2 text-[13px]">
            {breadcrumbs.map((crumb, i) => {
              const isLast = i === breadcrumbs.length - 1;
              const crumbFolderId = i === 0 ? null : folderPath[i - 1]?.id ?? null;
              const isDragOver = draggingItem && !isLast && (dragOverTarget === (crumbFolderId ?? "__root__"));
              return (
                <span key={i} className="flex items-center gap-1">
                  {i > 0 && <span className="text-stone-300 text-[13px] mx-0.5">/</span>}
                  <span
                    className={`${isLast ? "px-2 py-1 rounded-md font-bold text-stone-800" : "kb-breadcrumb-item font-medium text-stone-500"}${isDragOver ? " drag-over" : ""}`}
                    onClick={() => {
                      if (!isLast) crumb.onClick();
                    }}
                    onDragOver={!isLast ? (e) => handleDragOverFolder(e, crumbFolderId) : undefined}
                    onDragLeave={!isLast ? handleDragLeaveFolder : undefined}
                    onDrop={!isLast ? (e) => handleDropOnFolder(e, crumbFolderId) : undefined}
                  >
                    {crumb.label}
                  </span>
                </span>
              );
            })}
          </div>
        )}

        {/* ── Trash View ──────────────────────────── */}
        {librarySection === "trash" ? (
          <div className="flex-1 overflow-y-auto">
            <div className="flex items-center justify-between px-4 py-3 mb-4">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-red-50 flex items-center justify-center">
                  <IconTrash size={20} className="text-red-400" />
                </div>
                <div>
                  <h3 className="text-[15px] font-bold text-stone-800">{t("page.workspaces.trash")}</h3>
                  <p className="text-xs text-stone-400">
                    {Array.isArray(trashData) ? trashData.length : 0} {t("page.knowledge.item")}{(Array.isArray(trashData) ? trashData.length : 0) !== 1 ? "s" : ""} {t("page.knowledge.in_trash")}
                  </p>
                </div>
              </div>
              {Array.isArray(trashData) && trashData.length > 0 && (
                <Button
                  variant="ghost"
                  onClick={() => {
                    if (confirm(t("page.knowledge.permanently_delete_all_items_in_trash_this_canno"))) {
                      emptyTrashMutation.mutate();
                    }
                  }}
                  disabled={emptyTrashMutation.isPending}
                  className="!text-red-500 hover:!bg-red-50"
                >
                  <IconTrash size={14} />
                  {emptyTrashMutation.isPending ? t("page.knowledge.emptying") : t("page.knowledge.empty_trash")}
                </Button>
              )}
            </div>

            {isLoadingTrash ? (
              <div style={{ padding: "16px" }}>
                <SkeletonTable rows={4} cols={4} />
              </div>
            ) : !Array.isArray(trashData) || trashData.length === 0 ? (
              <EmptyState
                icon={<IconTrash size={32} className="text-stone-300" />}
                title={t("page.knowledge.trash_is_empty")}
                description={t("page.knowledge.deleted_documents_will_appear_here")}
              />
            ) : (
              <table className="glass-table">
                <thead>
                  <tr>
                    <th>{t("page.task_collections.name")}</th>
                    <th className="w-16 text-center">{t("page.custom_fields.type")}</th>
                    <th className="w-20 text-right">{t("page.qr.size")}</th>
                    <th className="w-28 text-right">{t("page.knowledge.deleted")}</th>
                    <th className="w-48 text-right">{t("page.custom_fields.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(trashData as any[]).map((doc: any) => {
                    const typeInfo = getFileTypeInfo(doc.name, doc.file_type || undefined);
                    return (
                      <tr key={doc.id}>
                        <td>
                          <div className="flex items-center gap-3">
                            <div
                              className="w-9 h-9 rounded-[10px] flex items-center justify-center flex-shrink-0"
                              style={{ background: typeInfo.bg }}
                            >
                              <span className="text-[10px] font-extrabold" style={{ color: typeInfo.color }}>{typeInfo.icon}</span>
                            </div>
                            <span className="text-sm font-semibold text-stone-500 line-through">{doc.name}</span>
                          </div>
                        </td>
                        <td className="text-center">
                          <span
                            className="text-[10px] font-extrabold py-0.5 px-1.5 rounded"
                            style={{ background: typeInfo.bg, color: typeInfo.color }}
                          >
                            {typeInfo.icon}
                          </span>
                        </td>
                        <td className="text-right text-xs text-stone-500">
                          {doc.file_size != null ? formatFileSize(doc.file_size) : "--"}
                        </td>
                        <td className="text-right text-xs text-stone-500">
                          {doc.deleted_at ? relativeTime(doc.deleted_at) : doc.updated_at ? relativeTime(doc.updated_at) : "--"}
                        </td>
                        <td className="text-right">
                          <div className="flex items-center justify-end gap-2">
                            <button
                              className="kb-trash-restore-btn"
                              onClick={() => restoreMutation.mutate(doc.id)}
                              disabled={restoreMutation.isPending}
                            >
                              <IconRefresh size={12} /> {t("page.workspaces.restore")}
                            </button>
                            <button
                              className="kb-trash-permdelete-btn"
                              onClick={() => {
                                if (confirm(`Permanently delete "${doc.name}"?`)) {
                                  deleteMutation.mutate(doc.id);
                                }
                              }}
                              disabled={deleteMutation.isPending}
                            >
                              {t("action.delete")}
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        ) : (
        <>
        {/* ── Files View ─────────────────────────── */}
        <>
            {/* File count display */}
            <div className="flex items-center gap-2 px-4 pb-2 text-xs text-stone-400">
              {folderCount > 0 && <span>{folderCount} {t("page.knowledge.folder")}{folderCount !== 1 ? "s" : ""}</span>}
              {folderCount > 0 && fileCount > 0 && <span className="w-[3px] h-[3px] rounded-full bg-stone-300" />}
              {fileCount > 0 && <span>{fileCount} {t("page.knowledge.file")}{fileCount !== 1 ? "s" : ""}</span>}
              {folderCount === 0 && fileCount === 0 && !isLoading && <span>{t("page.knowledge.empty_folder")}</span>}
              {wikiIndex && (
                <button type="button" className="kb-wiki-map-pill" onClick={() => setShowWikiMap(true)}>
                  <IconFlow size={13} />
                  {t("page.knowledge.wiki_map")}
                  <span>{wikiPages.length}</span>
                </button>
              )}
            </div>

            {/* Hidden file input (always in DOM so refs work) */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                const folderId = filePickerFolderIdRef.current === undefined
                  ? currentFolderId
                  : filePickerFolderIdRef.current;
                handleFiles(e.target.files, folderId);
                filePickerFolderIdRef.current = undefined;
                e.currentTarget.value = "";
              }}
            />

            {/* Content area */}
            <div
              className={`flex-1 overflow-y-auto${dragOverTarget === "__current__" && (draggingItem || dragOver) ? " kb-drop-zone-active" : ""}`}
              onContextMenu={(e) => {
                // Only fire on the background, not on cards
                if ((e.target as HTMLElement).closest(".kb-folder-card, .kb-file-card, .glass-card")) return;
                contextMenu.show(e, buildEmptySpaceContextMenu());
              }}
              onDragOver={(e) => {
                if (canUploadKnowledge && canWriteFolderId(currentFolderId) && !draggingItem && isExternalFileDrag(e)) {
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "copy";
                  setDragOver(true);
                  setDragOverTarget("__current__");
                  return;
                }
                // Only act as drop zone on the background itself
                if (draggingItem && !(e.target as HTMLElement).closest(".kb-folder-card, .kb-file-card")) {
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "move";
                  setDragOverTarget("__current__");
                }
              }}
              onDragLeave={(e) => {
                const related = e.relatedTarget as Node | null;
                if (related && (e.currentTarget as Node).contains(related)) return;
                if (dragOverTarget === "__current__") {
                  setDragOver(false);
                  setDragOverTarget(null);
                }
              }}
              onDrop={(e) => {
                if (draggingItem && !(e.target as HTMLElement).closest(".kb-folder-card")) {
                  handleDropOnFolder(e, currentFolderId);
                  return;
                }
                if (canUploadKnowledge && canWriteFolderId(currentFolderId) && !draggingItem && e.dataTransfer.files.length > 0) {
                  handleDrop(e, currentFolderId);
                }
              }}
            >
              {isLoading ? (
                <div style={{ padding: "16px" }}>
                  <SkeletonTable rows={6} cols={4} />
                </div>
              ) : (foldersAtLevel.length === 0 && allDocuments.length === 0 && !creatingFolder) ? (
                <div className="kb-empty-folder-shell">
                  <div
                    className={`kb-empty-folder-panel${canAddToCurrentFolder ? " can-upload" : ""}${dragOver ? " is-drag-over" : ""}`}
                    onClick={() => {
                      if (canAddToCurrentFolder) openFilePicker(currentFolderId);
                    }}
                    onDragOver={(e) => {
                      if (!canAddToCurrentFolder) return;
                      e.preventDefault();
                      e.dataTransfer.dropEffect = "copy";
                      setDragOver(true);
                    }}
                    onDragLeave={() => setDragOver(false)}
                    onDrop={(e) => {
                      if (canAddToCurrentFolder) handleDrop(e, currentFolderId);
                    }}
                  >
                    <div className="kb-empty-folder-visual" aria-hidden="true">
                      <svg className="kb-empty-folder-sketch" viewBox="0 0 158 112" focusable="false">
                        <path className="dash" d="M45 31c18-17 52-18 72 1 10 10 12 23 7 35" />
                        <rect className="paper" x="86" y="18" width="34" height="26" rx="6" transform="rotate(-8 103 31)" />
                        <path className="thin" d="M96 29h13M96 35h8" />
                        <rect className="paper" x="111" y="45" width="30" height="24" rx="6" transform="rotate(8 126 57)" />
                        <path className="thin" d="M119 55h12M119 61h8" />
                        <path className="folder-tab" d="M34 46c0-7 4-11 11-11h19c5 0 8 2 11 6l4 5h36c7 0 11 4 11 11v3H34z" />
                        <path className="folder-fill" d="M28 56h102l-7 37c-1 6-6 10-12 10H45c-7 0-12-4-13-10z" />
                        <path className="line" d="M34 60v-14c0-7 4-11 11-11h19c5 0 8 2 11 6l4 5h36c7 0 11 4 11 11v3" />
                        <path className="line" d="M28 56h102l-7 37c-1 6-6 10-12 10H45c-7 0-12-4-13-10z" />
                        <circle className="upload-fill" cx="126" cy="84" r="15" />
                        <path className="line" d="M126 91V78" />
                        <path className="line" d="M121 82l5-5 5 5" />
                        <path className="thin" d="M118 94h16" />
                        <path className="thin" d="M47 72h38" />
                        <path className="thin" d="M47 82h25" />
                      </svg>
                    </div>
                    <h3 className="kb-empty-folder-title">
                      {uploadMutation.isPending ? t("page.knowledge.uploading") : t("page.knowledge.empty_folder")}
                    </h3>
                    <p className="kb-empty-folder-copy">
                      {canAddToCurrentFolder
                        ? t("page.knowledge.drag_and_drop_files_here_or_click_to_browse")
                        : t("page.knowledge.empty_folder_readonly_description")}
                      {canAddToCurrentFolder && (
                        <>
                          <br />
                          {t("page.knowledge.supports_pdf_txt_docx_xlsx_csv_and_more")}
                        </>
                      )}
                    </p>
                    {canAddToCurrentFolder ? (
                      <div className="kb-empty-folder-actions">
                        <button
                          type="button"
                          className="kb-empty-folder-action primary"
                          onClick={(e) => {
                            e.stopPropagation();
                            openFilePicker(currentFolderId);
                          }}
                        >
                          <IconUpload size={14} />
                          {t("page.knowledge.upload_file")}
                        </button>
                        <button
                          type="button"
                          className="kb-empty-folder-action"
                          onClick={(e) => {
                            e.stopPropagation();
                            startCreateFolder();
                          }}
                        >
                          <IconFolder size={14} />
                          {t("page.knowledge.new_folder")}
                        </button>
                        <button
                          type="button"
                          className="kb-empty-folder-action"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (guardStorage()) return;
                            setShowCreateBlankModal(true);
                          }}
                        >
                          <IconDocument size={14} />
                          {t("page.knowledge.new_blank_document")}
                        </button>
                      </div>
                    ) : null}
                  </div>
                </div>
              ) : viewMode === "grid" ? (
                /* ── Grid View ──────────────────────────── */
                <div className="space-y-7">
                  {/* Folders section */}
                  {(foldersAtLevel.length > 0 || creatingFolder) && (
                    <section>
                      <div className="kb-section-header">
                        <h3 className="kb-section-title">
                          <IconFolder size={14} />
                          {t("page.knowledge.folders")}
                        </h3>
                        <span className="kb-section-count">{folderCount} {t("page.knowledge.folder")}{folderCount !== 1 ? "s" : ""}</span>
                      </div>
                      <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
                        {/* Inline folder creation card */}
                        {creatingFolder && (
                          <div className="kb-folder-card">
                            <div className="w-10 h-10 rounded-[12px] bg-gradient-to-br from-amber-500/10 to-orange-500/10 flex items-center justify-center flex-shrink-0">
                              <IconFolder size={20} className="text-amber-600" />
                            </div>
                            <div className="flex-1 min-w-0">
                              <input
                                ref={inlineFolderRef}
                                type="text"
                                value={newFolderName}
                                onChange={(e) => setNewFolderName(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") commitCreateFolder();
                                  if (e.key === "Escape") { setCreatingFolder(false); setNewFolderName(""); }
                                }}
                                onBlur={commitCreateFolder}
                                className="kb-inline-folder-input"
                                placeholder={t("page.knowledge.folder_name")}
                              />
                            </div>
                          </div>
                        )}

                        {foldersAtLevel.map((folder: any) => {
                          const folderManage = canManageFolderItem(folder);
                          return (
                          <div
                            key={folder.id}
                            className={`kb-folder-card${draggingItem?.id === folder.id ? " dragging" : ""}${dragOverTarget === folder.id ? " drag-over" : ""}${selectMode && selectedIds.has(folder.id) ? " !border-manor-500" : ""}`}
                            style={{ position: "relative" }}
                            draggable={!selectMode && folderManage}
                            onDragStart={(e) => { if (!selectMode && folderManage) handleDragStartItem(e, { id: folder.id, type: "folder", name: folder.name }); }}
                            onDragEnd={handleDragEndItem}
                            onDragOver={(e) => {
                              if (draggingItem && draggingItem.id !== folder.id) {
                                handleDragOverFolder(e, folder.id);
                                return;
                              }
                              if (canUploadKnowledge && folderManage && !draggingItem && isExternalFileDrag(e)) {
                                e.preventDefault();
                                e.dataTransfer.dropEffect = "copy";
                                setDragOverTarget(folder.id);
                              }
                            }}
                            onDragLeave={handleDragLeaveFolder}
                            onDrop={(e) => {
                              if (draggingItem && draggingItem.id !== folder.id) {
                                handleDropOnFolder(e, folder.id);
                                return;
                              }
                              if (canUploadKnowledge && folderManage && !draggingItem && e.dataTransfer.files.length > 0) handleDrop(e, folder.id);
                            }}
                            onClick={() => selectMode ? (folderManage && toggleSelection(folder.id)) : enterFolder(folder)}
                            onContextMenu={(e) => { if (!selectMode) contextMenu.show(e, buildFolderContextMenu(folder)); }}
                          >
                            {selectMode && folderManage && (
                              <div className={`kb-select-checkbox${selectedIds.has(folder.id) ? " checked" : ""}`} style={{ position: "absolute", top: 8, left: 8, zIndex: 2 }}>
                                {selectedIds.has(folder.id) && (
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                                )}
                              </div>
                            )}
                            <div className="w-10 h-10 rounded-[12px] bg-gradient-to-br from-amber-500/10 to-orange-500/10 flex items-center justify-center flex-shrink-0">
                              <IconFolder size={20} className="text-amber-600" />
                            </div>
                            <div className="flex-1 min-w-0">
                              <p className="text-[14px] font-semibold text-stone-800 truncate">{folder.name}</p>
                              <p className="text-xs text-stone-400 mt-0.5">
                                {folder.document_count ?? 0} {t("page.knowledge.item")}{(folder.document_count ?? 0) !== 1 ? "s" : ""}
                              </p>
                            </div>
                            <IconChevronRight size={14} className="text-stone-300 flex-shrink-0" />
                          </div>
                          );
                        })}
                      </div>
                    </section>
                  )}

                  {/* Files section */}
                  <section>
                    <div className="kb-section-header">
                      <h3 className="kb-section-title">
                        <IconDocument size={14} />
                        {t("page.knowledge.files")}
                      </h3>
                      <span className="kb-section-count">{fileCount} {t("page.knowledge.file")}{fileCount !== 1 ? "s" : ""}</span>
                    </div>
                    {allDocuments.length === 0 && uploadingFiles.length === 0 ? (
                      <p className="kb-section-empty">{t("page.knowledge.no_files_in_current_folder")}</p>
                    ) : (
                      <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
                        {/* Uploading placeholder cards */}
                        {uploadingFiles.map((name) => {
                          const typeInfo = getFileTypeInfo(name);
                          return (
                            <div key={`uploading-${name}`} className="kb-file-card card-uploading" style={{ position: "relative" }}>
                              <div
                                className="w-full flex items-center justify-center"
                                style={{ aspectRatio: "16/10", background: typeInfo.bg }}
                              >
                                <span className="text-2xl font-black tracking-wide" style={{ color: typeInfo.color, opacity: 0.7 }}>{typeInfo.icon}</span>
                              </div>
                              <div className="kb-file-card-body">
                                <p className="text-[13px] font-semibold text-stone-800 mb-1 truncate">{name}</p>
                                <div className="flex items-center gap-2 mb-2">
                                  <span className="text-[11px] text-stone-400">--</span>
                                </div>
                                <CardStatusOverlay variant="uploading" />
                              </div>
                            </div>
                          );
                        })}
                        {allDocuments.map((doc) => {
                      const typeInfo = getFileTypeInfo(doc.name, doc.file_type || undefined);
                      const statusInfo = getVectorStatusBadge(doc.vector_status);
                      const statusCls = cardStatusClass(doc.vector_status);
                      const isGenerating = doc.vector_status === VectorStatus.GENERATING;
                      const idxProgress = parseIndexingProgress(doc.indexing_progress);
                      const media = getMediaPreviewUrl(doc);
                      const docCanManageMetadata = canManageDocMetadata(doc);
                      const docManage = docCanManageMetadata || canDeleteDoc(doc);
                      const folderPathLabel = isSearching ? folderPathLabelForDocument(doc) : "";
                      return (
                        <div
                          key={doc.id}
                          className={`kb-file-card${draggingItem?.id === doc.id ? " dragging" : ""}${selectMode && selectedIds.has(doc.id) ? " !border-manor-500" : ""}${statusCls ? ` ${statusCls}` : ""}`}
                          draggable={!selectMode && !statusCls && docCanManageMetadata}
                          onDragStart={(e) => { if (!selectMode && docCanManageMetadata) handleDragStartItem(e, { id: doc.id, type: "file", name: doc.name }); }}
                          onDragEnd={handleDragEndItem}
                          onClick={() => {
                            if (isGenerating) return;
                            if (selectMode) { if (docManage) toggleSelection(doc.id); return; }
                            openDocument(doc);
                          }}
                          onContextMenu={(e) => { if (!selectMode && !isGenerating) contextMenu.show(e, buildFileContextMenu(doc)); }}
                        >
                          {selectMode && docManage && (
                            <div className={`kb-select-checkbox${selectedIds.has(doc.id) ? " checked" : ""}`} style={{ position: "absolute", top: 10, left: 10, zIndex: 2 }}>
                              {selectedIds.has(doc.id) && (
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                              )}
                            </div>
                          )}
                          {/* Favorite star */}
                          {favoriteDocIds.has(doc.id) && (
                            <div style={{ position: "absolute", top: 10, right: 10, zIndex: 2, color: "#ddbb63" }}>
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="1.5"><path d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" /></svg>
                            </div>
                          )}

                          {/* Card header: media preview or icon banner */}
                          {media ? (
                            <div className="w-full bg-stone-100" style={{ height: 100 }}>
                              <DocumentMediaPreview doc={doc} media={media} />
                            </div>
                          ) : (
                            <div
                              className="w-full flex items-center justify-center"
                              style={{ height: 100, background: typeInfo.bg }}
                            >
                              <span className="text-2xl font-black tracking-wide" style={{ color: typeInfo.color, opacity: 0.7 }}>{typeInfo.icon}</span>
                            </div>
                          )}

                          {/* Card body */}
                          <div className="kb-file-card-body">
                            <p className="text-[13px] font-semibold text-stone-800 mb-1 truncate">{doc.name}</p>
                            {folderPathLabel && (
                              <p className="m-0 mb-1 truncate text-[11px] font-medium text-stone-400">
                                {folderPathLabel}
                              </p>
                            )}

                            <div className="flex items-center gap-2 mb-2">
                              <span className="text-[11px] text-stone-400">
                                {doc.file_size != null && doc.file_size > 0 ? formatFileSize(doc.file_size) : doc.vector_status === VectorStatus.GENERATING ? "..." : "--"}
                              </span>
                              <span className="w-[3px] h-[3px] rounded-full bg-stone-300" />
                              <span className="text-[11px] text-stone-400">
                                {doc.created_at ? new Date(doc.created_at).toLocaleDateString() : "--"}
                              </span>
                            </div>

                            {statusCls ? (
                              <CardStatusOverlay
                                vectorStatus={doc.vector_status}
                                progress={idxProgress.progress}
                                stepLabel={idxProgress.stepLabel}
                              />
                            ) : (
                              <StatusBadge type={statusInfo.type} dot>{statusInfo.label}</StatusBadge>
                            )}
                          </div>
                        </div>
                      );
                        })}
                      </div>
                    )}
                  </section>
                </div>
              ) : (
                /* ── Table View ─────────────────────────── */
                <div className="space-y-7">
                  {/* Folder rows in table */}
                  {(foldersAtLevel.length > 0 || creatingFolder) && (
                    <section>
                      <div className="kb-section-header">
                        <h3 className="kb-section-title">
                          <IconFolder size={14} />
                          {t("page.knowledge.folders")}
                        </h3>
                        <span className="kb-section-count">{folderCount} {t("page.knowledge.folder")}{folderCount !== 1 ? "s" : ""}</span>
                      </div>
                      <table className="glass-table">
                        <thead>
                          <tr>
                            {selectMode && <th className="w-10" />}
                            <th>{t("page.knowledge.folder_2")}</th>
                            <th className="w-20 text-right">{t("page.knowledge.items")}</th>
                            <th className="w-16" />
                          </tr>
                        </thead>
                        <tbody>
                          {creatingFolder && (
                            <tr>
                              {selectMode && <td />}
                              <td>
                                <div className="flex items-center gap-3">
                                  <div className="w-9 h-9 rounded-[10px] bg-gradient-to-br from-amber-500/10 to-orange-500/10 flex items-center justify-center flex-shrink-0">
                                    <IconFolder size={16} className="text-amber-600" />
                                  </div>
                                  <input
                                    ref={inlineFolderRef}
                                    type="text"
                                    value={newFolderName}
                                    onChange={(e) => setNewFolderName(e.target.value)}
                                    onKeyDown={(e) => {
                                      if (e.key === "Enter") commitCreateFolder();
                                      if (e.key === "Escape") { setCreatingFolder(false); setNewFolderName(""); }
                                    }}
                                    onBlur={commitCreateFolder}
                                    className="kb-inline-folder-input"
                                    placeholder={t("page.knowledge.folder_name")}
                                  />
                                </div>
                              </td>
                              <td />
                              <td />
                            </tr>
                          )}
                          {foldersAtLevel.map((folder: any) => {
                            const folderManage = canManageFolderItem(folder);
                            return (
                            <tr
                              key={folder.id}
                              className={`cursor-pointer${draggingItem?.id === folder.id ? " dragging" : ""}${dragOverTarget === folder.id ? " drag-over" : ""}${selectMode && selectedIds.has(folder.id) ? " bg-manor-50/50" : ""}`}
                              draggable={!selectMode && folderManage}
                              onDragStart={(e) => { if (!selectMode && folderManage) handleDragStartItem(e, { id: folder.id, type: "folder", name: folder.name }); }}
                              onDragEnd={handleDragEndItem}
                              onDragOver={(e) => {
                                if (draggingItem && draggingItem.id !== folder.id) {
                                  handleDragOverFolder(e, folder.id);
                                  return;
                                }
                                if (canUploadKnowledge && folderManage && !draggingItem && isExternalFileDrag(e)) {
                                  e.preventDefault();
                                  e.dataTransfer.dropEffect = "copy";
                                  setDragOverTarget(folder.id);
                                }
                              }}
                              onDragLeave={handleDragLeaveFolder}
                              onDrop={(e) => {
                                if (draggingItem && draggingItem.id !== folder.id) {
                                  handleDropOnFolder(e, folder.id);
                                  return;
                                }
                                if (canUploadKnowledge && folderManage && !draggingItem && e.dataTransfer.files.length > 0) handleDrop(e, folder.id);
                              }}
                              onClick={() => selectMode ? (folderManage && toggleSelection(folder.id)) : enterFolder(folder)}
                              onContextMenu={(e) => { if (!selectMode) contextMenu.show(e, buildFolderContextMenu(folder)); }}
                            >
                              {selectMode && (
                                <td className="w-10">
                                  {folderManage && (
                                    <div className={`kb-select-checkbox${selectedIds.has(folder.id) ? " checked" : ""}`}>
                                      {selectedIds.has(folder.id) && (
                                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                                      )}
                                    </div>
                                  )}
                                </td>
                              )}
                              <td>
                                <div className="flex items-center gap-3">
                                  <div className="w-9 h-9 rounded-[10px] bg-gradient-to-br from-amber-500/10 to-orange-500/10 flex items-center justify-center flex-shrink-0">
                                    <IconFolder size={16} className="text-amber-600" />
                                  </div>
                                  <span className="text-sm font-semibold text-stone-800">{folder.name}</span>
                                </div>
                              </td>
                              <td className="text-right text-xs text-stone-500">
                                {folder.document_count ?? 0}
                              </td>
                              <td className="text-right">
                                {folderManage && (
                                  <button
                                    onClick={(e) => { e.stopPropagation(); deleteFolderMutation.mutate(folder.id); }}
                                    className="kb-delete-btn"
                                  >
                                    <IconTrash size={14} />
                                  </button>
                                )}
                              </td>
                            </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </section>
                  )}

                  {/* File rows */}
                  <section>
                    <div className="kb-section-header">
                      <h3 className="kb-section-title">
                        <IconDocument size={14} />
                        {t("page.knowledge.files")}
                      </h3>
                      <span className="kb-section-count">{fileCount} {t("page.knowledge.file")}{fileCount !== 1 ? "s" : ""}</span>
                    </div>
                    {(allDocuments.length > 0 || uploadingFiles.length > 0) ? (
                      <table className="glass-table">
                        <thead>
                          <tr>
                            {selectMode && <th className="w-10" />}
                            <th>{t("page.task_collections.name")}</th>
                            <th className="w-16 text-center">{t("page.custom_fields.type")}</th>
                            <th className="w-20 text-right">{t("page.qr.size")}</th>
                            <th className="w-28 text-right">{t("page.knowledge.modified")}</th>
                            <th className="w-24 text-center">{t("page.agent_dashboard.status")}</th>
                            <th className="w-16" />
                          </tr>
                        </thead>
                        <tbody>
                          {uploadingFiles.map((name) => {
                            const typeInfo = getFileTypeInfo(name);
                            return (
                              <tr key={`uploading-${name}`} style={{ opacity: 0.55 }}>
                                {selectMode && <td />}
                                <td>
                                  <div className="flex items-center gap-3">
                                    <div className="w-9 h-9 rounded-[10px] flex items-center justify-center flex-shrink-0" style={{ background: typeInfo.bg }}>
                                      <span className="text-[10px] font-extrabold" style={{ color: typeInfo.color }}>{typeInfo.icon}</span>
                                    </div>
                                    <div className="flex items-center gap-2">
                                      <div className="status-spinner" style={{ color: "#4a7d96", width: 14, height: 14, borderWidth: 2 }} />
                                      <span className="text-sm font-semibold text-stone-500">{name}</span>
                                    </div>
                                  </div>
                                </td>
                                <td className="text-center"><span className="text-[10px] font-extrabold py-0.5 px-1.5 rounded" style={{ background: typeInfo.bg, color: typeInfo.color }}>{typeInfo.icon}</span></td>
                                <td className="text-right text-xs text-stone-400">--</td>
                                <td className="text-right text-xs text-stone-400">--</td>
                                <td className="text-center"><StatusBadge type="info" dot>{t("page.knowledge.uploading")}</StatusBadge></td>
                                <td />
                              </tr>
                            );
                          })}
                          {allDocuments.map((doc) => {
                            const typeInfo = getFileTypeInfo(doc.name, doc.file_type || undefined);
                            const statusInfo = getVectorStatusBadge(doc.vector_status);
                            const tblIdxProgress = parseIndexingProgress(doc.indexing_progress);
                            const docCanManageMetadata = canManageDocMetadata(doc);
                            const docManage = docCanManageMetadata || canDeleteDoc(doc);
                            const folderPathLabel = isSearching ? folderPathLabelForDocument(doc) : "";
                            return (
                              <tr
                                key={doc.id}
                                className={`${draggingItem?.id === doc.id ? "dragging" : ""}${selectMode && selectedIds.has(doc.id) ? " bg-manor-50/50" : ""}`}
                                draggable={!selectMode && docCanManageMetadata}
                                onDragStart={(e) => { if (!selectMode && docCanManageMetadata) handleDragStartItem(e, { id: doc.id, type: "file", name: doc.name }); }}
                                onDragEnd={handleDragEndItem}
                                onClick={selectMode ? () => { if (docManage) toggleSelection(doc.id); } : undefined}
                                onContextMenu={(e) => { if (!selectMode) contextMenu.show(e, buildFileContextMenu(doc)); }}
                                style={selectMode ? { cursor: "pointer" } : undefined}
                              >
                                {selectMode && (
                                  <td className="w-10">
                                    {docManage && (
                                      <div className={`kb-select-checkbox${selectedIds.has(doc.id) ? " checked" : ""}`}>
                                        {selectedIds.has(doc.id) && (
                                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                                        )}
                                      </div>
                                    )}
                                  </td>
                                )}
                                <td>
                                  <div className="flex items-center gap-3">
                                    <div
                                      className="w-9 h-9 rounded-[10px] flex items-center justify-center flex-shrink-0"
                                      style={{ background: typeInfo.bg }}
                                    >
                                      <span className="text-[10px] font-extrabold" style={{ color: typeInfo.color }}>{typeInfo.icon}</span>
                                    </div>
                                    <div className="min-w-0">
                                      {isViewable(doc) ? (
                                        <button onClick={() => openDocument(doc)} className="kb-doc-name-link" type="button">
                                          {doc.name}
                                        </button>
                                      ) : isEditable(doc) ? (
                                        <button onClick={() => openDocument(doc)} className="kb-doc-name-link" type="button">
                                          {doc.name}
                                        </button>
                                      ) : (
                                        <button
                                          onClick={async () => {
                                            const url = await api.documents.download(doc.id);
                                            const a = document.createElement("a");
                                            a.href = url;
                                            a.download = doc.name;
                                            a.click();
                                            URL.revokeObjectURL(url);
                                          }}
                                          className="kb-doc-name-link"
                                          type="button"
                                        >
                                          {doc.name}
                                        </button>
                                      )}
                                      {folderPathLabel ? (
                                        <p className="text-[11px] text-stone-400 m-0">{folderPathLabel}</p>
                                      ) : doc.source && (
                                        <p className="text-[11px] text-stone-400 m-0">{doc.source}</p>
                                      )}
                                    </div>
                                  </div>
                                </td>
                                <td className="text-center">
                                  <span
                                    className="text-[10px] font-extrabold py-0.5 px-1.5 rounded"
                                    style={{ background: typeInfo.bg, color: typeInfo.color }}
                                  >
                                    {typeInfo.icon}
                                  </span>
                                </td>
                                <td className="text-right text-xs text-stone-500">
                                  {doc.file_size != null ? formatFileSize(doc.file_size) : "--"}
                                </td>
                                <td className="text-right text-xs text-stone-500">
                                  {doc.created_at ? new Date(doc.created_at).toLocaleDateString() : "--"}
                                </td>
                                <td className="text-center">
                                  <div className="flex items-center justify-center gap-1.5">
                                    {isVectorInProgress(doc.vector_status) && (
                                      <div className="status-spinner" style={{ width: 12, height: 12, borderWidth: 2, color: doc.vector_status === VectorStatus.GENERATING ? "#9079c2" : "#4f7d75" }} />
                                    )}
                                    <StatusBadge type={statusInfo.type} dot>
                                      {statusInfo.label}
                                      {tblIdxProgress.progress != null && tblIdxProgress.progress > 0 && ` ${tblIdxProgress.progress}%`}
                                    </StatusBadge>
                                  </div>
                                </td>
                                <td className="text-right">
                                  {canDeleteDoc(doc) && (
                                    <button
                                      onClick={(e) => { e.stopPropagation(); trashMutation.mutate({ id: doc.id }); }}
                                      disabled={trashMutation.isPending}
                                      className="kb-delete-btn"
                                    >
                                      <IconTrash size={16} />
                                    </button>
                                  )}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    ) : (
                      <p className="kb-section-empty">{t("page.knowledge.no_files_in_current_folder")}</p>
                    )}
                  </section>
                </div>
              )}
            </div>
          </>
        </>
      )}
      </div>

      <WikiMapModal
        open={showWikiMap}
        pages={wikiPages}
        missingLinks={wikiMissingLinks}
        onClose={() => setShowWikiMap(false)}
        onCreatePage={startCreateWikiPage}
        onOpenPage={openWikiPage}
        onConnect={handleConnectWikiPages}
        onRemoveLink={handleRemoveWikiLink}
        isConnecting={connectWikiPagesMutation.isPending || removeWikiLinkMutation.isPending}
      />

      {/* ── Quick Look Drawer ────────────────────────── */}
      {quickLookDoc && (
        <>
          <div className="kb-quick-look-backdrop" onClick={() => setQuickLookDoc(null)} />
          <div className="kb-quick-look-overlay" role="dialog" aria-modal="true">
            {/* Header */}
            <div className="flex items-center justify-between p-5 border-b border-stone-200/60">
              <div className="flex items-center gap-3 min-w-0">
                {(() => {
                  const typeInfo = getFileTypeInfo(quickLookDoc.name, quickLookDoc.file_type || undefined);
                  return (
                    <div
                      className="w-10 h-10 rounded-[12px] flex items-center justify-center flex-shrink-0"
                      style={{ background: typeInfo.bg }}
                    >
                      <span className="text-xs font-extrabold" style={{ color: typeInfo.color }}>{typeInfo.icon}</span>
                    </div>
                  );
                })()}
                <div className="min-w-0">
                  <p className="text-[15px] font-bold text-stone-800 truncate">{quickLookDoc.name}</p>
                  <p className="text-xs text-stone-400">{quickLookDoc.file_type || quickLookDoc.name?.split(".").pop() || t("page.knowledge.unknown_type")}</p>
                </div>
              </div>
              <button
                onClick={() => setQuickLookDoc(null)}
                className="w-8 h-8 rounded-lg border-none bg-stone-100 hover:bg-stone-200 cursor-pointer flex items-center justify-center transition-colors"
              >
                <IconClose size={16} className="text-stone-500" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-5">
              {isEditable(quickLookDoc) ? (
                isQuickLookMarkdown ? (
                  <WikiLinkedText
                    content={quickLookContent?.content || t("page.knowledge.loading_content")}
                    links={(quickLookWikiLinks?.links || []) as WikiLinkInfo[]}
                    onOpenLink={openWikiDocument}
                    className="wiki-linked-text rounded-xl bg-stone-50/80 border border-stone-200/50 p-4 font-mono text-[13px] text-stone-700 leading-relaxed max-h-[60vh] overflow-y-auto"
                  />
                ) : (
                  <div className="rounded-xl bg-stone-50/80 border border-stone-200/50 p-4 font-mono text-[13px] text-stone-700 leading-relaxed whitespace-pre-wrap max-h-[60vh] overflow-y-auto">
                    {quickLookContent?.content || t("page.knowledge.loading_content")}
                  </div>
                )
              ) : (
                <QuickLookPreview doc={quickLookDoc} />
              )}

              {/* Metadata */}
              <div className="mt-6 space-y-2.5">
                <h4 className="text-xs font-bold text-stone-400 uppercase tracking-wide mb-3">{t("page.knowledge.file_details")}</h4>
                {[
                  { label: t("page.qr.size"), value: quickLookDoc.file_size != null ? formatFileSize(quickLookDoc.file_size) : t("page.workspace_detail.unknown") },
                  { label: t("page.dashboard.created"), value: quickLookDoc.created_at ? new Date(quickLookDoc.created_at).toLocaleString() : t("page.workspace_detail.unknown") },
                  { label: t("page.knowledge.vector_status"), value: quickLookDoc.vector_status || "Unknown" },
                  { label: t("page.skills.source"), value: quickLookDoc.source || "Upload" },
                ].map((row) => (
                  <div key={row.label} className="flex items-center justify-between py-1.5 border-b border-stone-100">
                    <span className="text-xs font-semibold text-stone-500">{row.label}</span>
                    <span className="text-sm text-stone-700 font-medium">{row.value}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Footer actions */}
            <div className="flex items-center justify-end gap-2 p-5 border-t border-stone-200/60">
              {isEditable(quickLookDoc) && (
                <Button
                  variant="ghost"
                  onClick={() => { navigateToDocument(quickLookDoc); setQuickLookDoc(null); }}
                >
                  <IconEdit size={14} />
                  {t("page.knowledge.open_in_editor")}
                </Button>
              )}
              <Button
                variant="primary"
                onClick={async () => {
                  const url = await api.documents.download(quickLookDoc.id);
                  const a = document.createElement("a");
                  a.href = url;
                  a.download = quickLookDoc.name;
                  a.click();
                  URL.revokeObjectURL(url);
                }}
              >
                <IconDownload size={14} />
                {t("page.knowledge.download")}
              </Button>
            </div>
          </div>
        </>
      )}

      {/* ── Context Menu ────────────────────────────── */}
      {contextMenu.menu && (
        <ContextMenu
          items={contextMenu.menu.items}
          x={contextMenu.menu.x}
          y={contextMenu.menu.y}
          onClose={contextMenu.close}
        />
      )}

      {/* ── Batch Action Bar ─────────────────────────── */}
      {batchActionBar && (
        typeof document !== "undefined"
          ? createPortal(batchActionBar, document.body)
          : batchActionBar
      )}

      {/* ── Rename Modal ───────────────────���────────── */}
      <Modal
        open={!!renameTarget}
        onClose={() => setRenameTarget(null)}
        title={`${t("page.knowledge.rename")} ${renameTarget?.type === "folder" ? t("page.knowledge.folder_2") : t("page.knowledge.file")}`}
      >
        <div className="flex flex-col gap-4">
          <Input
            label={t("page.task_collections.name")}
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            placeholder={t("page.knowledge.enter_new_name")}
          />
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="ghost" onClick={() => setRenameTarget(null)}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                if (!renameTarget || !renameValue.trim()) return;
                if (renameTarget.type === "folder") {
                  const folder = (folders as any[]).find((f: any) => f.id === renameTarget.id);
                  if (!canManageFolderItem(folder)) return;
                  renameFolderMutation.mutate({ id: renameTarget.id, name: renameValue.trim() });
                } else {
                  const doc = allDocuments.find((d: any) => d.id === renameTarget.id);
                  if (!canManageDocMetadata(doc)) return;
                  renameDocMutation.mutate({ id: renameTarget.id, name: renameValue.trim() });
                }
              }}
              disabled={!renameValue.trim() || renameFolderMutation.isPending || renameDocMutation.isPending}
            >
              {(renameFolderMutation.isPending || renameDocMutation.isPending) ? t("page.knowledge.renaming") : t("page.knowledge.rename")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* ── Get Info Modal ──────────────────────────── */}
      <Modal
        open={!!infoTarget}
        onClose={() => setInfoTarget(null)}
        title={t("page.knowledge.file_info")}
      >
        {infoTarget && (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-3 pb-3 border-b border-stone-200/50">
              {(() => {
                const typeInfo = getFileTypeInfo(infoTarget.name, infoTarget.file_type || undefined);
                return (
                  <div
                    className="w-14 h-14 rounded-[16px] flex items-center justify-center"
                    style={{ background: typeInfo.bg }}
                  >
                    <span className="text-sm font-extrabold" style={{ color: typeInfo.color }}>{typeInfo.icon}</span>
                  </div>
                );
              })()}
              <div className="min-w-0">
                <p className="text-[15px] font-bold text-stone-800 truncate">{infoTarget.name}</p>
                <p className="text-xs text-stone-400">{infoTarget.file_type || t("page.knowledge.unknown_type")}</p>
              </div>
            </div>
            {[
              { label: t("page.task_collections.name"), value: infoTarget.name },
              { label: t("page.custom_fields.type"), value: infoTarget.file_type || infoTarget.name?.split(".").pop() || "Unknown" },
              { label: t("page.qr.size"), value: infoTarget.file_size != null ? formatFileSize(infoTarget.file_size) : t("page.workspace_detail.unknown") },
              { label: t("page.skills.source"), value: infoTarget.source || "Upload" },
              { label: t("page.knowledge.vector_status"), value: infoTarget.vector_status || "Unknown" },
              { label: t("page.dashboard.created"), value: infoTarget.created_at ? new Date(infoTarget.created_at).toLocaleString() : t("page.workspace_detail.unknown") },
            ].map((row) => (
              <div key={row.label} className="flex items-center justify-between py-1.5">
                <span className="text-xs font-semibold text-stone-500 uppercase tracking-wide">{row.label}</span>
                <span className="text-sm text-stone-700 font-medium">{row.value}</span>
              </div>
            ))}

            {/* Permissions section — gives users at-a-glance access info
                without having to open the file viewer. Same vocabulary as
                FileViewer's details drawer so labels stay consistent. */}
            <div className="pt-3 mt-1 border-t border-stone-200/50">
              <div className="flex items-center justify-between py-1.5">
                <span className="text-xs font-semibold text-stone-500 uppercase tracking-wide">
                  {t("page.file_viewer.details.classification")}
                </span>
                <ClassificationBadge level={infoTarget.classification} size="sm" />
              </div>
              <div className="flex items-center justify-between py-1.5">
                <span className="text-xs font-semibold text-stone-500 uppercase tracking-wide">
                  {t("page.file_viewer.details.visibility")}
                </span>
                <span className="inline-flex items-center gap-1.5 text-sm text-stone-700 font-medium">
                  <VisibilityIcon visibility={infoTarget.visibility} size={13} />
                  {infoTarget.visibility ?? "—"}
                </span>
              </div>
              {infoTarget.client_visible && (
                <div className="flex items-center justify-between py-1.5">
                  <span className="text-xs font-semibold text-stone-500 uppercase tracking-wide">
                    {t("page.file_viewer.details.client_portal")}
                  </span>
                  <span className="text-sm font-medium" style={{ color: "#57534e" }}>
                    {t("page.file_viewer.details.client_visible_value")}
                  </span>
                </div>
              )}
              <div className="flex items-center justify-between py-1.5">
                <span className="text-xs font-semibold text-stone-500 uppercase tracking-wide">
                  {t("page.file_viewer.details.owner")}
                </span>
                <span className="text-sm text-stone-700 font-medium truncate ml-2" title={infoTarget.owner_id || ""}>
                  {infoTarget.owner_id ?? "—"}
                </span>
              </div>
            </div>

            <div className="flex items-center justify-between gap-2 mt-3 pt-3 border-t border-stone-200/50 flex-wrap">
              <div className="flex items-center gap-2 flex-wrap">
                {(canManageDocMetadata(infoTarget) || canShareDoc(infoTarget)) && (
                  <>
                    {canManageDocMetadata(infoTarget) && (
                      <Button
                        variant="outline"
                        onClick={() => {
                          const target = infoTarget;
                          setInfoTarget(null);
                          setDocPropsTarget(target);
                        }}
                      >
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <IconEdit size={14} />
                          {t("page.knowledge.file_properties")}
                        </span>
                      </Button>
                    )}
                    {canShareDoc(infoTarget) && (
                      <Button
                        variant="outline"
                        onClick={() => {
                          const target = infoTarget;
                          setInfoTarget(null);
                          setDocShareTarget(target);
                        }}
                      >
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <IconLink size={14} />
                          {t("page.file_viewer.details.manage_access")}
                        </span>
                      </Button>
                    )}
                  </>
                )}
              </div>
              <Button variant="ghost" onClick={() => setInfoTarget(null)}>{t("page.flows.close")}</Button>
            </div>
          </div>
        )}
      </Modal>

      {/* ── Add to Workspace Picker ──────────────── */}
      <Modal
        open={!!workspacePickerDoc && canManageAllDocuments && canManageDocMetadata(workspacePickerDoc)}
        onClose={() => setWorkspacePickerDoc(null)}
        title={t("page.knowledge.add_to_workspace")}
      >
        {workspacePickerDoc && (
          <WorkspacePickerContent
            doc={workspacePickerDoc}
            workspaces={workspaces as any[]}
            onSelect={(ws) => {
              addToWorkspaceMutation.mutate({ docId: workspacePickerDoc.id, workspace: ws });
              setWorkspacePickerDoc(null);
            }}
          />
        )}
      </Modal>

      {/* ── Move to Folder Picker ────────────────── */}
      <Modal
        open={!!movePickerDoc && canManageDocMetadata(movePickerDoc)}
        onClose={() => setMovePickerDoc(null)}
        title={t("page.knowledge.move_to_folder_2")}
      >
        {movePickerDoc && (
          <div className="flex flex-col gap-1 max-h-[320px] overflow-y-auto">
            {movePickerDoc.folder_id && (
              <button
                className="flex items-center gap-3 w-full px-3 py-2.5 rounded-lg text-left hover:bg-stone-50 transition-colors"
                onClick={() => { moveMutation.mutate({ id: movePickerDoc.id, folder_id: null }); setMovePickerDoc(null); }}
              >
                <IconFolder size={16} className="text-amber-500 shrink-0" />
                <span className="text-sm font-medium text-stone-700">{t("page.knowledge.root_no_folder")}</span>
              </button>
            )}
            {(folders as any[]).filter((f: any) => f.id !== movePickerDoc.folder_id && canManageFolderItem(f)).map((f: any) => (
              <button
                key={f.id}
                className="flex items-center gap-3 w-full px-3 py-2.5 rounded-lg text-left hover:bg-stone-50 transition-colors"
                onClick={() => { moveMutation.mutate({ id: movePickerDoc.id, folder_id: f.id }); setMovePickerDoc(null); }}
              >
                <IconFolder size={16} className="text-amber-500 shrink-0" />
                <span className="text-sm font-medium text-stone-700 truncate">{f.name}</span>
              </button>
            ))}
            {(folders as any[]).filter((f: any) => f.id !== movePickerDoc?.folder_id && canManageFolderItem(f)).length === 0 && !movePickerDoc.folder_id && (
              <p className="text-sm text-stone-400 py-4 text-center">{t("page.knowledge.no_folders_available")}</p>
            )}
          </div>
        )}
      </Modal>

      {/* ── Create Blank Document Modal ──────────── */}
      <Modal
        open={canUploadKnowledge && showCreateBlankModal}
        onClose={() => { setShowCreateBlankModal(false); setBlankDocName(""); setBlankDocType("md"); }}
        title={t("page.knowledge.create_blank_document")}
      >
        <div className="flex flex-col gap-4">
          <Input
            label={t("page.knowledge.document_name")}
            value={blankDocName}
            onChange={(e) => setBlankDocName(e.target.value)}
            placeholder={t("page.knowledge.e_g_meeting_notes")}
            autoFocus
            onFocus={(e) => e.currentTarget.select()}
          />
          <div>
            <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2">
              {t("page.knowledge.file_type")}
            </label>
            <div className="grid grid-cols-4 gap-2">
              {([
                { key: "md", label: t("page.skill_form.markdown"), ext: ".md" },
                { key: "txt", label: t("page.skill_form.text"), ext: ".txt" },
                { key: "docx", label: t("page.knowledge.word"), ext: ".docx" },
                { key: "xlsx", label: t("page.knowledge.spreadsheet"), ext: ".xlsx" },
                { key: "pptx", label: t("page.knowledge.presentation"), ext: ".pptx" },
                { key: "diagram.json", label: t("page.knowledge.diagram_canvas"), ext: ".diagram.json" },
                { key: "csv", label: t("page.knowledge.csv"), ext: ".csv" },
                { key: "json", label: t("page.skill_form.json"), ext: ".json" },
                { key: "html", label: t("page.skill_form.html"), ext: ".html" },
              ] as const).map((ft) => {
                const typeInfo = FILE_TYPE_ICONS[ft.key] || { icon: ft.key.toUpperCase(), color: "#57534e", bg: "#f5f5f4" };
                return (
                  <button
                    key={ft.key}
                    type="button"
                    className={`kb-file-type-card ${blankDocType === ft.key ? "selected" : ""}`}
                    onClick={() => setBlankDocType(ft.key)}
                  >
                    <div
                      className="w-9 h-9 rounded-[10px] flex items-center justify-center"
                      style={{ background: typeInfo.bg }}
                    >
                      <span className="text-[10px] font-extrabold" style={{ color: typeInfo.color }}>{typeInfo.icon}</span>
                    </div>
                    <span className="text-[11px] font-semibold text-stone-600">{ft.label}</span>
                    <span className="text-[10px] text-stone-400">{ft.ext}</span>
                  </button>
                );
              })}
            </div>
          </div>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="ghost" onClick={() => { setShowCreateBlankModal(false); setBlankDocName(""); setBlankDocType("md"); }}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                if (!canUploadKnowledge) return;
                if (!blankDocName.trim()) return;
                createBlankMutation.mutate({ name: blankDocName.trim(), file_type: blankDocType });
              }}
              disabled={!blankDocName.trim() || createBlankMutation.isPending}
            >
              {createBlankMutation.isPending ? t("page.flows.creating") : t("action.create")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* ── Import from URL Modal ────────────────── */}
      <Modal
        open={canUploadKnowledge && showImportUrlModal}
        onClose={() => { setShowImportUrlModal(false); setImportUrl(""); setImportUrlName(""); }}
        title={t("page.knowledge.import_from_url")}
      >
        <div className="flex flex-col gap-4">
          <Input
            label={t("page.webhooks.url")}
            value={importUrl}
            onChange={(e) => setImportUrl(e.target.value)}
            placeholder={t("page.knowledge.https_example_com_document_pdf")}
          />
          <Input
            label={t("page.knowledge.name_optional")}
            value={importUrlName}
            onChange={(e) => setImportUrlName(e.target.value)}
            placeholder={t("page.knowledge.auto_detect_from_url")}
          />
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="ghost" onClick={() => { setShowImportUrlModal(false); setImportUrl(""); setImportUrlName(""); }}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                if (!canUploadKnowledge) return;
                if (!importUrl.trim()) return;
                importFromUrlMutation.mutate({
                  url: importUrl.trim(),
                  name: importUrlName.trim() || undefined,
                });
              }}
              disabled={!importUrl.trim() || importFromUrlMutation.isPending}
            >
              {importFromUrlMutation.isPending ? t("page.skills.importing") : t("action.import")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* ── AI Draft Modal ────────────────── */}
      <Modal
        open={canUploadKnowledge && showAiDraftModal}
        onClose={() => { setShowAiDraftModal(false); setAiDraftPrompt(""); setAiDraftName(""); setAiDraftType("md"); }}
        title={t("page.knowledge.ai_draft")}
      >
        <div className="flex flex-col gap-4">
          <Input
            label={t("page.knowledge.document_name_optional")}
            value={aiDraftName}
            onChange={(e) => setAiDraftName(e.target.value)}
            placeholder={t("page.knowledge.auto_generate_from_content")}
          />
          <div>
            <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2">
              {t("page.knowledge.prompt")}
            </label>
            <Textarea
              value={aiDraftPrompt}
              onChange={(e) => setAiDraftPrompt(e.target.value)}
              placeholder={t("page.knowledge.describe_the_document_you_want_to_create")}
              rows={4}
            />
          </div>
          <div>
            <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2">
              {t("page.knowledge.file_type")}
            </label>
            <div className="grid grid-cols-4 gap-2">
              {([
                { key: "md", label: t("page.skill_form.markdown"), ext: ".md" },
                { key: "txt", label: t("page.skill_form.text"), ext: ".txt" },
                { key: "docx", label: t("page.knowledge.word"), ext: ".docx" },
                { key: "xlsx", label: t("page.knowledge.spreadsheet"), ext: ".xlsx" },
                { key: "pptx", label: t("page.knowledge.presentation"), ext: ".pptx" },
                { key: "csv", label: t("page.knowledge.csv"), ext: ".csv" },
                { key: "json", label: t("page.skill_form.json"), ext: ".json" },
                { key: "html", label: t("page.skill_form.html"), ext: ".html" },
              ] as const).map((ft) => {
                const typeInfo = FILE_TYPE_ICONS[ft.key] || { icon: ft.key.toUpperCase(), color: "#57534e", bg: "#f5f5f4" };
                return (
                  <button
                    key={ft.key}
                    type="button"
                    className={`kb-file-type-card ${aiDraftType === ft.key ? "selected" : ""}`}
                    onClick={() => setAiDraftType(ft.key)}
                  >
                    <div
                      className="w-9 h-9 rounded-[10px] flex items-center justify-center"
                      style={{ background: typeInfo.bg }}
                    >
                      <span className="text-[10px] font-extrabold" style={{ color: typeInfo.color }}>{typeInfo.icon}</span>
                    </div>
                    <span className="text-[11px] font-semibold text-stone-600">{ft.label}</span>
                    <span className="text-[10px] text-stone-400">{ft.ext}</span>
                  </button>
                );
              })}
            </div>
          </div>
          <div className="flex justify-end gap-2 mt-2">
            <Button variant="ghost" onClick={() => { setShowAiDraftModal(false); setAiDraftPrompt(""); setAiDraftName(""); setAiDraftType("md"); }}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                if (!canUploadKnowledge) return;
                if (!aiDraftPrompt.trim()) return;
                aiDraftMutation.mutate({
                  prompt: aiDraftPrompt.trim(),
                  file_type: aiDraftType,
                  name: aiDraftName.trim() || undefined,
                });
              }}
              disabled={!aiDraftPrompt.trim() || aiDraftMutation.isPending}
            >
              {aiDraftMutation.isPending ? t("page.knowledge.generating") : t("action.create")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Upload-options wizard — drives every upload entry (drop, picker, +) */}
      <UploadOptionsDialog
        open={canUploadKnowledge && uploadDialogOpen}
        files={pendingUploadFiles}
        onCancel={() => {
          setPendingUploadFiles([]);
          setPendingUploadFolderId(null);
        }}
        onConfirm={(opts) => {
          if (!canUploadKnowledge) return;
          const files = pendingUploadFiles;
          const folderId = pendingUploadFolderId;
          setPendingUploadFiles([]);
          setPendingUploadFolderId(null);
          uploadMutation.mutate({ files, folderId, options: opts });
        }}
      />

      {/* Folder properties dialog (Phase B) */}
      {folderPropsTarget && canManageFolderItem(folderPropsTarget) && (
        <FolderPropertiesDialog
          open={!!folderPropsTarget}
          folderId={folderPropsTarget.id}
          folderName={folderPropsTarget.name}
          visibility={folderPropsTarget.visibility}
          classification={folderPropsTarget.classification}
          clientVisible={folderPropsTarget.client_visible}
          onCancel={() => setFolderPropsTarget(null)}
          onSave={async (data) => {
            const resp = await api.folderPermissions.setProperties(folderPropsTarget.id, data);
            // Refresh folder list + any visible documents (cascade may have
            // mutated their classification/visibility).
            await invalidateDocumentBrowseAndFolderTree();
            return resp.cascade_summary;
          }}
        />
      )}

      {/* Folder ShareDialog (Phase B tail — reuses the GDrive layout from
          Phase A, just scoped to a folder. Backend routes are mirrored at
          /api/v1/folders/{id}/grants and /shares.) */}
      {folderShareTarget && canShareFolderItem(folderShareTarget) && (
        <FolderShareDialogContainer
          folder={folderShareTarget}
          onClose={() => setFolderShareTarget(null)}
        />
      )}

      {/* Document ShareDialog — same dialog, scoped to a single doc.
          Reachable from the file context menu's "Share file..." and from
          the Get info modal's "Manage access" button so users don't have
          to open the file viewer just to manage permissions. */}
      {docShareTarget && canShareDoc(docShareTarget) && (
        <DocShareDialogContainer
          doc={docShareTarget}
          onClose={() => setDocShareTarget(null)}
        />
      )}

      {/* Document properties — change visibility / classification /
          client_visible on a single file. Reachable from "File properties..."
          in the context menu and from the Get Info modal. */}
      {docPropsTarget && canManageDocMetadata(docPropsTarget) && (
        <DocumentPropertiesDialog
          open
          docId={docPropsTarget.id}
          docName={docPropsTarget.name}
          visibility={docPropsTarget.visibility}
          classification={docPropsTarget.classification}
          clientVisible={docPropsTarget.client_visible}
          onCancel={() => setDocPropsTarget(null)}
          onSave={async (changes) => {
            // Three independent endpoints — call only what changed. Order:
            // classify first so that a same-call client_visible toggle sees
            // the new classification when the server re-checks the
            // confidential+client_visible invariant.
            const docId = docPropsTarget.id;
            if (changes.classification) {
              await api.permissionsV1.classify(docId, changes.classification);
            }
            if (changes.visibility) {
              await api.permissionsV1.setVisibility(docId, changes.visibility);
            }
            if (changes.client_visible !== undefined) {
              await api.permissionsV1.setClientVisible(docId, changes.client_visible);
            }
            await invalidateDocumentBrowse();
          }}
        />
      )}
    </div>
  );
}

// ── Folder ShareDialog container ────────────────────────────────────────
//
// Self-contained so we don't sprinkle yet more useQuery wiring into the
// 3.5k-line Knowledge.tsx. Mirrors the FileViewer wiring but talks to
// api.folderPermissions.* instead of api.docPermissions.*.

function FolderShareDialogContainer({
  folder,
  onClose,
}: {
  folder: DocumentFolderInfo;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const grantsQuery = useQuery({
    queryKey: ["folder-grants", folder.id],
    queryFn: () => api.folderPermissions.listGrants(folder.id),
  });
  const sharesQuery = useQuery({
    queryKey: ["folder-shares", folder.id],
    queryFn: () => api.folderPermissions.listShares(folder.id),
  });

  return (
    <ShareDialog
      open
      onClose={onClose}
      resourceType="document_folder"
      resourceId={folder.id}
      resourceName={folder.name}
      classification={folder.classification}
      visibility={folder.visibility}
      externalShareNeedsApproval={folder.classification === "confidential"}
      internalGrants={(grantsQuery.data || []).map(_folderGrantToInternal)}
      externalShares={(sharesQuery.data || []).map(_folderShareToExternal)}
      onAddInternal={async (pick, role, opts) => {
        // Resolve to a user_id either via staff.user_id (preferred) or
        // via /users/lookup-by-email (fallback for external invites).
        let user_id: string;
        if (pick.kind === "staff") {
          if (pick.staff.user_id) {
            user_id = pick.staff.user_id;
          } else {
            if (!pick.staff.email) {
              throw new Error(t("permissions.error.staff_no_account"));
            }
            try {
              const resolved = await api.users.lookupByEmail(pick.staff.email);
              user_id = resolved.id;
            } catch {
              throw new Error(t("permissions.error.staff_no_account_named", { name: pick.staff.name }));
            }
          }
        } else {
          try {
            const resolved = await api.users.lookupByEmail(pick.email);
            user_id = resolved.id;
          } catch (err: any) {
            if (err?.status === 404) {
              throw new Error(t("permissions.error.user_not_in_org", { email: pick.email }));
            }
            throw err;
          }
        }
        await api.folderPermissions.createGrant(folder.id, {
          subject_type: "user",
          subject_id: user_id,
          capabilities: _folderRoleToCaps(role),
          expires_at: opts.expiresAt,
        });
        void opts.notify;
        void opts.message;
        await queryClient.invalidateQueries({ queryKey: ["folder-grants", folder.id] });
      }}
      onUpdateInternalRole={async (grantId, role) => {
        const existing = (grantsQuery.data || []).find((g) => g.id === grantId);
        if (!existing) throw new Error("Grant not found");
        await api.folderPermissions.createGrant(folder.id, {
          subject_type: "user",
          subject_id: existing.subject_user_id || existing.subject_id,
          capabilities: _folderRoleToCaps(role),
        });
        await queryClient.invalidateQueries({ queryKey: ["folder-grants", folder.id] });
      }}
      onRemoveInternal={async (grantId) => {
        await api.folderPermissions.revokeGrant(folder.id, grantId);
        await queryClient.invalidateQueries({ queryKey: ["folder-grants", folder.id] });
      }}
      onCreateExternal={async (config: NewExternalShareConfig) => {
        // Confidential folders bounce with 409 — surface the message
        // and let the user know they need to downgrade first. Folder
        // approval inbox is Phase B.5.
        if (folder.classification === "confidential") {
          throw new Error(t("permissions.share.folder_confidential_approval_unimplemented"));
        }
        const result = await api.folderPermissions.createShare(folder.id, {
          audience_type: config.audience_type,
          audience_value: config.audience_value,
          capabilities: config.capabilities,
          expires_in_days: config.expires_in_days,
          watermark: config.watermark,
          require_otp: config.require_otp,
          allow_download: config.capabilities.includes("download"),
        });
        await queryClient.invalidateQueries({ queryKey: ["folder-shares", folder.id] });
        const url = result.url
          || (result.token
            ? `${window.location.origin}/shared-doc/${result.token}`
            : undefined);
        return { url };
      }}
      onRevokeExternal={async (shareId) => {
        await api.folderPermissions.revokeShare(folder.id, shareId);
        await queryClient.invalidateQueries({ queryKey: ["folder-shares", folder.id] });
      }}
    />
  );
}

// Adapter: capability set (server) -> role enum (UI). Mirrors the same
// mapping FileViewer uses, kept inline to avoid an extra shared module
// for two callers.
function _folderCapsToRole(caps: string[]): "viewer" | "commenter" | "editor" | "curator" {
  const set = new Set(caps);
  if (set.has("manage_metadata") || set.has("grant_access")) return "curator";
  if (set.has("edit")) return "editor";
  if (set.has("comment")) return "commenter";
  return "viewer";
}

function _folderRoleToCaps(role: "viewer" | "commenter" | "editor" | "curator"): string[] {
  switch (role) {
    case "viewer":    return ["view"];
    case "commenter": return ["view", "comment"];
    case "editor":    return ["view", "comment", "edit", "upload_to"];
    case "curator":   return ["view", "comment", "edit", "upload_to", "manage_metadata", "grant_access", "share_internal"];
  }
}

function _folderGrantToInternal(g: DocumentGrant) {
  let user_email = g.subject_email || g.subject_id;
  let user_name: string | undefined = g.subject_display_name || undefined;
  let avatar_url: string | undefined = g.subject_avatar_url || undefined;
  if (g.subject_type !== "user") {
    user_email = g.subject_display_name || g.subject_email || `${g.subject_type}: ${g.subject_id}`;
    user_name = undefined;
    avatar_url = undefined;
  }
  return {
    id: g.id,
    user_email,
    user_name,
    avatar_url,
    role: _folderCapsToRole(g.capabilities),
    expires_at: g.expires_at,
    source: "explicit" as const,
  };
}

function _folderShareToExternal(s: DocumentShare) {
  return {
    id: s.id,
    audience: s.audience || "anonymous",
    capabilities: s.capabilities,
    expires_at: s.expires_at,
    watermark: s.watermark,
    require_otp: s.require_otp,
    use_count: s.use_count,
    last_used_at: s.last_used_at,
  };
}

// ── Doc ShareDialog container ────────────────────────────────────────────
//
// Same dialog as the folder version, just routed to api.docPermissions.*.
// Carries its own grant + share queries + user batch-resolve so the
// People-with-access list shows display_name / avatar instead of raw uuids.
//
// Mirrors the wiring in FileViewer.tsx (which has its own share toolbar) so
// behavior — confidential-needs-approval branch, external email fallback,
// etc. — stays identical across both entry points.

function DocShareDialogContainer({
  doc,
  onClose,
}: {
  doc: Document;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const currentUser = useAuthStore((s) => s.user);

  const grantsQuery = useQuery({
    queryKey: ["doc-grants", doc.id],
    queryFn: () => api.docPermissions.listGrants(doc.id),
  });
  const sharesQuery = useQuery({
    queryKey: ["doc-shares", doc.id],
    queryFn: () => api.docPermissions.listShares(doc.id),
  });

  // Batch-resolve grant subject_ids → user display info so the People with
  // access list shows name/avatar instead of opaque uuids.
  const grantUserIds = useMemo(
    () =>
      Array.from(
        new Set(
          (grantsQuery.data || [])
            .filter((g) => g.subject_type === "user")
            .map((g) => g.subject_user_id || g.subject_id)
            .filter((id): id is string => Boolean(id)),
        ),
      ),
    [grantsQuery.data],
  );
  const grantUsersQuery = useQuery({
    queryKey: ["users-batch", grantUserIds.sort().join(",")],
    queryFn: () => api.users.batchByIds(grantUserIds),
    enabled: grantUserIds.length > 0,
  });
  const userById = useMemo(() => {
    const map = new Map<string, UserSummary>();
    for (const u of grantUsersQuery.data || []) map.set(u.id, u);
    return map;
  }, [grantUsersQuery.data]);

  return (
    <ShareDialog
      open
      onClose={onClose}
      resourceType="document"
      resourceId={doc.id}
      resourceName={doc.name}
      classification={doc.classification}
      visibility={doc.visibility}
      externalShareNeedsApproval={doc.classification === "confidential"}
      internalGrants={(grantsQuery.data || []).map((g) => _docGrantToInternal(g, userById))}
      externalShares={(sharesQuery.data || []).map(_docShareToExternal)}
      entityDomain={_entityDomainFromEmail(currentUser?.email)}
      onAddInternal={async (pick, role, opts) => {
        // Same staff_id → user_id resolution as FileViewer (preferred:
        // staff.user_id; fallback: email lookup; surfaces a friendly error
        // when the staff member has no login account yet).
        let user_id: string;
        if (pick.kind === "staff") {
          if (pick.staff.user_id) {
            user_id = pick.staff.user_id;
          } else {
            if (!pick.staff.email) {
              throw new Error(t("permissions.error.staff_no_account"));
            }
            try {
              const resolved: UserSummary = await api.users.lookupByEmail(pick.staff.email);
              user_id = resolved.id;
            } catch {
              throw new Error(t("permissions.error.staff_no_account_named", { name: pick.staff.name }));
            }
          }
        } else {
          let resolved: UserSummary;
          try {
            resolved = await api.users.lookupByEmail(pick.email);
          } catch (err: any) {
            if (err?.status === 404) {
              throw new Error(t("permissions.error.user_not_in_org", { email: pick.email }));
            }
            throw err;
          }
          user_id = resolved.id;
        }
        await api.docPermissions.createGrant(doc.id, {
          subject_type: "user",
          subject_id: user_id,
          capabilities: _docRoleToCaps(role),
          expires_at: opts.expiresAt,
        });
        void opts.notify;
        void opts.message;
        await queryClient.invalidateQueries({ queryKey: ["doc-grants", doc.id] });
      }}
      onUpdateInternalRole={async (grantId, role) => {
        const existing = (grantsQuery.data || []).find((g) => g.id === grantId);
        if (!existing) throw new Error("Grant not found");
        await api.docPermissions.createGrant(doc.id, {
          subject_type: "user",
          subject_id: existing.subject_user_id || existing.subject_id,
          capabilities: _docRoleToCaps(role),
        });
        await queryClient.invalidateQueries({ queryKey: ["doc-grants", doc.id] });
      }}
      onRemoveInternal={async (grantId) => {
        await api.docPermissions.revokeGrant(doc.id, grantId);
        await queryClient.invalidateQueries({ queryKey: ["doc-grants", doc.id] });
      }}
      onCreateExternal={async (config: NewExternalShareConfig) => {
        // Confidential -> submit for admin approval; backend never returns
        // a live URL until an admin decides via the approval inbox.
        if (doc.classification === "confidential") {
          if (!config.approval_reason || !config.approval_reason.trim()) {
            throw new Error(t("permissions.error.confidential_approval_required"));
          }
          await api.docPermissions.requestShareApproval(doc.id, {
            audience_type: config.audience_type,
            audience_value: config.audience_value,
            capabilities: config.capabilities,
            expires_in_days: config.expires_in_days,
            watermark: config.watermark,
            require_otp: config.require_otp,
            allow_download: config.capabilities.includes("download"),
            reason: config.approval_reason.trim(),
          });
          await queryClient.invalidateQueries({ queryKey: ["doc-share-approvals", doc.id] });
          return { pending: true };
        }
        const result = await api.docPermissions.createShare(doc.id, {
          audience_type: config.audience_type,
          audience_value: config.audience_value,
          capabilities: config.capabilities,
          expires_in_days: config.expires_in_days,
          watermark: config.watermark,
          require_otp: config.require_otp,
          allow_download: config.capabilities.includes("download"),
        });
        await queryClient.invalidateQueries({ queryKey: ["doc-shares", doc.id] });
        // Defensive fallback: if the server response is missing `url`
        // (older backend, proxy stripping the field, etc.) but we do
        // have a `token`, reconstruct the link client-side so the
        // dialog can still show it.
        const url = result.url
          || (result.token
            ? `${window.location.origin}/shared-doc/${result.token}`
            : undefined);
        return { url };
      }}
      onRevokeExternal={async (shareId) => {
        await api.docPermissions.revokeShare(doc.id, shareId);
        await queryClient.invalidateQueries({ queryKey: ["doc-shares", doc.id] });
      }}
    />
  );
}

function _docCapsToRole(caps: string[]): "viewer" | "commenter" | "editor" | "curator" {
  const set = new Set(caps);
  if (set.has("manage_metadata") || set.has("grant_access")) return "curator";
  if (set.has("edit")) return "editor";
  if (set.has("comment")) return "commenter";
  return "viewer";
}

function _docRoleToCaps(role: "viewer" | "commenter" | "editor" | "curator"): string[] {
  switch (role) {
    case "viewer":    return ["view"];
    case "commenter": return ["view", "comment"];
    case "editor":    return ["view", "comment", "edit"];
    case "curator":   return ["view", "comment", "edit", "manage_metadata", "grant_access", "share_internal"];
  }
}

function _docGrantToInternal(g: DocumentGrant, userById: Map<string, UserSummary>) {
  let user_email = g.subject_email || g.subject_id;
  let user_name: string | undefined = g.subject_display_name || undefined;
  let avatar_url: string | undefined = g.subject_avatar_url || undefined;
  if (g.subject_type === "user") {
    const u = userById.get(g.subject_user_id || g.subject_id);
    if (u) {
      user_email = g.subject_email || u.email;
      user_name = g.subject_display_name || u.display_name;
      avatar_url = g.subject_avatar_url || u.avatar_url;
    }
  } else {
    user_email = g.subject_display_name || g.subject_email || `${g.subject_type}: ${g.subject_id}`;
  }
  return {
    id: g.id,
    user_email,
    user_name,
    avatar_url,
    role: _docCapsToRole(g.capabilities),
    expires_at: g.expires_at,
    source: "explicit" as const,
  };
}

function _docShareToExternal(s: DocumentShare) {
  return {
    id: s.id,
    audience: s.audience || "anonymous",
    capabilities: s.capabilities,
    expires_at: s.expires_at,
    watermark: s.watermark,
    require_otp: s.require_otp,
    use_count: s.use_count,
    last_used_at: s.last_used_at,
  };
}

/** Same domain-derivation as FileViewer's `_entityDomain` — kept inline
 *  to avoid a 4th caller dragging this into a shared module. */
function _entityDomainFromEmail(email: string | undefined): string | undefined {
  if (!email) return undefined;
  const at = email.indexOf("@");
  if (at <= 0) return undefined;
  const domain = email.slice(at + 1).trim().toLowerCase();
  const personal = new Set([
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
    "icloud.com", "qq.com", "163.com", "126.com", "foxmail.com",
    "protonmail.com", "test.com",
  ]);
  if (!domain || personal.has(domain)) return undefined;
  return domain;
}
