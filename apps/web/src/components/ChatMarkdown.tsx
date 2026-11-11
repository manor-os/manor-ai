/**
 * ChatMarkdown — renders markdown content in chat bubbles.
 *
 * Supports GFM (tables, strikethrough, task lists), code highlighting,
 * and inline code. Designed for both bot and user messages.
 *
 * Uses react-markdown v10 (named export: Markdown).
 */
import { createContext, memo, useContext, useEffect, useState, type CSSProperties, type ReactNode } from "react";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
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
import { api, parseTaskAttachmentUrl, resolveDisplayMediaUrl } from "../lib/api";
import { t } from "../lib/i18n";
import InlineFileReferenceCard from "./InlineFileReferenceCard";
import ChatFilePreview, { FoldedPreview, ResultFilePreview, previewName } from "./ChatFilePreview";
import InlineTaskReferenceCard from "./InlineTaskReferenceCard";
import TaskAttachmentLink from "./task/TaskAttachmentLink";
import InlineIntegrationLink from "./InlineIntegrationLink";
import SourceCitation from "./SourceCitation";
import remarkSourceCitations from "../lib/remarkSourceCitations.mjs";
import { parseIntegrationSetupLink } from "../lib/integrationSetupLinks";
import {
  decodeRouteReferenceHref,
  linkifyChatRouteReferencesInMarkdown,
  looksLikeTaskRouteReference,
  looksLikeViewerRouteReference,
} from "../lib/chatRouteReferences";
import { visibleAssistantText } from "../lib/assistantTextProjection";
import { redactInternalAssistantErrorDetails, stripInternalReferences } from "../lib/assistant-visible-text.mjs";
import { decodeFileReferenceHref, isInternalFileTarget, isOpenableFileReference, linkifyFileReferencesInMarkdown } from "../lib/fileReferences";
import { inPlaceResultFileForLink, resultFilePreviewTarget } from "../lib/resultFiles";


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
  /** Literal file content and Task descriptions keep identifiers rather than treating them as chat prose. */
  preserveReferences?: boolean;
  /** A reply's linked results (tables, documents, pictures) show as themselves under their sentence. */
  showFilesInPlace?: boolean;
  returnTo?: string;
}

interface MarkdownRenderOptions {
  isUser?: boolean;
  returnTo?: string;
  enableFileCards: boolean;
  showFilesInPlace?: boolean;
  deferSyntaxHighlighting: boolean;
}

const MarkdownRenderContext = createContext<MarkdownRenderOptions>({
  enableFileCards: true,
  deferSyntaxHighlighting: false,
});

/** Links whose file is shown in place right below; in the sentence they read as the plain name. */
const InPlaceFileLinks = createContext<ReadonlySet<string>>(new Set());

type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

function hastText(node: HastNode): string {
  if (node.type === "text") return node.value || "";
  return (node.children || []).map(hastText).join("");
}

/**
 * The results linked in one paragraph or list item (not in a nested list),
 * and whether the line holds nothing but those links.
 */
type InPlaceLine = {
  files: NonNullable<ReturnType<typeof inPlaceResultFileForLink>>[];
  hrefs: Set<string>;
  linksOnly: boolean;
};

function inPlaceFilesInLine(node: HastNode | undefined): InPlaceLine {
  const files: InPlaceLine["files"] = [];
  const hrefs = new Set<string>();
  if (!node || typeof window === "undefined") return { files, hrefs, linksOnly: false };
  let rest = "";
  const visit = (child: HastNode) => {
    if (child.type === "text") { rest += child.value || ""; return; }
    if (child.type !== "element" || ["p", "ul", "ol", "pre", "table"].includes(child.tagName || "")) return;
    if (child.tagName === "img") {
      const src = String(child.properties?.src || "");
      const file = inPlaceResultFileForLink(src, String(child.properties?.alt || ""), window.location.origin, { image: true });
      if (file) {
        if (!files.some((existing) => existing.reference === file.reference)) files.push(file);
        hrefs.add(src);
      }
      return;
    }
    if (child.tagName === "a") {
      const href = String(child.properties?.href || "");
      const label = hastText(child);
      const file = inPlaceResultFileForLink(href, label, window.location.origin);
      if (file) {
        if (!files.some((existing) => existing.reference === file.reference)) files.push(file);
        hrefs.add(href);
        return;
      }
      rest += label;
      return;
    }
    (child.children || []).forEach(visit);
  };
  (node.children || []).forEach(visit);
  return { files, hrefs, linksOnly: files.length > 0 && !rest.replace(/[\s:：,，、;；.。·•\-–—|()（）]+/g, "") };
}

/**
 * One pass over the whole reply, in reading order: each paragraph or list item
 * that names results gets the files it shows, and a file already shown above
 * (the same file named twice, or as a link and a picture) is not shown again.
 */
/** A file's identity in this reply: its document id, else its address. */
function fileKeys(file: InPlaceLine["files"][number]): string[] {
  const target = typeof window === "undefined" ? null : resultFilePreviewTarget(file.reference, window.location.origin);
  return [target && "documentId" in target ? `document:${target.documentId}` : `ref:${file.reference}`];
}

function rehypeInPlaceFiles() {
  return (tree: HastNode) => {
    const shown = new Set<string>();
    const visit = (node: HastNode) => {
      if (node.type === "element" && (node.tagName === "p" || node.tagName === "li")) {
        const line = inPlaceFilesInLine(node);
        if (line.hrefs.size) {
          line.files = line.files.filter((file) => !fileKeys(file).some((key) => shown.has(key)));
          line.files.forEach((file) => fileKeys(file).forEach((key) => shown.add(key)));
          (node as HastNode & { data?: { inPlace?: InPlaceLine } }).data = { ...(node as { data?: object }).data, inPlace: line };
        }
      }
      (node.children || []).forEach(visit);
    };
    visit(tree);
  };
}
const inPlaceRehypePlugins = [rehypeInPlaceFiles];

function inPlaceLineOf(node: unknown): InPlaceLine | null {
  return (node as { data?: { inPlace?: InPlaceLine } } | undefined)?.data?.inPlace || null;
}

function InPlaceFiles({ files, returnTo }: { files: InPlaceLine["files"]; returnTo?: string }) {
  return <div className="chat-md-files">
    {files.map((file) => <ResultFilePreview key={file.key} file={file} returnTo={returnTo} />)}
  </div>;
}

const assistantRemarkPlugins = [remarkGfm, remarkSourceCitations, remarkBreaks];
const userRemarkPlugins = [remarkGfm, remarkBreaks];

function transformChatUrl(url: string): string {
  if (url.startsWith("manor-route:") || url.startsWith("manor-file:")) return url;
  return defaultUrlTransform(url);
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

/**
 * Where an image's bytes are. ``/viewer/<id>`` is the file page, not an image:
 * a Knowledge file is read through its preview, a file in this Manor's storage
 * through the storage address. Anything else is an ordinary image URL.
 */
async function markdownImageBytes(src: string): Promise<{ url: string; revoke: () => void }> {
  const reference = decodeFileReferenceHref(src) || src;
  const target = typeof window === "undefined" ? null : resultFilePreviewTarget(reference, window.location.origin);
  if (target && "documentId" in target) {
    const url = await api.documents.preview(target.documentId, { cache: true });
    return { url, revoke: () => URL.revokeObjectURL(url) };
  }
  if (target) return resolveDisplayMediaUrl(target.url);
  // A file page that names no previewable file has no picture to show.
  if (looksLikeViewerRouteReference(reference)) throw new Error("Not an image address");
  return resolveDisplayMediaUrl(src);
}

function ProtectedMarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const [display, setDisplay] = useState<{ src: string; url: string | null; failed: boolean } | null>(null);
  const current = display?.src === src ? display : null;

  useEffect(() => {
    if (!src) return;
    let cancelled = false;
    let revoke = () => {};
    markdownImageBytes(src)
      .then((resolved) => {
        revoke = resolved.revoke;
        if (!cancelled) setDisplay({ src, url: resolved.url, failed: false });
        else resolved.revoke();
      })
      .catch(() => {
        if (!cancelled) setDisplay({ src, url: null, failed: true });
      });
    return () => { cancelled = true; revoke(); };
  }, [src]);

  // Never a broken picture: an image that cannot load reads as its name.
  if (current?.failed) return alt ? <span className="chat-md-image-missing">{alt}</span> : null;
  if (!current?.url) return null;
  return <img src={current.url} alt={alt || ""} style={{ maxWidth: "100%", borderRadius: 10 }}
    onError={() => src && setDisplay({ src, url: null, failed: true })} />;
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

export function hasUnclosedFencedCodeBlock(text: string): boolean {
  let fence: { character: string; length: number } | null = null;
  for (const line of text.split(/\r?\n/)) {
    if (!fence) {
      const match = /^(?: {0,3})(`{3,}|~{3,})(.*)$/.exec(line);
      if (!match || (match[1][0] === "`" && match[2].includes("`"))) continue;
      const marker = match[1];
      fence = { character: marker[0], length: marker.length };
    } else if (new RegExp(`^(?: {0,3})${fence.character}{${fence.length},}[ \\t]*$`).test(line)) {
      fence = null;
    }
  }
  return fence !== null;
}

const markdownComponents: Components = {
          span({ node, children, ...props }) {
            if (node?.properties?.["data-source-citation"]) {
              return <SourceCitation
                label={String(node.properties["data-source-label"] || "Source")}
                kind={node.properties["data-source-kind"] === "file" ? "file" : "web"}
                iconUrl={String(node.properties["data-source-icon"] || "")}
                count={Number(node.properties["data-source-count"] || 1)}
              >{children}</SourceCitation>;
            }
            return <span {...props}>{children}</span>;
          },
          /* Override pre to handle fenced code blocks */
          pre({ children }) {
            const { deferSyntaxHighlighting, isUser } = useContext(MarkdownRenderContext);
            // children is a <code> element with className="language-xxx"
            const codeEl = children as any;
            if (!codeEl?.props) {
              return <pre>{children}</pre>;
            }
            const className = codeEl.props.className || "";
            const match = /language-(\w+)/.exec(className);
            const codeString = getTextContent(codeEl.props.children).replace(/\n$/, "");
            const lang = match?.[1] || "";
            const normalizedLang = lang.toLowerCase();
            const isLongTechnicalJson =
              codeString.length > 800 &&
              (normalizedLang === "json" ||
                (!normalizedLang && /^[\s]*[\[{]/.test(codeString)));

            const renderedCode = (
              <div className={`chat-code-block${isUser ? " chat-code-block--user" : ""}`}>
                {lang && (
                  <div className="chat-code-block-label">
                    {lang}
                  </div>
                )}
                <CopyButton text={codeString} />
                {deferSyntaxHighlighting ? (
                  <pre style={{
                    margin: 0,
                    padding: "12px 14px",
                    background: "var(--chat-code-bg)",
                    borderRadius: 0,
                    color: "var(--chat-code-fg)",
                    fontSize: 12,
                    lineHeight: 1.6,
                    whiteSpace: "pre-wrap",
                  }}><code>{codeString}</code></pre>
                ) : (
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
                )}
              </div>
            );

            if (isLongTechnicalJson) {
              return (
                <details className="chat-technical-details">
                  <summary>
                    <span>{t("component.chat_markdown.technical_details")}</span>
                    <span className="chat-technical-details-meta">JSON</span>
                  </summary>
                  {renderedCode}
                </details>
              );
            }

            return renderedCode;
          },

          /* Inline code only */
          code({ className, children }) {
            const { isUser } = useContext(MarkdownRenderContext);
            return (
              <code
                className={`${className || ""} chat-inline-code${isUser ? " chat-inline-code--user" : ""}`.trim()}
              >
                {children}
              </code>
            );
          },

          p({ node, children }) {
            const { returnTo } = useContext(MarkdownRenderContext);
            const inPlace = inPlaceLineOf(node);
            if (!inPlace) return <p>{children}</p>;
            // Nothing but the file (shown here or already above): no empty line is left behind.
            if (inPlace.linksOnly) return inPlace.files.length ? <InPlaceFiles files={inPlace.files} returnTo={returnTo} /> : null;
            return <>
              <InPlaceFileLinks.Provider value={inPlace.hrefs}><p>{children}</p></InPlaceFileLinks.Provider>
              {inPlace.files.length > 0 && <InPlaceFiles files={inPlace.files} returnTo={returnTo} />}
            </>;
          },

          li({ node, children, className }) {
            const { returnTo } = useContext(MarkdownRenderContext);
            const inPlace = inPlaceLineOf(node);
            if (!inPlace) return <li className={className}>{children}</li>;
            if (inPlace.linksOnly && !inPlace.files.length) return null;
            if (inPlace.linksOnly) {
              return <li className={`${className || ""} chat-md-file-item`.trim()}>
                <InPlaceFiles files={inPlace.files} returnTo={returnTo} />
              </li>;
            }
            return <li className={className}>
              <InPlaceFileLinks.Provider value={inPlace.hrefs}>{children}</InPlaceFileLinks.Provider>
              {inPlace.files.length > 0 && <InPlaceFiles files={inPlace.files} returnTo={returnTo} />}
            </li>;
          },

          a({ href, children }) {
            const { enableFileCards, isUser, returnTo } = useContext(MarkdownRenderContext);
            const inPlaceLinks = useContext(InPlaceFileLinks);
            const label = getTextContent(children).trim();
            if (href && inPlaceLinks.has(href)) return <span className="chat-md-file-name">{children}</span>;
            // An unsafe scheme (emptied by urlTransform) or a sandbox-internal
            // file location is not a destination: keep the words, drop the link.
            if (!href?.trim() || isInternalFileTarget(href)) return <>{children}</>;
            if (parseTaskAttachmentUrl(href)) {
              return <TaskAttachmentLink href={href} style={{ color: isUser ? "#c4dfd2" : "#4f7d75", textDecoration: "underline", textUnderlineOffset: 2 }}>{children}</TaskAttachmentLink>;
            }
            const integrationProvider = href && typeof window !== "undefined"
              ? parseIntegrationSetupLink(href, window.location.origin)
              : null;
            if (integrationProvider) {
              return <InlineIntegrationLink provider={integrationProvider} label={label} />;
            }
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
            // A file card is only rendered when the Markdown target carries a
            // destination. Knowledge references use /viewer/<document_id>;
            // a filename-shaped label alone is never treated as clickable.
            if (
              enableFileCards &&
              (
                fileReference ||
                looksLikeViewerRouteReference(targetReference) ||
                isOpenableFileReference(targetReference)
              )
            ) {
              const chip = (
                <InlineFileReferenceCard
                  reference={targetReference}
                  label={label || undefined}
                  returnTo={returnTo}
                  compact
                />
              );
              // A reply shows the image, video or document it links to, folded on request.
              if (isUser) return chip;
              return (
                <ChatFilePreview reference={targetReference} label={label || undefined} returnTo={returnTo}
                  fallback={chip} />
              );
            }
            const anchor = (
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
            return anchor;
          },

          img({ src, alt }) {
            const { isUser } = useContext(MarkdownRenderContext);
            const inPlaceLinks = useContext(InPlaceFileLinks);
            if (src && inPlaceLinks.has(src)) return null;
            const image = <ProtectedMarkdownImage src={src || ""} alt={alt || ""} />;
            // A reply's image folds away like the files it links to.
            if (isUser || !src) return image;
            return <FoldedPreview label={alt?.trim() || previewName(src)}>{image}</FoldedPreview>;
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
            const { isUser } = useContext(MarkdownRenderContext);
            return (
              <th className={isUser ? "chat-md-table-cell--user" : undefined}>
                {children}
              </th>
            );
          },

          td({ children }) {
            const { isUser } = useContext(MarkdownRenderContext);
            return (
              <td className={isUser ? "chat-md-table-cell--user" : undefined}>
                {children}
              </td>
            );
          },
};

function ChatMarkdown({ content, isUser, streaming, enableFileCards = true, preserveReferences = false, showFilesInPlace = false, returnTo }: ChatMarkdownProps) {
  const projectedText = visibleAssistantText(toMarkdownText(content));
  // What Manor says names things: no IDs or raw tool keys in its prose.
  const messageText = isUser
    ? projectedText
    : redactInternalAssistantErrorDetails(projectedText);
  const rawText = isUser || preserveReferences ? messageText : stripInternalReferences(messageText);
  const routeLinkedText = enableFileCards ? linkifyChatRouteReferencesInMarkdown(rawText) : rawText;
  const text = enableFileCards ? linkifyFileReferencesInMarkdown(routeLinkedText) : routeLinkedText;
  const deferSyntaxHighlighting = Boolean(streaming && hasUnclosedFencedCodeBlock(text));

  return (
    <div className={isUser ? "chat-md chat-md--user" : `chat-md${streaming ? " chat-md--streaming" : ""}`}>
      <MarkdownRenderContext.Provider value={{ isUser, returnTo, enableFileCards, showFilesInPlace: showFilesInPlace && !isUser, deferSyntaxHighlighting }}>
        <Markdown
          remarkPlugins={isUser ? userRemarkPlugins : assistantRemarkPlugins}
          rehypePlugins={enableFileCards && showFilesInPlace && !isUser ? inPlaceRehypePlugins : undefined}
          urlTransform={transformChatUrl}
          components={markdownComponents}
        >
          {text}
        </Markdown>
      </MarkdownRenderContext.Provider>
    </div>
  );
}

export default memo(ChatMarkdown);
