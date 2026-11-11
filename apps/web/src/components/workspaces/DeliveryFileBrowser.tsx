import { useEffect, useRef, useState } from "react";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import InlineResultDocument, { isInlineResultDocument } from "../InlineResultDocument";
import { IconDocument } from "../icons";
import { groupDeliveryFiles, openingDeliveryFile } from "../../lib/deliveryFiles";
import { t } from "../../lib/i18n";
import "./DeliveryFileBrowser.css";

export type DeliveryBrowserFile = {
  key: string;
  id: string;
  name: string;
  path: string;
  fileType: string;
  mimeType: string;
  group?: string;
};

// A grouped delivery as one list. Opening a file shows its content right under its own row, so the
// text is read where the file was chosen, not at the top of the page.
export default function DeliveryFileBrowser({ files }: { files: DeliveryBrowserFile[] }) {
  // One file is open at a time; a click on the open file closes it. Until the reader chooses, the
  // delivery's opening file (its overview) is open.
  const [chosenKey, setChosenKey] = useState<string | null | undefined>(undefined);
  const openKey = chosenKey === undefined ? openingDeliveryFile(files)?.key ?? null : chosenKey;
  const rootRef = useRef<HTMLElement>(null);
  // Bring a newly opened file's row to the top of the view; its content follows directly beneath it.
  useEffect(() => {
    if (!openKey || chosenKey === undefined) return;
    rootRef.current?.querySelector<HTMLElement>(`[data-file-key="${CSS.escape(openKey)}"]`)
      ?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [openKey, chosenKey]);
  if (files.length === 0) return null;
  return <section ref={rootRef} className="delivery-files" aria-label={t("component.workspace_app_view.files")}>
    {groupDeliveryFiles(files).map((section) => <div key={section.group} className="delivery-files__group">
      <h4>{section.group}</h4>
      {section.files.map((file) => {
        const open = file.key === openKey;
        return <div key={file.key} className="delivery-files__item" data-file-key={file.key}>
          <button type="button" className="delivery-files__row" aria-expanded={open}
            onClick={() => setChosenKey(open ? null : file.key)}>
            <span className="delivery-files__tile" aria-hidden="true"><IconDocument size={16} /></span>
            <span className="delivery-files__name">{file.name}</span>
          </button>
          {open && <div className="delivery-files__body">
            {isInlineResultDocument(file) && <InlineResultDocument documentId={file.id} name={file.name}
              mimeType={file.mimeType} fileType={file.fileType} />}
            <InlineFileReferenceCard reference={file.path} label={file.name} fileType={file.fileType}
              mimeType={file.mimeType} compact trustedReference />
          </div>}
        </div>;
      })}
    </div>)}
  </section>;
}
