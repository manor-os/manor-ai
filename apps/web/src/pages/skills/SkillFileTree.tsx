/**
 * SkillFileTree — collapsible file tree for the skill viewer.
 *
 * Turns a flat list of bundle paths ("scripts/office/comment.py") into a
 * nested tree, claude.ai-skill-viewer style: SKILL.md pinned first at the
 * root, folders collapsible (expanded by default), non-previewable files
 * greyed out with a tooltip.
 */
import { useMemo, useState } from "react";
import { t } from "../../lib/i18n";

export interface SkillTreeFile {
  path: string;
  previewable: boolean;
  size?: number;
  reason?: string;
}

interface TreeFolder {
  name: string;
  path: string;
  folders: Map<string, TreeFolder>;
  files: SkillTreeFile[];
}

function buildTree(files: SkillTreeFile[]): TreeFolder {
  const root: TreeFolder = { name: "", path: "", folders: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i];
      let child = node.folders.get(seg);
      if (!child) {
        child = {
          name: seg,
          path: node.path ? `${node.path}/${seg}` : seg,
          folders: new Map(),
          files: [],
        };
        node.folders.set(seg, child);
      }
      node = child;
    }
    node.files.push(file);
  }
  return root;
}

function fileGlyph(path: string): string {
  const name = path.split("/").pop() || "";
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  switch (ext) {
    case "md":
      return "M↓";
    case "py":
      return "py";
    case "sh":
      return "sh";
    case "js":
    case "ts":
    case "tsx":
    case "jsx":
      return "js";
    case "json":
      return "{}";
    case "html":
    case "xml":
    case "xsd":
      return "<>";
    default:
      return "≡";
  }
}

function fileBasename(path: string): string {
  return path.split("/").pop() || path;
}

function FileRow({
  file,
  depth,
  selected,
  onSelect,
}: {
  file: SkillTreeFile;
  depth: number;
  selected: boolean;
  onSelect: (path: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(file.path)}
      title={
        file.previewable
          ? file.path
          : `${file.path} — ${t("page.skills.viewer_not_previewable")}`
      }
      className="skill-tree-row"
      style={{
        opacity: file.previewable ? 1 : 0.55,
        display: "flex",
        alignItems: "center",
        gap: 7,
        width: "100%",
        textAlign: "left",
        padding: "5px 8px",
        paddingLeft: 8 + depth * 14,
        border: "none",
        borderRadius: 8,
        cursor: "pointer",
        background: selected ? "var(--modal-sunken-bg)" : "transparent",
        boxShadow: selected ? "0 1px 2px rgba(28,25,23,0.06)" : "none",
        color: selected ? "var(--text-strong)" : "var(--text-muted)",
      }}
    >
      <span
        aria-hidden
        style={{
          flexShrink: 0,
          width: 18,
          fontSize: 10,
          fontWeight: 600,
          letterSpacing: "0.02em",
          color: "var(--text-muted)",
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        }}
      >
        {fileGlyph(file.path)}
      </span>
      <span
        style={{
          fontSize: 12,
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          fontWeight: selected ? 600 : 400,
        }}
      >
        {fileBasename(file.path)}
      </span>
    </button>
  );
}

function FolderRow({
  folder,
  depth,
  selected,
  onSelect,
}: {
  folder: TreeFolder;
  depth: number;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="skill-tree-row"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          width: "100%",
          textAlign: "left",
          padding: "5px 8px",
          paddingLeft: 8 + depth * 14,
          border: "none",
          borderRadius: 8,
          cursor: "pointer",
          background: "transparent",
          color: "var(--text-muted)",
        }}
      >
        <svg
          aria-hidden
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.5}
          style={{
            flexShrink: 0,
            transition: "transform 0.12s ease",
            transform: open ? "rotate(90deg)" : "none",
          }}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        <span
          style={{
            fontSize: 12,
            fontWeight: 400,
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {folder.name}
        </span>
      </button>
      {open && <FolderChildren folder={folder} depth={depth + 1} selected={selected} onSelect={onSelect} />}
    </div>
  );
}

function FolderChildren({
  folder,
  depth,
  selected,
  onSelect,
}: {
  folder: TreeFolder;
  depth: number;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const folders = [...folder.folders.values()].sort((a, b) =>
    a.name.localeCompare(b.name),
  );
  const files = [...folder.files].sort((a, b) => a.path.localeCompare(b.path));
  return (
    <div>
      {folders.map((child) => (
        <FolderRow
          key={child.path}
          folder={child}
          depth={depth}
          selected={selected}
          onSelect={onSelect}
        />
      ))}
      {files.map((file) => (
        <FileRow
          key={file.path}
          file={file}
          depth={depth}
          selected={selected === file.path}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

export function SkillFileTree({
  files,
  selected,
  onSelect,
}: {
  files: SkillTreeFile[];
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const { pinned, tree } = useMemo(() => {
    const pinnedNames = ["SKILL.md"];
    const pinnedFiles = pinnedNames
      .map((name) => files.find((f) => f.path === name))
      .filter(Boolean) as SkillTreeFile[];
    const rest = files.filter((f) => !pinnedNames.includes(f.path));
    return { pinned: pinnedFiles, tree: buildTree(rest) };
  }, [files]);

  return (
    <nav aria-label={t("page.skills.viewer_files")} style={{ display: "grid", gap: 1 }}>
      {pinned.map((file) => (
        <FileRow
          key={file.path}
          file={file}
          depth={0}
          selected={selected === file.path}
          onSelect={onSelect}
        />
      ))}
      <FolderChildren folder={tree} depth={0} selected={selected} onSelect={onSelect} />
    </nav>
  );
}
