import React, { useState } from "react";
import { createPortal } from "react-dom";
import Button from "../../components/ui/Button";
import LoadingSpinner from "../../components/ui/LoadingSpinner";
import { useToastStore } from "../../stores/toast";
import { api } from "../../lib/api";
import { ParsedSkill } from "./skillTypes";

import { t } from "../../lib/i18n";
interface ImportSkillsDialogProps {
  open: boolean;
  onClose: () => void;
  onImported: () => void;
}

export function ImportSkillsDialog({
  open,
  onClose,
  onImported,
}: ImportSkillsDialogProps) {
  const toast = useToastStore();
  const [reading, setReading] = useState(false);
  const [parseError, setParseError] = useState("");
  const [parsedSkills, setParsedSkills] = useState<ParsedSkill[]>([]);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<{
    imported: number;
    skipped: number;
    failed: number;
  } | null>(null);
  const folderInputRef = React.useRef<HTMLInputElement>(null);

  const reset = () => {
    setReading(false);
    setParseError("");
    setParsedSkills([]);
    setImporting(false);
    setImportResult(null);
    if (folderInputRef.current) folderInputRef.current.value = "";
  };

  React.useEffect(() => {
    if (open) reset();
  }, [open]);

  const validSkills = parsedSkills.filter((s) => s.valid);
  const invalidCount = parsedSkills.filter((s) => !s.valid).length;
  const selectedCount = parsedSkills.filter(
    (s) => s.valid && s.selected,
  ).length;
  const allSelected =
    validSkills.length > 0 && validSkills.every((s) => s.selected);
  const someSelected = validSkills.some((s) => s.selected);

  const handleSelectAll = (checked: boolean) => {
    setParsedSkills((prev) =>
      prev.map((s) => (s.valid ? { ...s, selected: checked } : s)),
    );
  };

  const handleRowCheck = (idx: number, checked: boolean) => {
    setParsedSkills((prev) =>
      prev.map((s, i) => (i === idx ? { ...s, selected: checked } : s)),
    );
  };

  function readFileText(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () =>
        reject(new Error(t("page.import_skills_dialog.failed_to_read_file")));
      reader.readAsText(file);
    });
  }

  function parseFrontmatter(content: string): {
    meta: Record<string, any> | null;
    body: string;
  } {
    const match = content.match(/^---\s*\n([\s\S]*?)\n---\s*\n([\s\S]*)$/);
    if (!match) return { meta: null, body: content };
    const meta: Record<string, any> = {};
    for (const line of match[1].split("\n")) {
      const idx = line.indexOf(":");
      if (idx <= 0) continue;
      const key = line.slice(0, idx).trim();
      let val: any = line.slice(idx + 1).trim();
      if (
        (val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))
      )
        val = val.slice(1, -1);
      if (val.startsWith("[") && val.endsWith("]")) {
        try {
          val = JSON.parse(val);
        } catch {
          /* keep */
        }
      }
      meta[key] = val;
    }
    return { meta, body: match[2].trim() };
  }

  function detectSkillFolders(fileMap: Record<string, File>) {
    const paths = Object.keys(fileMap);
    const candidates: any[] = [];
    const seenPrefixes = new Set<string>();
    const SKILL_FILES = new Set([
      "skill.md",
      "config.json",
      "_meta.json",
      "manor.json",
    ]);

    const folderIndex = new Map<
      string,
      { files: Set<string>; dirParts: string[] }
    >();
    for (const p of paths) {
      const parts = p.split("/");
      if (parts.length < 2) continue;
      const fileName = parts[parts.length - 1].toLowerCase();
      const dirKey = parts.slice(0, -1).join("/");
      if (!folderIndex.has(dirKey))
        folderIndex.set(dirKey, {
          files: new Set(),
          dirParts: parts.slice(0, -1),
        });
      folderIndex.get(dirKey)!.files.add(fileName);
    }

    for (const [dirKey, info] of folderIndex) {
      if (![...info.files].some((f) => SKILL_FILES.has(f))) continue;
      const childHasSkill = [...folderIndex.entries()].some(
        ([k, v]) =>
          k !== dirKey &&
          k.startsWith(dirKey + "/") &&
          [...v.files].some((f) => SKILL_FILES.has(f)),
      );
      if (childHasSkill) continue;
      seenPrefixes.add(dirKey);
      const folderName = info.dirParts[info.dirParts.length - 1];
      const findCI = (name: string) =>
        paths.find(
          (p) => p.toLowerCase() === `${dirKey}/${name}`.toLowerCase(),
        ) || null;
      candidates.push({
        type: "folder",
        folder: folderName,
        prefix: dirKey,
        dirParts: info.dirParts,
        skillMdPath: findCI("SKILL.md"),
        configPath: findCI("config.json"),
        metaPath: findCI("_meta.json"),
        manorJsonPath: findCI("manor.json"),
      });
    }
    for (const p of paths) {
      if (!p.endsWith(".json")) continue;
      const parts = p.split("/");
      const fileName = parts[parts.length - 1].toLowerCase();
      if (SKILL_FILES.has(fileName)) continue;
      const parentPrefix = parts.slice(0, -1).join("/");
      if (seenPrefixes.has(parentPrefix)) continue;
      candidates.push({
        type: "json",
        folder: fileName.replace(/\.json$/, ""),
        jsonPath: p,
      });
    }
    return candidates;
  }

  async function parseSkillCandidate(
    candidate: any,
    fileMap: Record<string, File>,
  ): Promise<ParsedSkill> {
    const result: ParsedSkill = {
      folder: candidate.folder,
      name: "",
      description: "",
      prompt: "",
      tags: [],
      is_public: false,
      version: "1.0.0",
      valid: false,
      selected: false,
      error: "",
    };
    try {
      if (candidate.type === "folder") {
        let config: any = null;
        for (const key of [
          "configPath",
          "manorJsonPath",
          "metaPath",
        ] as const) {
          if (config || !candidate[key] || !fileMap[candidate[key]]) continue;
          try {
            const raw = await readFileText(fileMap[candidate[key]]);
            const parsed = JSON.parse(raw);
            config =
              key === "metaPath"
                ? {
                    name:
                      parsed.displayName || parsed.name || parsed.slug || "",
                    description: parsed.description || "",
                    tags: Array.isArray(parsed.tags) ? parsed.tags : [],
                    version: parsed.latest?.version || "1.0.0",
                  }
                : parsed;
          } catch {
            /* skip */
          }
        }
        let promptContent = "";
        let frontmatterMeta: any = null;
        if (candidate.skillMdPath && fileMap[candidate.skillMdPath]) {
          const raw = await readFileText(fileMap[candidate.skillMdPath]);
          const { meta, body } = parseFrontmatter(raw);
          frontmatterMeta = meta;
          promptContent = body || raw;
        }
        if (!config && frontmatterMeta) {
          config = {
            name: frontmatterMeta.name || "",
            description: frontmatterMeta.description || "",
            tags: Array.isArray(frontmatterMeta.tags)
              ? frontmatterMeta.tags
              : [],
            version: frontmatterMeta.version || "1.0.0",
          };
        }
        config = config || {};
        result.name = (config.name || "").trim() || candidate.folder;
        result.description =
          config.description || frontmatterMeta?.description || "";
        result.tags = Array.isArray(config.tags) ? config.tags : [];
        result.version = config.version || "1.0.0";
        result.prompt =
          promptContent ||
          (typeof config.prompt === "string" ? config.prompt : "");
        if (!result.prompt.trim()) {
          result.error = t(
            "page.import_skills_dialog.missing_skill_md_or_prompt",
          );
          return result;
        }
        result.valid = true;
        result.selected = true;
      } else if (candidate.type === "json") {
        const text = await readFileText(fileMap[candidate.jsonPath]);
        let data: any;
        try {
          data = JSON.parse(text);
        } catch {
          result.error = t("page.import_skills_dialog.not_valid_json");
          return result;
        }
        if (!(data.name || data.displayName || data.slug || "").trim()) {
          result.error = t("page.import_skills_dialog.json_missing_name_field");
          return result;
        }
        if (!data.prompt?.trim()) {
          result.error = t("page.import_skills_dialog.json_missing_prompt_field");
          return result;
        }
        result.name = (data.name || data.displayName || data.slug || "").trim();
        result.description = data.description || "";
        result.prompt = data.prompt;
        result.tags = Array.isArray(data.tags) ? data.tags : [];
        result.version = data.version || "1.0.0";
        if (data.id) result.id = data.id;
        result.valid = true;
        result.selected = true;
      }
    } catch (err: any) {
      result.error = err.message || t("page.import_skills_dialog.parse_error");
    }
    return result;
  }

  async function parseFiles(fileList: File[]) {
    setReading(true);
    setParseError("");
    setParsedSkills([]);
    try {
      const fileMap: Record<string, File> = {};
      for (const f of fileList) {
        const rp = (f as any).webkitRelativePath || f.name;
        fileMap[rp] = f;
      }
      const candidates = detectSkillFolders(fileMap);
      if (!candidates.length) {
        setParseError(
          t("page.import_skills_dialog.no_valid_skill_structures_found"),
        );
        setReading(false);
        return;
      }
      const parsed = await Promise.all(
        candidates.map((c) => parseSkillCandidate(c, fileMap)),
      );
      setParsedSkills(parsed);
    } catch (err: any) {
      setParseError(
        err.message || t("page.import_skills_dialog.failed_to_read_files"),
      );
    } finally {
      setReading(false);
    }
  }

  const handleFolderSelected = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) parseFiles(Array.from(e.target.files));
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const items = e.dataTransfer.items;
    if (!items?.length) return;
    const files: File[] = [];
    const readEntry = async (entry: any, path = ""): Promise<void> => {
      if (entry.isFile) {
        const file: File = await new Promise((res) => entry.file(res));
        Object.defineProperty(file, "webkitRelativePath", {
          value: path + entry.name,
          writable: false,
        });
        files.push(file);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        const entries: any[] = await new Promise((res) =>
          reader.readEntries(res),
        );
        for (const child of entries)
          await readEntry(child, path + entry.name + "/");
      }
    };
    const entry = items[0].webkitGetAsEntry?.();
    if (entry)
      readEntry(entry).then(() => {
        if (files.length) parseFiles(files);
      });
    else if (e.dataTransfer.files.length)
      parseFiles(Array.from(e.dataTransfer.files));
  };

  const doImport = async () => {
    const selected = parsedSkills.filter((s) => s.valid && s.selected);
    if (!selected.length) return;
    setImporting(true);
    try {
      const result = await api.skills.batchImport(
        selected.map((s) => ({
          name: s.name,
          prompt: s.prompt,
          description: s.description,
          tags: s.tags,
          is_public: s.is_public,
          version: s.version,
        })),
      );
      setImportResult(result);
      if (result.imported > 0) {
        const message = t("page.import_skills_dialog.imported_skills_toast")
          .replace("{count}", String(result.imported));
        const skipped = result.skipped
          ? ` ${t("page.import_skills_dialog.skipped_count_suffix").replace("{count}", String(result.skipped))}`
          : "";
        toast.success(
          `${message}${skipped}`,
        );
        onImported();
      } else if (result.skipped > 0) {
        toast.success(t("page.import_skills_dialog.all_skills_already_exist_skipped"));
      } else {
        toast.error(t("page.import_skills_dialog.import_failed"));
      }
    } catch (err: any) {
      toast.error(
        err.message || t("page.import_skills_dialog.import_failed"),
      );
    } finally {
      setImporting(false);
    }
  };

  if (!open) return null;

  return createPortal(
    <div
      className="manor-dialog-overlay"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 20000,
        background: "var(--modal-overlay-bg)",
        backdropFilter: "blur(5px)",
        WebkitBackdropFilter: "blur(5px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 16,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="manor-dialog import-skills-dialog"
        style={{
          background: "var(--modal-bg)",
          backdropFilter: "blur(20px) saturate(1.08)",
          WebkitBackdropFilter: "blur(20px) saturate(1.08)",
          borderRadius: 24,
          width: "100%",
          maxWidth: 700,
          border: "1px solid var(--modal-border)",
          boxShadow: "var(--modal-shadow)",
          display: "flex",
          flexDirection: "column",
          maxHeight: "85vh",
        }}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "20px 24px 0",
          }}
        >
          <h2
            style={{ margin: 0, fontSize: 17, fontWeight: 800, color: "var(--text-strong)" }}
          >
            {t("page.skills.import_skills")}
          </h2>
          <button
            onClick={onClose}
            style={{
              width: 32,
              height: 32,
              borderRadius: "50%",
              border: "none",
              background: "var(--modal-muted-bg)",
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--text-faint)",
            }}
          >
            <svg
              style={{ width: 16, height: 16 }}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflowY: "auto", padding: "16px 24px" }}>
          {!parsedSkills.length && !parseError && (
            <>
              <div
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 12,
                  padding: "12px 16px",
                  background: "rgba(28,25,23,0.06)",
                  border: "1px solid rgba(28,25,23,0.2)",
                  borderRadius: 12,
                  marginBottom: 16,
                }}
              >
                <svg
                  style={{
                    width: 16,
                    height: 16,
                    color: "#57534e",
                    flexShrink: 0,
                    marginTop: 1,
                  }}
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2}
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                  />
                </svg>
                <div>
                  <p
                    style={{
                      margin: "0 0 2px",
                      fontSize: 12,
                      fontWeight: 700,
                      color: "#292524",
                    }}
                  >
                    {t("page.import_skills_dialog.supported_formats")}
                  </p>
                  <p
                    style={{
                      margin: 0,
                      fontSize: 12,
                      color: "#78716c",
                      lineHeight: 1.6,
                    }}
                  >
                    {t("page.import_skills_dialog.select_a_folder_containing_skill_sub_folders_eac")} <code>{t("page.import_skills_dialog.skill_md")}</code> {t("page.import_skills_dialog.file_the_prompt_with_optional")}{" "}
                    <code>{t("page.import_skills_dialog.config_json")}</code> / <code>{t("page.import_skills_dialog.manor_json")}</code> {t("page.import_skills_dialog.for_metadata_standalone_json_files_with")} <code>{t("page.import_skills_dialog.name")}</code> +{" "}
                    <code>{t("page.import_skills_dialog.prompt")}</code> {t("page.import_skills_dialog.fields_are_also_supported")}
                  </p>
                </div>
              </div>

              {/* Drop zone */}
              <div
                onClick={() => folderInputRef.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={handleDrop}
                style={{
                  border: "3px dashed rgba(28,25,23,0.06)",
                  borderRadius: 20,
                  padding: "48px 24px",
                  textAlign: "center",
                  cursor: "pointer",
                  transition: "all 0.2s",
                  background:
                    "linear-gradient(180deg, rgba(255,255,255,0.5) 0%, #fafaf9 100%)",
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLElement).style.borderColor =
                    "rgba(28,25,23,0.4)";
                  (e.currentTarget as HTMLElement).style.background =
                    "rgba(28,25,23,0.03)";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLElement).style.borderColor = "#e7e5e4";
                  (e.currentTarget as HTMLElement).style.background =
                    "linear-gradient(180deg, rgba(255,255,255,0.5) 0%, #fafaf9 100%)";
                }}
              >
                <input
                  ref={folderInputRef}
                  type="file"
                  style={{ display: "none" }}
                  // @ts-ignore webkitdirectory is non-standard
                  webkitdirectory=""
                  multiple
                  onChange={handleFolderSelected}
                />
                <div
                  style={{
                    width: 64,
                    height: 64,
                    background: "#f5f5f4",
                    borderRadius: "50%",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    margin: "0 auto 12px",
                    transition: "all 0.2s",
                  }}
                >
                  <svg
                    style={{ width: 28, height: 28, color: "#a8a29e" }}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={1.5}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z"
                    />
                  </svg>
                </div>
                <p
                  style={{
                    fontSize: 14,
                    fontWeight: 700,
                    color: "#44403c",
                    margin: "0 0 4px",
                  }}
                >
                  {t("page.import_skills_dialog.click_to_select_a_folder")}
                </p>
                <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>
                  {t("page.import_skills_dialog.or_drag_drop_files_here")}
                </p>
              </div>

              {reading && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: 8,
                    padding: 16,
                    color: "#78716c",
                    fontSize: 13,
                  }}
                >
                  <LoadingSpinner size={14} />
                  <span>{t("page.import_skills_dialog.reading_files")}</span>
                </div>
              )}
            </>
          )}

          {parseError && (
            <div style={{ textAlign: "center", padding: "32px 16px" }}>
              <div
                style={{
                  width: 56,
                  height: 56,
                  margin: "0 auto 12px",
                  borderRadius: "50%",
                  background: "#f8f0ef",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <svg
                  style={{ width: 28, height: 28, color: "#d65f59" }}
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={1.5}
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                  />
                </svg>
              </div>
              <p
                style={{
                  fontSize: 15,
                  fontWeight: 700,
                  color: "#292524",
                  margin: "0 0 6px",
                }}
              >
                {t("page.import_skills_dialog.format_validation_failed")}
              </p>
              <p
                style={{
                  fontSize: 12,
                  color: "#78716c",
                  margin: "0 0 16px",
                  lineHeight: 1.6,
                }}
              >
                {parseError}
              </p>
              <Button variant="outline" size="sm" onClick={reset}>
                {t("page.import_skills_dialog.try_again")}
              </Button>
            </div>
          )}

          {parsedSkills.length > 0 && !importResult && (
            <div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  marginBottom: 12,
                  flexWrap: "wrap",
                }}
              >
                <span
                  style={{ fontSize: 13, fontWeight: 700, color: "#44403c" }}
                >
                  {t("page.import_skills_dialog.found_skills_count").replace(
                    "{count}",
                    String(parsedSkills.length),
                  )}
                </span>
                {invalidCount > 0 && (
                  <span
                    style={{
                      fontSize: 12,
                      fontWeight: 600,
                      color: "#d65f59",
                      background: "#f8f0ef",
                      padding: "2px 8px",
                      borderRadius: 6,
                    }}
                  >
                    {invalidCount} {t("page.import_skills_dialog.invalid")}
                  </span>
                )}
                <button
                  onClick={reset}
                  style={{
                    marginLeft: "auto",
                    fontSize: 12,
                    color: "#78716c",
                    background: "none",
                    border: "none",
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                    fontWeight: 600,
                  }}
                >
                  <svg
                    style={{ width: 12, height: 12 }}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"
                    />
                  </svg>
                  {t("page.import_skills_dialog.re_select")}
                </button>
              </div>

              <div
                style={{
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 12,
                  overflow: "hidden",
                  maxHeight: 320,
                  overflowY: "auto",
                }}
              >
                <table
                  style={{
                    width: "100%",
                    borderCollapse: "collapse",
                    fontSize: 13,
                  }}
                >
                  <thead>
                    <tr
                      style={{
                        background: "#fafaf9",
                        borderBottom: "1px solid rgba(28,25,23,0.06)",
                      }}
                    >
                      <th
                        style={{
                          padding: "10px 12px",
                          width: 36,
                          textAlign: "center",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={allSelected}
                          ref={(el) => {
                            if (el)
                              el.indeterminate = someSelected && !allSelected;
                          }}
                          onChange={(e) => handleSelectAll(e.target.checked)}
                        />
                      </th>
                      {[
                        t("page.import_skills_dialog.name_column"),
                        t("page.import_skills_dialog.description_column"),
                        t("page.import_skills_dialog.status_column"),
                      ].map((h) => (
                        <th
                          key={h}
                          style={{
                            padding: "10px 12px",
                            textAlign: "left",
                            fontSize: 11,
                            fontWeight: 700,
                            color: "#78716c",
                            textTransform: "uppercase",
                            letterSpacing: "0.05em",
                          }}
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {parsedSkills.map((item, idx) => (
                      <tr
                        key={idx}
                        style={{
                          borderBottom: "1px solid #f5f5f4",
                          background: item.valid ? "#fff" : "#f8f0ef",
                        }}
                      >
                        <td
                          style={{ padding: "10px 12px", textAlign: "center" }}
                        >
                          <input
                            type="checkbox"
                            disabled={!item.valid}
                            checked={item.valid && item.selected}
                            onChange={(e) =>
                              handleRowCheck(idx, e.target.checked)
                            }
                          />
                        </td>
                        <td
                          style={{
                            padding: "10px 12px",
                            fontWeight: 600,
                            color: item.valid ? "#292524" : "#a8a29e",
                            maxWidth: 180,
                          }}
                        >
                          {item.name || item.folder || "—"}
                        </td>
                        <td
                          style={{
                            padding: "10px 12px",
                            color: "#78716c",
                            fontSize: 12,
                            maxWidth: 200,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {item.description || "—"}
                        </td>
                        <td style={{ padding: "10px 12px" }}>
                          {item.valid ? (
                            <span
                              style={{
                                fontSize: 12,
                                fontWeight: 600,
                                color: "#44895f",
                              }}
                            >
                              {t("page.import_skills_dialog.valid")}
                            </span>
                          ) : (
                            <span
                              style={{
                                fontSize: 12,
                                fontWeight: 600,
                                color: "#d65f59",
                              }}
                              title={item.error}
                            >
                              ✗ {item.error}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {importResult && (
            <div
              style={{
                display: "flex",
                gap: 20,
                flexWrap: "wrap",
                fontSize: 13,
                fontWeight: 700,
                padding: "8px 0",
              }}
            >
              <span style={{ color: "#44895f" }}>
                {t("page.import_skills_dialog.imported")} {importResult.imported}
              </span>
              {importResult.skipped > 0 && (
                <span style={{ color: "#b27c34" }}>
                  {t("page.import_skills_dialog.skipped")} {importResult.skipped}
                </span>
              )}
              {importResult.failed > 0 && (
                <span style={{ color: "#d65f59" }}>
                  {t("page.import_skills_dialog.failed")} {importResult.failed}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: 10,
            padding: "16px 24px",
            borderTop: "1px solid #f5f5f4",
          }}
        >
          <Button variant="outline" onClick={onClose}>
            {importResult ? t("page.flows.close") : t("action.cancel")}
          </Button>
          {parsedSkills.length > 0 && !importResult && (
            <Button
              variant="primary"
              disabled={selectedCount === 0 || importing}
              onClick={doImport}
            >
              {importing
                ? t("page.import_skills_dialog.importing")
                : t("page.import_skills_dialog.import_selected_count").replace(
                    "{count}",
                    String(selectedCount),
                  )}
            </Button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
