import { useContext } from "react";
import { partitionGeneratedResultFiles, partitionPreviewedResultFiles, type ResultFileMetadata } from "../lib/resultFiles";
import { t } from "../lib/i18n";
import InlineFileReferenceCard from "./InlineFileReferenceCard";
import { PreviewsStartFolded, ResultFilePreview } from "./ChatFilePreview";

type ResultFile = ResultFileMetadata & { key: string; reference: string };

/** Use the same file hierarchy in Chat and Workspace App results. */
export default function GeneratedResultFiles({ files, returnTo }: {
  files: ResultFile[];
  returnTo?: string;
}) {
  const startFolded = useContext(PreviewsStartFolded);
  const { primary, supporting } = partitionGeneratedResultFiles(files);
  if (!primary.length) return null;
  const heading = t("component.workspace_app_view.files");
  // Final files are seen first, as themselves; formats with nothing to show and PDF copies follow as links.
  // No heading: each file names itself on the line above its content.
  const { previewed, listed } = partitionPreviewedResultFiles(primary);
  return <section className="video-result-files" aria-label={heading}>
    {/* Folded rows stack in one column, however many files there are. */}
    {previewed.length > 0 && <div className="video-result-files__primary" data-count={startFolded ? 1 : previewed.length}>
      {previewed.map((file) => <ResultFilePreview key={file.key} file={file} returnTo={returnTo} />)}
    </div>}
    {listed.length > 0 && <div className="video-result-files__list">{listed.map((file) => <InlineFileReferenceCard
      key={file.key} reference={file.reference} label={file.name}
      fileType={file.fileType} mimeType={file.mimeType} returnTo={returnTo}
      compact trustedReference
    />)}</div>}
    {supporting.length > 0 && <details className="video-result-files__supporting">
      <summary>{t("component.result_files.supporting")} <span>{supporting.length}</span></summary>
      <div className="video-result-files__list">{supporting.map((file) => <InlineFileReferenceCard
        key={file.key} reference={file.reference} label={file.name}
        fileType={file.fileType} mimeType={file.mimeType} returnTo={returnTo}
        compact trustedReference
      />)}</div>
    </details>}
  </section>;
}
