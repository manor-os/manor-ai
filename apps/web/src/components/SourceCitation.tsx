import { useState, type ReactNode } from "react";
import { t } from "../lib/i18n";
import { fileReferenceKind } from "../lib/fileReferences";
import { getFileReferenceIcon } from "./contentTypeIcons";
import { IconGlobe } from "./icons";
import AnchoredPopover from "./ui/AnchoredPopover";
import "./SourceCitation.css";

function SiteIcon({ url }: { url: string }) {
  const [failed, setFailed] = useState(false);
  return <span className="chat-source-icon" aria-hidden="true">
    <IconGlobe size={16} />
    {/* Display-only favicons must not require CORS headers from the source site. */}
    {url && !failed && <img src={url} alt="" width={16} height={16}
      referrerPolicy="no-referrer" loading="lazy"
      onError={() => setFailed(true)} />}
  </span>;
}

export default function SourceCitation({ children, label, kind, iconUrl = "", count = 1 }: {
  children: ReactNode;
  label: string;
  kind: "file" | "web";
  iconUrl?: string;
  count?: number;
}) {
  const FileIcon = getFileReferenceIcon(fileReferenceKind(label));
  return (
    <span className="chat-source-reference">
      <AnchoredPopover
        ariaLabel={t("component.chat_markdown.source_details")}
        align="left"
        width={360}
        openOnHover
        panelClassName="chat-source-popover"
        trigger={(
          <button type="button" className="chat-source-link" title={label}>
            {kind === "file"
              ? <span className="chat-source-icon" aria-hidden="true"><FileIcon size={16} /></span>
              : <SiteIcon key={iconUrl} url={iconUrl} />}
            <span className="chat-source-label">{label}</span>
            {count > 1 && <span className="chat-source-count">+{count - 1}</span>}
          </button>
        )}
      >
        <div className="chat-md chat-source-details">{children}</div>
      </AnchoredPopover>
    </span>
  );
}
