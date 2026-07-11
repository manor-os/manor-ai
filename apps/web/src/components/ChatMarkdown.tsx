/**
 * ChatMarkdown — renders markdown content in chat bubbles.
 *
 * Supports GFM (tables, strikethrough, task lists), code highlighting,
 * and inline code. Designed for both bot and user messages.
 *
 * Uses react-markdown v10 (named export: Markdown).
 */
import { memo, useEffect, useState, type CSSProperties, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import markdownLang from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import java from "react-syntax-highlighter/dist/esm/languages/prism/java";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import docker from "react-syntax-highlighter/dist/esm/languages/prism/docker";
import { resolveDisplayMediaUrl } from "../lib/api";
import { t } from "../lib/i18n";
import InlineFileReferenceCard from "./InlineFileReferenceCard";
import InlineTaskReferenceCard from "./InlineTaskReferenceCard";
import {
  decodeRouteReferenceHref,
  linkifyChatRouteReferencesInMarkdown,
  looksLikeTaskRouteReference,
  looksLikeViewerRouteReference,
} from "../lib/chatRouteReferences";
import { decodeFileReferenceHref, linkifyFileReferencesInMarkdown, looksLikeFileReference } from "../lib/fileReferences";


SyntaxHighlighter.registerLanguage("tsx", tsx);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("ts", typescript);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("js", javascript);
SyntaxHighlighter.registerLanguage("jsx", tsx);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("py", python);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("sh", bash);
SyntaxHighlighter.registerLanguage("shell", bash);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("yml", yaml);
SyntaxHighlighter.registerLanguage("markdown", markdownLang);
SyntaxHighlighter.registerLanguage("md", markdownLang);
SyntaxHighlighter.registerLanguage("java", java);
SyntaxHighlighter.registerLanguage("go", go);
SyntaxHighlighter.registerLanguage("docker", docker);
SyntaxHighlighter.registerLanguage("dockerfile", docker);

interface ChatMarkdownProps {
  content: unknown;
  isUser?: boolean;
  streaming?: boolean;
  enableFileCards?: boolean;
  returnTo?: string;
}

/* Custom theme tweaks on top of oneLight */
const codeTheme: Record<string, CSSProperties> = {
  ...oneLight,
  'pre[class*="language-"]': {
    ...(oneLight['pre[class*="language-"]'] as CSSProperties),
    margin: 0,
    padding: "12px 14px",
    background: "var(--chat-code-bg)",
    borderRadius: 0,
    color: "var(--chat-code-fg)",
    fontSize: 12,
    lineHeight: 1.6,
    textShadow: "none",
  },
  'code[class*="language-"]': {
    ...(oneLight['code[class*="language-"]'] as CSSProperties),
    color: "var(--chat-code-fg)",
    fontSize: 12,
    lineHeight: 1.6,
    textShadow: "none",
  },
};

const userCodeTheme: Record<string, CSSProperties> = {
  ...oneLight,
  'pre[class*="language-"]': {
    ...(oneLight['pre[class*="language-"]'] as CSSProperties),
    margin: 0,
    padding: "12px 14px",
    background: "var(--chat-code-bg)",
    borderRadius: 0,
    fontSize: 12,
    lineHeight: 1.6,
    color: "var(--chat-code-fg)",
    textShadow: "none",
  },
  'code[class*="language-"]': {
    ...(oneLight['code[class*="language-"]'] as CSSProperties),
    fontSize: 12,
    lineHeight: 1.6,
    color: "var(--chat-code-fg)",
    textShadow: "none",
  },
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className={`chat-code-copy-button${copied ? " chat-code-copy-button--copied" : ""}`}
      onClick={() => {
        navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      title={t("component.chat_markdown.copy_code")}
    >
      {copied ? (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
        </svg>
      ) : (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
        </svg>
      )}
    </button>
  );
}

/* Helper to extract text from ReactNode children */
function getTextContent(children: ReactNode): string {
  if (typeof children === "string") return children;
  if (typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(getTextContent).join("");
  if (children && typeof children === "object" && "props" in children) {
    return getTextContent((children as any).props?.children);
  }
  return "";
}

function ProtectedMarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!src) return;
    let cancelled = false;
    let revoke = () => {};
    resolveDisplayMediaUrl(src)
      .then((resolved) => {
        revoke = resolved.revoke;
        if (!cancelled) setDisplayUrl(resolved.url);
      })
      .catch(() => {
        if (!cancelled) setDisplayUrl(null);
      });
    return () => { cancelled = true; revoke(); };
  }, [src]);

  if (!displayUrl) return null;
  return <img src={displayUrl} alt={alt || ""} style={{ maxWidth: "100%", borderRadius: 10 }} />;
}

function toMarkdownText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    const serialized = JSON.stringify(value);
    return serialized === undefined ? String(value) : serialized;
  } catch {
    return String(value);
  }
}

const MANOR_FINAL_RESPONSE_OPEN_TAG = "<manor-final-response>";
const MANOR_FINAL_RESPONSE_TAG_RE = /<\/?manor-final-response>\s*/gi;

function stripManorFinalResponseMarker(value: string): string {
  const markerIndex = value.toLowerCase().lastIndexOf(MANOR_FINAL_RESPONSE_OPEN_TAG);
  const visible = markerIndex >= 0
    ? value.slice(markerIndex + MANOR_FINAL_RESPONSE_OPEN_TAG.length)
    : value;
  return visible.replace(MANOR_FINAL_RESPONSE_TAG_RE, "").trim();
}

function ChatMarkdown({ content, isUser, streaming, enableFileCards = true, returnTo }: ChatMarkdownProps) {
  const rawText = stripManorFinalResponseMarker(toMarkdownText(content));
  const routeLinkedText = enableFileCards ? linkifyChatRouteReferencesInMarkdown(rawText) : rawText;
  const text = enableFileCards ? linkifyFileReferencesInMarkdown(routeLinkedText) : routeLinkedText;

  return (
    <div className={isUser ? "chat-md chat-md--user" : `chat-md${streaming ? " chat-md--streaming" : ""}`}>
      <Markdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          /* Override pre to handle fenced code blocks */
          pre({ children }) {
            // children is a <code> element with className="language-xxx"
            const codeEl = children as any;
            if (!codeEl?.props) {
              return <pre>{children}</pre>;
            }
            const className = codeEl.props.className || "";
            const match = /language-(\w+)/.exec(className);
            const codeString = getTextContent(codeEl.props.children).replace(/\n$/, "");
            const lang = match?.[1] || "";

            return (
              <div className={`chat-code-block${isUser ? " chat-code-block--user" : ""}`}>
                {lang && (
                  <div className="chat-code-block-label">
                    {lang}
                  </div>
                )}
                <CopyButton text={codeString} />
                <SyntaxHighlighter
                  style={isUser ? userCodeTheme : codeTheme}
                  language={lang || "text"}
                  PreTag="div"
                  customStyle={{
                    background: "var(--chat-code-bg)",
                    borderRadius: 0,
                    color: "var(--chat-code-fg)",
                    marginTop: 0,
                  }}
                >
                  {codeString}
                </SyntaxHighlighter>
              </div>
            );
          },

          /* Inline code only */
          code({ className, children }) {
            return (
              <code
                className={`${className || ""} chat-inline-code${isUser ? " chat-inline-code--user" : ""}`.trim()}
              >
                {children}
              </code>
            );
          },

          a({ href, children }) {
            const label = getTextContent(children).trim();
            const routeReference = href ? decodeRouteReferenceHref(href) : null;
            const fileReference = href ? decodeFileReferenceHref(href) : null;
            const targetReference = routeReference || fileReference || href || label;
            if (enableFileCards && looksLikeTaskRouteReference(targetReference)) {
              return (
                <InlineTaskReferenceCard
                  reference={targetReference}
                  label={label || undefined}
                  returnTo={returnTo}
                  compact
                />
              );
            }
            if (
              enableFileCards &&
              (
                fileReference ||
                looksLikeViewerRouteReference(targetReference) ||
                looksLikeFileReference(targetReference) ||
                looksLikeFileReference(label)
              )
            ) {
              return (
                <InlineFileReferenceCard
                  reference={targetReference}
                  label={label || undefined}
                  returnTo={returnTo}
                  compact
                />
              );
            }
            return (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  color: isUser ? "#c4dfd2" : "#4f7d75",
                  textDecoration: "underline",
                  textDecorationStyle: "dotted" as const,
                  textUnderlineOffset: 2,
                }}
              >
                {children}
              </a>
            );
          },

          img({ src, alt }) {
            return <ProtectedMarkdownImage src={src || ""} alt={alt || ""} />;
          },

          table({ children }) {
            return (
              <div className="chat-md-table-scroll">
                <table className="chat-md-table">
                  {children}
                </table>
              </div>
            );
          },

          th({ children }) {
            return (
              <th className={isUser ? "chat-md-table-cell--user" : undefined}>
                {children}
              </th>
            );
          },

          td({ children }) {
            return (
              <td className={isUser ? "chat-md-table-cell--user" : undefined}>
                {children}
              </td>
            );
          },
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}

export default memo(ChatMarkdown);
