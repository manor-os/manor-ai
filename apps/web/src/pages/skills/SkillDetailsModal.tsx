/**
 * SkillDetailsModal — claude.ai-style skill viewer.
 *
 * Large dialog with a left file tree (SKILL.md, references/, scripts/, …)
 * and a right content pane that renders the selected file: markdown for
 * .md, syntax highlighting for code. Contents come from
 * GET /skills/{id}/files; marketplace catalog entries (no DB row) and
 * fetch failures fall back to a map built from the skill object itself so
 * the viewer always shows something.
 */
import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../../components/ui/Button";
import LoadingSpinner from "../../components/ui/LoadingSpinner";
import { IconCheck, IconClose, IconPlus } from "../../components/icons";
import { formatCategory, getSkillDescription } from "./skillTypes";
import { SkillFileTree, type SkillTreeFile } from "./SkillFileTree";
import { SkillFileContent } from "./SkillFileContent";

const OVERVIEW_KEY = "__overview__";

function skillExamples(skill: any): any[] {
  const examples = skill?.examples ?? skill?.config?.examples ?? [];
  return Array.isArray(examples) ? examples : [];
}

function skillExampleScenarios(skill: any): string[] {
  const scenarios =
    skill?.example_scenarios ?? skill?.config?.example_scenarios ?? [];
  return Array.isArray(scenarios) ? scenarios : [];
}

function skillUsageSummary(skill: any): string {
  return skill?.usage_summary ?? skill?.config?.usage_summary ?? "";
}

function skillExtraPaths(skill: any): string[] {
  const paths =
    skill?.extra_file_paths ?? skill?.config?.extra_file_paths ?? [];
  return Array.isArray(paths) ? paths : [];
}

/** Files reconstructable from the skill object alone (no /files call). */
function buildFallbackFiles(skill: any): Record<string, string> {
  const map: Record<string, string> = {};
  const prompt = skill?.system_prompt || skill?.prompt || "";
  if (prompt) map["SKILL.md"] = prompt;
  const scripts = skill?.scripts ?? skill?.config?.scripts;
  if (scripts && typeof scripts === "object" && !Array.isArray(scripts)) {
    for (const [name, content] of Object.entries(scripts)) {
      if (typeof content === "string") {
        map[name.startsWith("scripts/") ? name : `scripts/${name}`] = content;
      }
    }
  }
  const extras = skill?.config?.extra_files;
  if (extras && typeof extras === "object" && !Array.isArray(extras)) {
    for (const [rel, content] of Object.entries(extras)) {
      if (typeof content === "string" && !(rel in map)) map[rel] = content;
    }
  }
  for (const example of skillExamples(skill)) {
    const rel = String(example?.path || example?.title || "").trim();
    const content = example?.content;
    if (rel && typeof content === "string") {
      map[rel.startsWith("examples/") ? rel : `examples/${rel}`] = content;
    }
  }
  const requirements = skill?.requirements ?? skill?.config?.requirements;
  if (typeof requirements === "string" && requirements.trim()) {
    map["requirements.txt"] = requirements;
  }
  return map;
}

export function SkillDetailsModal({
  skill,
  onClose,
  onImport,
  importing,
  subscribed,
}: {
  skill: any | null;
  onClose: () => void;
  onImport?: () => void;
  importing?: boolean;
  subscribed?: boolean;
}) {
  const [selectedBySkill, setSelectedBySkill] = useState<
    Record<string, string>
  >({});

  const skillId: string | null = skill?.id ?? null;
  const filesQuery = useQuery({
    queryKey: ["skill-files", skillId],
    queryFn: () => api.skills.getFiles(skillId!),
    enabled: !!skillId,
    staleTime: 60_000,
    retry: false,
  });

  const usage = skillUsageSummary(skill);
  const scenarios = skillExampleScenarios(skill);
  const hasOverview = !!usage || scenarios.length > 0;

  const { files, skipped } = useMemo(() => {
    // Examples may exist only in config (marketplace imports) — surface them
    // in the tree regardless of where the rest of the bundle came from.
    const mergeExamples = (map: Record<string, string>) => {
      for (const example of skillExamples(skill)) {
        const rel = String(example?.path || example?.title || "").trim();
        const content = example?.content;
        if (!rel || typeof content !== "string") continue;
        const key = rel.startsWith("examples/") ? rel : `examples/${rel}`;
        if (!(key in map)) map[key] = content;
      }
      return map;
    };
    const remote = filesQuery.data;
    if (remote?.files && Object.keys(remote.files).length > 0) {
      const hasRealPrompt = (remote.files["SKILL.md"] || "").trim().length > 0;
      const map = { ...remote.files };
      if (!hasRealPrompt) delete map["SKILL.md"];
      return { files: mergeExamples(map), skipped: remote.skipped_files || [] };
    }
    const fallback = buildFallbackFiles(skill);
    // Paths known from metadata but whose content we don't have — list them
    // greyed out so the bundle's shape is still visible.
    const skippedFromPaths = skillExtraPaths(skill)
      .filter((path) => !(path in fallback))
      .map((path) => ({ path, reason: "unavailable" }));
    return { files: fallback, skipped: skippedFromPaths };
  }, [filesQuery.data, skill]);

  const treeFiles = useMemo<SkillTreeFile[]>(() => {
    const previewable = Object.keys(files).map((path) => ({
      path,
      previewable: true,
    }));
    const unavailable = skipped.map((entry: any) => ({
      path: entry.path,
      previewable: false,
      size: entry.size,
      reason: entry.reason,
    }));
    return [...previewable, ...unavailable];
  }, [files, skipped]);

  const skippedByPath = useMemo(() => {
    const map: Record<string, string> = {};
    for (const entry of skipped as any[]) map[entry.path] = entry.reason;
    return map;
  }, [skipped]);

  if (!skill) return null;

  const title =
    skill.display_name || skill.name || skill.skill_name || t("page.skills.skill");
  const description = getSkillDescription(skill);
  const category = formatCategory(skill.category);
  const tags: string[] = Array.isArray(skill.tags) ? skill.tags : [];
  const selectionKey = skillId || skill.marketplace_id || title;

  const defaultSelected = files["SKILL.md"]
    ? "SKILL.md"
    : hasOverview
      ? OVERVIEW_KEY
      : Object.keys(files)[0] || (treeFiles[0]?.path ?? null);
  const selected = selectedBySkill[selectionKey] ?? defaultSelected;
  const setSelected = (path: string) =>
    setSelectedBySkill((prev) => ({ ...prev, [selectionKey]: path }));

  const loading = !!skillId && filesQuery.isLoading;

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
        padding: 20,
      }}
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="manor-dialog skill-details-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(1200px, calc(100vw - 40px))",
          height: "min(86vh, 900px)",
          overflow: "hidden",
          background: "var(--modal-bg)",
          backdropFilter: "blur(20px) saturate(1.08)",
          WebkitBackdropFilter: "blur(20px) saturate(1.08)",
          borderRadius: 18,
          boxShadow: "var(--modal-shadow)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          style={{
            padding: "20px 22px 16px",
            display: "flex",
            justifyContent: "space-between",
            gap: 16,
          }}
        >
          <div style={{ minWidth: 0 }}>
            <p
              className="text-xs font-semibold text-stone-500 uppercase tracking-wide"
              style={{ margin: "0 0 4px" }}
            >
              {t("page.skills.skill_details")}
            </p>
            <h2
              style={{
                margin: 0,
                fontSize: 20,
                fontWeight: 700,
                letterSpacing: "-0.02em",
                lineHeight: 1.3,
                color: "var(--text-strong)",
                wordBreak: "break-word",
              }}
            >
              {title}
            </h2>
            {description && (
              <p
                style={{
                  margin: "6px 0 0",
                  color: "var(--text-muted)",
                  lineHeight: 1.55,
                  fontSize: 13,
                  display: "-webkit-box",
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: "vertical",
                  overflow: "hidden",
                }}
              >
                {description}
              </p>
            )}
            {(skill.version || category || tags.length > 0) && (
              <p
                style={{
                  margin: "7px 0 0",
                  fontSize: 12,
                  color: "var(--text-muted)",
                  display: "flex",
                  flexWrap: "wrap",
                  columnGap: 8,
                  rowGap: 2,
                }}
              >
                {[
                  skill.version ? `v${skill.version}` : null,
                  category || null,
                  ...tags.slice(0, 6),
                ]
                  .filter(Boolean)
                  .map((item, index) => (
                    <span key={`${item}-${index}`} style={{ display: "inline-flex", columnGap: 8 }}>
                      {index > 0 && <span aria-hidden>·</span>}
                      <span className={index === 0 && skill.version ? "mono" : undefined}>
                        {item}
                      </span>
                    </span>
                  ))}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            title={t("page.flows.close")}
            style={{
              width: 34,
              height: 34,
              borderRadius: "50%",
              border: "none",
              background: "var(--modal-muted-bg)",
              color: "var(--text-muted)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              flexShrink: 0,
            }}
          >
            <IconClose size={16} />
          </button>
        </div>

        <div style={{ flex: 1, minHeight: 0, display: "flex", gap: 4, padding: "0 12px" }}>
          <aside
            style={{
              width: 250,
              flexShrink: 0,
              overflowY: "auto",
              padding: "12px 10px",
              borderRadius: 14,
              background: "var(--modal-muted-bg)",
            }}
          >
            {hasOverview && (
              <button
                type="button"
                onClick={() => setSelected(OVERVIEW_KEY)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 7,
                  width: "100%",
                  textAlign: "left",
                  padding: "5px 8px",
                  marginBottom: 6,
                  border: "none",
                  borderRadius: 8,
                  cursor: "pointer",
                  background:
                    selected === OVERVIEW_KEY
                      ? "var(--modal-sunken-bg)"
                      : "transparent",
                  boxShadow:
                    selected === OVERVIEW_KEY
                      ? "0 1px 2px rgba(28,25,23,0.06)"
                      : "none",
                  color:
                    selected === OVERVIEW_KEY
                      ? "var(--text-strong)"
                      : "var(--text-muted)",
                  fontSize: 12,
                  fontWeight: selected === OVERVIEW_KEY ? 600 : 400,
                  fontFamily:
                    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                }}
              >
                <span
                  aria-hidden
                  style={{
                    flexShrink: 0,
                    width: 18,
                    fontSize: 10,
                    color: "var(--text-muted)",
                    fontFamily:
                      "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
                  }}
                >
                  ☰
                </span>
                {t("page.skills.viewer_overview")}
              </button>
            )}
            {loading ? (
              <div style={{ display: "flex", justifyContent: "center", padding: 20 }}>
                <LoadingSpinner size={18} />
              </div>
            ) : (
              <SkillFileTree
                files={treeFiles}
                selected={selected === OVERVIEW_KEY ? null : selected}
                onSelect={setSelected}
              />
            )}
          </aside>

          <div style={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
            {!!skillId &&
              filesQuery.isError &&
              Object.keys(files).length === 0 &&
              !hasOverview && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  margin: "12px 16px 0",
                  padding: "8px 12px",
                  borderRadius: 10,
                  background: "var(--modal-muted-bg)",
                  color: "var(--text-muted)",
                  fontSize: 12,
                }}
              >
                <span style={{ flex: 1 }}>
                  {t("page.skills.viewer_load_failed")}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => filesQuery.refetch()}
                >
                  {t("page.skills.viewer_retry")}
                </Button>
              </div>
            )}
            {loading ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  height: "100%",
                }}
              >
                <LoadingSpinner size={22} />
              </div>
            ) : selected === OVERVIEW_KEY ? (
              <div style={{ padding: "14px 22px 22px" }}>
                {usage && (
                  <section style={{ marginBottom: 16 }}>
                    <h3
                      style={{
                        margin: "0 0 6px",
                        fontSize: 13,
                        color: "var(--text-strong)",
                        fontWeight: 600,
                        letterSpacing: "-0.014em",
                      }}
                    >
                      {t("page.skills.how_to_use")}
                    </h3>
                    <p
                      style={{
                        margin: 0,
                        color: "var(--text-default)",
                        lineHeight: 1.6,
                        fontSize: 13,
                        letterSpacing: "0.005em",
                      }}
                    >
                      {usage}
                    </p>
                  </section>
                )}
                {scenarios.length > 0 && (
                  <section>
                    <h3
                      style={{
                        margin: "0 0 6px",
                        fontSize: 13,
                        color: "var(--text-strong)",
                        fontWeight: 600,
                        letterSpacing: "-0.014em",
                      }}
                    >
                      {t("page.skills.example_scenarios")}
                    </h3>
                    <ul
                      style={{
                        margin: 0,
                        paddingLeft: 18,
                        listStyle: "disc",
                        color: "var(--text-default)",
                        fontSize: 13,
                        letterSpacing: "0.005em",
                      }}
                    >
                      {scenarios.map((scenario) => (
                        <li key={scenario} style={{ margin: "3px 0", lineHeight: 1.55 }}>
                          {scenario}
                        </li>
                      ))}
                    </ul>
                  </section>
                )}
              </div>
            ) : (
              <SkillFileContent
                path={selected}
                content={selected ? files[selected] : undefined}
                skippedReason={selected ? skippedByPath[selected] : undefined}
              />
            )}
          </div>
        </div>

        <div
          style={{
            padding: "14px 20px",
            display: "flex",
            justifyContent: "flex-end",
            gap: 10,
          }}
        >
          <Button variant="outline" size="sm" onClick={onClose}>
            {t("page.flows.close")}
          </Button>
          {onImport && (subscribed ? (
            <Button
              variant="outline"
              size="sm"
              disabled
              style={{
                color: "#437f6b",
                background: "var(--modal-muted-bg)",
              }}
            >
              <IconCheck size={12} />
              {t("page.skills.subscribed")}
            </Button>
          ) : (
            <Button
              variant="primary"
              size="sm"
              onClick={onImport}
              loading={importing}
            >
              <IconPlus size={12} />
              {t("page.skills.subscribe")}
            </Button>
          ))}
        </div>
      </div>
    </div>,
    document.body,
  );
}
