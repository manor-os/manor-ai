import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import { decodeFileReferenceHref, fileNameFromReference, fileReferenceKind } from "../lib/fileReferences";
import { resultFilePreviewTarget, type ResultFileMetadata } from "../lib/resultFiles";
import { t } from "../lib/i18n";
import GeneratedFileCard from "./GeneratedFileCard";
import { inlineDocumentTextKind } from "./InlineDocumentText";
import { documentThumbnailKind } from "./ui/useDocumentThumbnail";

const NAMED = /\.[a-z0-9]{2,5}$/i;

/** A Workspace result's files start as one row each and expand in the message. */
export const PreviewsStartFolded = createContext(false);

/** The name that says what a linked file is: the label when it names one, else the link's own path. */
export function previewName(reference: string, label?: string): string {
  if (label && NAMED.test(label.trim())) return label.trim();
  const fromPath = fileNameFromReference(decodeFileReferenceHref(reference) || reference);
  return NAMED.test(fromPath) ? fromPath : label || fromPath;
}

/** Whether a reply's link to this file shows the file itself rather than a chip. */
export function previewable(name: string, documentLinked: boolean, fileType?: string, mimeType?: string): boolean {
  const kind = fileReferenceKind(name, mimeType, fileType);
  if (kind === "image" || kind === "video") return true;
  // Pages and written text come from the Document, so they need its id.
  return documentLinked && Boolean(documentThumbnailKind(name) || inlineDocumentTextKind(name, fileType));
}

/**
 * A file a reply links to, shown in place (an image, a video, a document's
 * first page or its text) under a row that folds it away. Anything that cannot
 * be shown keeps the plain file chip (`fallback`).
 */
export default function ChatFilePreview({ reference, label, returnTo, fallback }: {
  reference: string;
  label?: string;
  returnTo?: string;
  fallback: JSX.Element;
}) {
  const startFolded = useContext(PreviewsStartFolded);
  const target = resultFilePreviewTarget(reference, window.location.origin);
  const documentId = target && "documentId" in target ? target.documentId : null;
  const guessed = previewName(reference, label);
  // A /viewer/<id> link under a label like "the screenshot" learns its type from the Document.
  const lookup = Boolean(documentId) && !NAMED.test(guessed);
  const [document, setDocument] = useState<{ name: string; file_type?: string; mime_type?: string } | null>(null);
  useEffect(() => {
    if (!lookup || !documentId) return;
    const abort = new AbortController();
    api.documents.get(documentId, abort.signal).then(setDocument).catch(() => undefined);
    return () => abort.abort();
  }, [lookup, documentId]);
  const name = NAMED.test(guessed) ? guessed : document?.name || guessed;
  const fileType = document?.file_type;
  const mimeType = document?.mime_type;
  if (!target || !previewable(name, Boolean(documentId), fileType, mimeType)) return fallback;

  return (
    <FoldedPreview label={label || name}>
      <GeneratedFileCard file={{ name, reference, fileType, mimeType }} returnTo={returnTo}
        openLabel={startFolded ? t("component.chat_action_card.open_link") : undefined} />
    </FoldedPreview>
  );
}

/** Something a reply shows in place, under a row that folds it away. */
export function FoldedPreview({ label, children }: { label: string; children: ReactNode }) {
  const startFolded = useContext(PreviewsStartFolded);
  const [open, setOpen] = useState(!startFolded);
  const action = t(open ? "component.chat_file_preview.collapse" : "component.chat_file_preview.expand");
  const chevron = (
    <svg className="chat-file-preview__chevron" width="12" height="12" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth={2.5} aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
    </svg>
  );
  return (
    <span className={`chat-file-preview${startFolded ? " chat-file-preview--result" : ""}${open ? " is-open" : ""}`}>
      <button type="button" className="chat-file-preview__toggle" aria-expanded={open}
        onClick={() => setOpen((value) => !value)} title={action}>
        {startFolded ? <>
          <span className="chat-file-preview__label">{label}</span>
          <span className="chat-file-preview__action">{action}{chevron}</span>
        </> : <>
          {chevron}
          <span className="chat-file-preview__label">{label}</span>
        </>}
      </button>
      {open && <span className="chat-file-preview__body">{children}</span>}
    </span>
  );
}

/** A final file shown as itself, or as one row that expands to it in a Workspace result. */
export function ResultFilePreview({ file, returnTo }: {
  file: ResultFileMetadata & { reference: string };
  returnTo?: string;
}) {
  if (!useContext(PreviewsStartFolded)) return <GeneratedFileCard file={file} returnTo={returnTo} />;
  return <FoldedPreview label={file.name}>
    <GeneratedFileCard file={file} returnTo={returnTo} openLabel={t("component.chat_action_card.open_link")} />
  </FoldedPreview>;
}
