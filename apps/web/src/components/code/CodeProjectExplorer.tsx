import { useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { isCodeLikeFile } from "../../lib/codeFiles";
import { IconChevronDown, IconChevronRight, IconDocument, IconFolder } from "../icons";
import type { CodeWorkspaceFileReference, CodeWorkspaceSaveStatus } from "../../lib/useCodeProjectWorkspace";
import "./CodeProjectWorkspace.css";

interface FileSystemItem {
  name: string;
  path: string;
  type: "directory" | "file";
  mime_type?: string | null;
  extension?: string | null;
}

interface CodeProjectExplorerProps {
  rootPath: string;
  rootName: string;
  activePath: string | null;
  openPaths: Set<string>;
  fileStatuses: Record<string, CodeWorkspaceSaveStatus>;
  onOpenFile: (file: CodeWorkspaceFileReference) => void;
}

interface DirectoryBranchProps extends CodeProjectExplorerProps {
  path: string;
  name: string;
  depth: number;
  initiallyExpanded?: boolean;
}

function fileKind(name: string): string {
  const extension = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toUpperCase() : "";
  return extension.slice(0, 4) || "TXT";
}

function DirectoryBranch({
  path,
  name,
  depth,
  initiallyExpanded = false,
  activePath,
  openPaths,
  fileStatuses,
  onOpenFile,
  rootPath,
  rootName,
}: DirectoryBranchProps) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const directoryQuery = useQuery({
    queryKey: ["code-project-directory", path],
    queryFn: () => api.fs.list(path),
    enabled: expanded,
    staleTime: 30_000,
  });
  const items = (directoryQuery.data?.items || []) as FileSystemItem[];
  const rowStyle = { "--tree-depth": depth } as CSSProperties;

  return (
    <li className="code-project-tree__branch" role="treeitem" aria-expanded={expanded}>
      <button
        type="button"
        className="code-project-tree__row code-project-tree__folder"
        style={rowStyle}
        onClick={() => setExpanded((value) => !value)}
        title={path}
      >
        {expanded ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
        <IconFolder size={15} />
        <span>{name}</span>
      </button>

      {expanded && (
        <ul className="code-project-tree__group" role="group">
          {directoryQuery.isLoading && (
            <li className="code-project-tree__message" style={{ "--tree-depth": depth + 1 } as CSSProperties}>
              <span className="code-project-tree__loading-dot" /> Loading…
            </li>
          )}
          {directoryQuery.isError && (
            <li className="code-project-tree__message is-error" style={{ "--tree-depth": depth + 1 } as CSSProperties}>
              <span>Unable to load folder</span>
              <button type="button" onClick={() => void directoryQuery.refetch()}>Retry</button>
            </li>
          )}
          {!directoryQuery.isLoading && !directoryQuery.isError && items.length === 0 && (
            <li className="code-project-tree__message" style={{ "--tree-depth": depth + 1 } as CSSProperties}>Empty folder</li>
          )}
          {items.map((item) => {
            if (item.type === "directory") {
              return (
                <DirectoryBranch
                  key={item.path}
                  path={item.path}
                  name={item.name}
                  depth={depth + 1}
                  rootPath={rootPath}
                  rootName={rootName}
                  activePath={activePath}
                  openPaths={openPaths}
                  fileStatuses={fileStatuses}
                  onOpenFile={onOpenFile}
                />
              );
            }

            const editable = isCodeLikeFile({ name: item.name, mime_type: item.mime_type, file_type: item.extension });
            const isActive = item.path === activePath;
            const status = fileStatuses[item.path];
            return (
              <li key={item.path} role="treeitem">
                <button
                  type="button"
                  className={`code-project-tree__row code-project-tree__file${isActive ? " is-active" : ""}${openPaths.has(item.path) ? " is-open" : ""}`}
                  style={{ "--tree-depth": depth + 1 } as CSSProperties}
                  onClick={() => editable && onOpenFile({ path: item.path, name: item.name, mimeType: item.mime_type })}
                  disabled={!editable}
                  aria-current={isActive ? "page" : undefined}
                  title={editable ? item.path : `${item.path} · Preview-only asset`}
                >
                  <span className="code-project-tree__spacer" />
                  <IconDocument size={14} />
                  <span className="code-project-tree__filename">{item.name}</span>
                  <span className="code-project-tree__kind">{fileKind(item.name)}</span>
                  {status && status !== "saved" && <span className={`code-project-tree__status is-${status}`} aria-label={status} />}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

export default function CodeProjectExplorer(props: CodeProjectExplorerProps) {
  return (
    <nav className="code-project-explorer" aria-label="Project files">
      <ul className="code-project-tree" role="tree">
        <DirectoryBranch
          {...props}
          path={props.rootPath}
          name={props.rootName}
          depth={0}
          initiallyExpanded
        />
      </ul>
    </nav>
  );
}
