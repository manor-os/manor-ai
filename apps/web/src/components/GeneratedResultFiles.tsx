import { partitionGeneratedResultFiles, partitionPreviewedResultFiles, type ResultFileMetadata } from "../lib/resultFiles";
import { t } from "../lib/i18n";
import InlineFileReferenceCard from "./InlineFileReferenceCard";
import GeneratedFileCard from "./GeneratedFileCard";

type ResultFile = ResultFileMetadata & { key: string; reference: string };

/** Use the same file hierarchy in Chat and Workspace App results. */
export default function GeneratedResultFiles({ files, returnTo }: {
  files: ResultFile[];
  returnTo?: string;
}) {
  const { primary, supporting } = partitionGeneratedResultFiles(files);
  if (!primary.length) return null;
  const heading = t("component.workspace_app_view.files");
  // Final files are seen first, as in a review; their text and PDF copies follow as links.
  const { previewed, listed } = partitionPreviewedResultFiles(primary);
  return <section className="video-result-files" aria-label={heading}>
    <h4>{heading}</h4>
    {previewed.length > 0 && <div className="video-result-files__primary" data-count={previewed.length}>
      {previewed.map((file) => <GeneratedFileCard key={file.key} file={file} returnTo={returnTo} />)}
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
