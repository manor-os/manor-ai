import { decodeFileReferenceHref, fileNameFromReference, isOpenableFileReference, looksLikeFileReference } from "./fileReferences";
import { decodeRouteReferenceHref } from "./chatRouteReferences";

const SOURCE_LABEL = /^(?:credits?|sources?|references?|来源|引用来源|参考来源|资料来源|参考资料|参考文献)\s*[:：]\s*/i;
const SOURCE_HEADING = /^(?:sources?|references?|来源|引用来源|参考来源|资料来源|参考资料|参考文献)\s*[:：]?\s*$/i;
const INLINE_LABEL = /^(?:sources?|来源|参考|\d+)$/i;

function textOf(node) {
  return node.value || (node.children || []).map(textOf).join("");
}

function hasReference(node) {
  if (node.type === "link" || node.type === "linkReference") return true;
  return looksLikeFileReference(textOf(node)) || (node.children || []).some(hasReference);
}

function linesOf(children) {
  const lines = [[]];
  for (const child of children) {
    if (child.type === "break") {
      lines.push([]);
    } else if (child.type === "text") {
      const parts = child.value.split("\n");
      parts.forEach((value, index) => {
        if (index) lines.push([]);
        if (value) lines.at(-1).push({ ...child, value });
      });
    } else {
      lines.at(-1).push(child);
    }
  }
  return lines;
}

function joinLines(lines) {
  return lines.flatMap((line, index) => index ? [{ type: "break" }, ...line] : line);
}

/** Collapse only attributed evidence, never code, ordinary links or user text.
 * Keep the original Markdown nodes inside the disclosure: canonical file
 * destinations, labels, reference definitions and attribution remain intact.
 */
export default function remarkSourceCitations() {
  return (tree) => {
    const definitions = new Map();
    for (const node of tree.children) {
      // CommonMark resolves duplicate definitions to their first destination.
      if (node.type === "definition" && !definitions.has(node.identifier)) {
        definitions.set(node.identifier, node);
      }
    }
    const linkUrl = (node) => node.url || definitions.get(node.identifier)?.url || "";
    const citation = (children) => {
      const references = new Map();
      const collect = (node) => {
        if (["link", "linkReference"].includes(node.type)) {
          const rawUrl = linkUrl(node);
          const target = decodeRouteReferenceHref(rawUrl) || decodeFileReferenceHref(rawUrl) || rawUrl;
          const label = textOf(node).trim();
          if (isOpenableFileReference(target)) {
            references.set(target, { label: INLINE_LABEL.test(label) ? fileNameFromReference(target) : label, kind: "file" });
          } else if (/^https?:\/\//i.test(target)) {
            try {
              const url = new URL(target);
              references.set(target, {
                label: !label || INLINE_LABEL.test(label) || /^https?:\/\//i.test(label)
                  ? url.hostname.replace(/^www\./, "") : label,
                kind: "web",
                // Load only the site's public icon, never a third-party favicon service.
                icon: url.protocol === "https:" ? `${url.origin}/favicon.ico` : "",
              });
            } catch { /* Malformed links keep their original disclosure content. */ }
          }
          return;
        }
        if (node.type === "inlineCode" && looksLikeFileReference(node.value)) {
          references.set(node.value, { label: fileNameFromReference(node.value), kind: "file" });
        }
        (node.children || []).forEach(collect);
      };
      children.forEach(collect);
      const first = references.values().next().value;
      return {
        type: "sourceCitation",
        data: { hName: "span", hProperties: {
          "data-source-citation": true,
          "data-source-label": first?.label || "Source",
          "data-source-kind": first?.kind || "web",
          "data-source-icon": first?.icon || "",
          "data-source-count": references.size,
        } },
        children,
      };
    };
    const isSourceLink = (node) => {
      const url = linkUrl(node);
      const target = decodeRouteReferenceHref(url) || decodeFileReferenceHref(url) || url;
      return /^https?:\/\//i.test(target) || isOpenableFileReference(target);
    };
    const projectBlocks = (nodes) => {
      const result = [];
      for (let index = 0; index < nodes.length; index++) {
        const node = nodes[index];
        const next = nodes[index + 1];
        if (
          ["heading", "paragraph"].includes(node.type)
          && SOURCE_HEADING.test(textOf(node).trim())
          && next?.type === "list"
          && next.children.length > 0
          && next.children.every(hasReference)
        ) {
          result.push(citation([next]));
          index++;
          continue;
        }
        if (node.type !== "paragraph") {
          result.push(["list", "listItem"].includes(node.type)
            ? { ...node, children: projectBlocks(node.children) } : node);
          continue;
        }
        const lines = linesOf(node.children);
        const projected = [];
        for (let lineIndex = 0; lineIndex < lines.length; lineIndex++) {
          const line = lines[lineIndex];
          const label = line.map(textOf).join("").trim();
          if (SOURCE_LABEL.test(label) && line.some(hasReference)) {
            projected.push([citation(line)]);
          } else if (SOURCE_HEADING.test(label) && lines[lineIndex + 1]?.some(hasReference)) {
            const sources = [];
            while (lines[lineIndex + 1]?.some(hasReference)) sources.push(lines[++lineIndex]);
            projected.push([citation(joinLines(sources))]);
          } else {
            projected.push(line.map((child, childIndex) => {
              if (!["link", "linkReference"].includes(child.type) || !isSourceLink(child)) return child;
              const previous = line[childIndex - 1];
              const following = line[childIndex + 1];
              const linkLabel = textOf(child).trim();
              const url = linkUrl(child);
              const explicitSource = /^(?:source|citation)$/i.test(child.title || definitions.get(child.identifier)?.title || "");
              // Parenthetical web references are citations, but output-file
              // and navigation actions keep their existing direct controls.
              const parenthetical = /^https?:\/\//i.test(url) && !isOpenableFileReference(url)
                && !/^(?:download|open|visit|view|下载|打开|查看|访问)/i.test(linkLabel)
                && previous?.type === "text" && /[(（]\s*$/.test(previous.value)
                && following?.type === "text" && /^\s*[)）]/.test(following.value);
              return INLINE_LABEL.test(linkLabel) || explicitSource || parenthetical ? citation([child]) : child;
            }));
          }
        }
        result.push({ ...node, children: joinLines(projected) });
      }
      return result;
    };
    tree.children = projectBlocks(tree.children);
  };
}
