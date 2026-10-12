import InlineFileReferenceCard from "../InlineFileReferenceCard";
import {
  groupedWorkflowCollectionFiles,
  type WorkflowFileCollection as WorkflowFileCollectionData,
} from "../../lib/workflowFileCollection";

export default function WorkflowFileCollection({ collection }: { collection: WorkflowFileCollectionData }) {
  const groups = groupedWorkflowCollectionFiles(collection);
  return (
    <div className="workflow-file-collection" aria-label={collection.title}>
      <div className="workflow-file-collection__intro">
        <div>
          <h3>{collection.title}</h3>
          {collection.summary && <p>{collection.summary}</p>}
        </div>
        <span className="workflow-file-collection__count">
          {collection.files.length} {collection.files.length === 1 ? "file" : "files"}
        </span>
      </div>
      {groups.map((group) => (
        <section key={group.label} className="workflow-file-collection__group" aria-label={group.label}>
          <h4>{group.label}</h4>
          <div className="workflow-file-collection__files">
            {group.files.map((file) => (
              <InlineFileReferenceCard
                key={file.documentId}
                reference={file.viewerUrl}
                label={file.name}
                compact
              />
            ))}
          </div>
        </section>
      ))}
      {collection.pendingItems.length > 0 && (
        <section className="workflow-file-collection__pending" aria-label="Still to come">
          <h4>Still to come</h4>
          <ul>{collection.pendingItems.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
        </section>
      )}
    </div>
  );
}
