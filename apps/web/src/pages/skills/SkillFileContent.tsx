/**
 * SkillFileContent — renders one bundle file in the skill viewer.
 *
 * Markdown files render through ChatMarkdown (same pipeline as chat);
 * code files get Prism highlighting via the languages ChatMarkdown already
 * registers; very large files fall back to a plain <pre> because Prism
 * janks past ~150KB; binary/oversized entries show a placeholder.
 */
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { CSSProperties } from "react";
import ChatMarkdown from "../../components/ChatMarkdown";
import { t } from "../../lib/i18n";

const HIGHLIGHT_CHAR_LIMIT = 150_000;

const EXT_LANGUAGE: Record<string, string> = {
  py: "python",
  sh: "bash",
  bash: "bash",
  js: "javascript",
  jsx: "jsx",
  ts: "typescript",
  tsx: "tsx",
  json: "json",
  css: "css",
  sql: "sql",
  yaml: "yaml",
  yml: "yaml",
  java: "java",
  go: "go",
  dockerfile: "docker",
};

const codeTheme: Record<string, CSSProperties> = {
  ...oneLight,
  'pre[class*="language-"]': {
    ...(oneLight['pre[class*="language-"]'] as CSSProperties),
    margin: 0,
    padding: "14px 16px",
    background: "transparent",
    fontSize: 12,
    lineHeight: 1.65,
    textShadow: "none",
  },
  'code[class*="language-"]': {
    ...(oneLight['code[class*="language-"]'] as CSSProperties),
    fontSize: 12,
    lineHeight: 1.65,
    textShadow: "none",
  },
};

function languageFor(path: string): string {
  const name = (path.split("/").pop() || "").toLowerCase();
  if (name === "dockerfile") return "docker";
  const ext = name.includes(".") ? name.split(".").pop()! : "";
  return EXT_LANGUAGE[ext] || "text";
}

function PlaceholderPanel({ message }: { message: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100%",
        minHeight: 180,
        color: "var(--text-muted)",
        fontSize: 13,
        padding: 24,
        textAlign: "center",
      }}
    >
      {message}
    </div>
  );
}

export function SkillFileContent({
  path,
  content,
  skippedReason,
}: {
  path: string | null;
  content: string | undefined;
  skippedReason?: string;
}) {
  if (!path) {
    return <PlaceholderPanel message={t("page.skills.viewer_select_file")} />;
  }
  if (skippedReason) {
    return (
      <PlaceholderPanel
        message={
          skippedReason === "binary"
            ? t("page.skills.viewer_not_previewable")
            : t("page.skills.viewer_too_large")
        }
      />
    );
  }
  if (content == null) {
    return <PlaceholderPanel message={t("page.skills.viewer_not_previewable")} />;
  }

  if (path.toLowerCase().endsWith(".md")) {
    return (
      <div className="skill-doc" style={{ padding: "16px 26px 26px" }}>
        <ChatMarkdown content={content} enableFileCards={false} />
      </div>
    );
  }

  if (content.length > HIGHLIGHT_CHAR_LIMIT) {
    return (
      <div>
        <p
          style={{
            margin: 0,
            padding: "10px 16px 0",
            fontSize: 12,
            color: "var(--text-muted)",
          }}
        >
          {t("page.skills.viewer_too_large")}
        </p>
        <pre
          style={{
            margin: 0,
            padding: "10px 16px 16px",
            fontSize: 12,
            lineHeight: 1.65,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            color: "var(--text-default)",
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          }}
        >
          {content}
        </pre>
      </div>
    );
  }

  return (
    <SyntaxHighlighter
      style={codeTheme}
      language={languageFor(path)}
      PreTag="div"
      customStyle={{ background: "transparent", margin: 0 }}
    >
      {content}
    </SyntaxHighlighter>
  );
}
