/**
 * SkillFilesEditor — edit a skill's bundled files (scripts/, references/,
 * requirements.txt) inside the skill form modal.
 *
 * The list uses replace semantics: whatever is here on save becomes the
 * bundle, so removing a row deletes the file. SKILL.md is not listed — the
 * system-prompt field above the editor is the SKILL.md body.
 */
import { useState } from "react";
import Button from "../../components/ui/Button";
import LoadingSpinner from "../../components/ui/LoadingSpinner";
import { t } from "../../lib/i18n";

export interface BundleFile {
  path: string;
  content: string;
}

const MONO =
  "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace";

function normalizePath(raw: string): string {
  const safe = raw.replace(/\\/g, "/").trim().replace(/^\/+/, "");
  if (!safe || safe.includes("..")) return "";
  if (safe === "SKILL.md" || safe === "config.json" || safe === "credentials.json") return "";
  return safe;
}

export function SkillFilesEditor({
  files,
  onChange,
  loading,
}: {
  files: BundleFile[];
  onChange: (files: BundleFile[]) => void;
  loading?: boolean;
}) {
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [newPath, setNewPath] = useState("");

  const addFile = () => {
    const path = normalizePath(newPath);
    if (!path || files.some((f) => f.path === path)) return;
    onChange([...files, { path, content: "" }]);
    setNewPath("");
    setOpenPath(path);
  };

  const removeFile = (path: string) => {
    onChange(files.filter((f) => f.path !== path));
    if (openPath === path) setOpenPath(null);
  };

  const setContent = (path: string, content: string) => {
    onChange(files.map((f) => (f.path === path ? { ...f, content } : f)));
  };

  return (
    <div>
      <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">
        {t("page.skill_form.files")}
      </label>
      <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
        {t("page.skill_form.files_hint")}
      </p>

      {loading ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 16 }}>
          <LoadingSpinner size={18} />
        </div>
      ) : (
        <div style={{ borderRadius: 12, overflow: "hidden" }}>
          {files.length === 0 && (
            <p
              style={{
                margin: 0,
                padding: "12px 14px",
                fontSize: 12,
                color: "var(--text-muted)",
              }}
            >
              {t("page.skill_form.no_files")}
            </p>
          )}
          {files.map((file, index) => {
            const open = openPath === file.path;
            return (
              <div
                key={file.path}
                style={{
                  borderTop: index > 0 ? "1px solid rgba(28,25,23,0.06)" : "none",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "2px 6px 2px 0" }}>
                  <button
                    type="button"
                    onClick={() => setOpenPath(open ? null : file.path)}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      textAlign: "left",
                      padding: "8px 12px",
                      border: "none",
                      background: "transparent",
                      cursor: "pointer",
                      color: "var(--text-default)",
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
                        color: "var(--text-muted)",
                        transition: "transform 0.12s ease",
                        transform: open ? "rotate(90deg)" : "none",
                      }}
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                    </svg>
                    <span
                      style={{
                        fontSize: 12,
                        fontFamily: MONO,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {file.path}
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => removeFile(file.path)}
                    title={t("action.delete")}
                    style={{
                      flexShrink: 0,
                      width: 26,
                      height: 26,
                      borderRadius: 8,
                      border: "none",
                      background: "transparent",
                      color: "var(--text-muted)",
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
                      <path d="M18 6L6 18M6 6l12 12" />
                    </svg>
                  </button>
                </div>
                {open && (
                  <div style={{ padding: "0 10px 10px" }}>
                    <textarea
                      value={file.content}
                      onChange={(e) => setContent(file.path, e.target.value)}
                      rows={10}
                      spellCheck={false}
                      className="manor-textarea"
                      style={{ fontSize: 12, lineHeight: 1.6, fontFamily: MONO }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <input
          value={newPath}
          onChange={(e) => setNewPath(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addFile();
            }
          }}
          placeholder={t("page.skill_form.add_file_placeholder")}
          className="manor-input"
          style={{ flex: 1, fontFamily: MONO, fontSize: 12 }}
        />
        <Button variant="outline" size="sm" onClick={addFile} disabled={!normalizePath(newPath)}>
          {t("page.skill_form.add_file")}
        </Button>
      </div>
    </div>
  );
}
