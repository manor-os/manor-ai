export function commentSearchParts(value) {
  const parts = [];
  for (let offset = 0; offset < value.length;) {
    const codePoint = value.codePointAt(offset);
    if (codePoint === undefined) break;
    const rawChar = String.fromCodePoint(codePoint);
    const endOffset = offset + rawChar.length;
    const normalized = rawChar.normalize("NFKC").toLocaleLowerCase();
    if (normalized.trim()) {
      for (const char of normalized) {
        if (char.trim()) parts.push(char);
      }
    }
    offset = endOffset;
  }
  return parts;
}

export function commentSearchKey(value) {
  return commentSearchParts(value).join("");
}

export function searchableCommentQuote(value) {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  const quote = clean.length > 180 ? `${clean.slice(0, 179)}...` : clean;
  return quote.replace(/\.{3}$/, "");
}

function compactCommentActionText(value) {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  return clean.length > 120 ? `${clean.slice(0, 119)}…` : clean;
}

export function commentActionLabel(comment, fallbackQuote, commentLabel = "Comments", ordinal = 1) {
  const label = String(commentLabel || "Comments").trim() || "Comments";
  const position = Number.isInteger(ordinal) && ordinal > 0 ? ordinal : 1;
  const author = compactCommentActionText(comment?.user_display_name || comment?.display_name || "");
  const summary = compactCommentActionText(comment?.content)
    || compactCommentActionText(fallbackQuote);
  const detail = [author, summary].filter(Boolean).join(" — ");
  return `${label} ${position}${detail ? `: ${detail}` : ""}`;
}

export function layoutCommentRanges(ranges) {
  const visibleRanges = [];
  return [...ranges]
    .filter((range) => range.end > range.start)
    .sort((a, b) => a.start - b.start || b.end - a.end)
    .map((range) => {
      const overlapsVisibleRange = visibleRanges.some(
        (visible) => range.start < visible.end && range.end > visible.start,
      );
      if (overlapsVisibleRange) return { ...range, actionOffset: range.start };
      visibleRanges.push(range);
      return { ...range };
    });
}

export function updateCommentMarkActiveState(root, activeCommentId) {
  root.querySelectorAll(".document-comment-mark").forEach((mark) => {
    const active = mark.getAttribute("data-comment-id") === activeCommentId;
    mark.classList.toggle("is-active", active);
    if (mark.tagName === "BUTTON") {
      mark.setAttribute("aria-pressed", String(active));
    }
  });
}

function isTextNode(node) {
  return node?.type === "text" && typeof node.value === "string";
}

function isLinkNode(node) {
  return node?.type === "element" && node.tagName === "a";
}

/**
 * Mark comment quotes in the rendered Markdown HAST. Working after
 * remark-rehype keeps inline code, fenced code, and link text in the same
 * visible-text sequence the browser uses for selection occurrence counts.
 */
export function markdownCommentMarkerPlugin(comments, ranges, activeCommentId, commentLabel = "Comments") {
  return () => (tree) => {
    const commentMetadata = new Map(comments.map((comment, index) => [
      comment.id,
      {
        accessibleLabel: commentActionLabel(
          comment,
          searchableCommentQuote(comment.anchor?.quote || ""),
          commentLabel,
          index + 1,
        ),
      },
    ]));
    const textNodes = [];
    const linkByTextNode = new Map();
    const collectTextNodes = (node, linkAncestor = null) => {
      const childLinkAncestor = isLinkNode(node) ? node : linkAncestor;
      if (isTextNode(node)) {
        textNodes.push(node);
        if (childLinkAncestor) linkByTextNode.set(node, childLinkAncestor);
        return;
      }
      node.children?.forEach((child) => collectTextNodes(child, childLinkAncestor));
    };
    collectTextNodes(tree);

    const positions = [];
    const searchParts = [];
    textNodes.forEach((node) => {
      for (let offset = 0; offset < node.value.length;) {
        const codePoint = node.value.codePointAt(offset);
        if (codePoint === undefined) break;
        const rawChar = String.fromCodePoint(codePoint);
        const endOffset = offset + rawChar.length;
        const normalized = rawChar.normalize("NFKC").toLocaleLowerCase();
        if (normalized.trim()) {
          for (const char of normalized) {
            if (!char.trim()) continue;
            searchParts.push(char);
            positions.push({ node, offset, endOffset });
          }
        }
        offset = endOffset;
      }
    });

    const searchableText = searchParts.join("");
    const nodeOrder = new Map(textNodes.map((node, index) => [node, index]));
    const occupied = new Map();
    const segments = [];
    const overlapActions = [];
    const markedCommentIds = new Set();

    const addCandidateSegments = (candidateSegments) => {
      if (!candidateSegments.length) return false;
      for (const segment of candidateSegments) {
        const nodeRanges = occupied.get(segment.node) || [];
        if (nodeRanges.some((range) => segment.start < range.end && segment.end > range.start)) {
          overlapActions.push(segment);
          return true;
        }
      }
      candidateSegments.forEach((segment) => {
        const nodeRanges = occupied.get(segment.node) || [];
        nodeRanges.push({ start: segment.start, end: segment.end });
        occupied.set(segment.node, nodeRanges);
        segments.push(segment);
      });
      return true;
    };

    for (const [commentIndex, comment] of comments.entries()) {
      const mode = String(comment.anchor?.mode || "").toLowerCase();
      if (mode && mode !== "markdown" && mode !== "richtext") continue;
      const quote = searchableCommentQuote(comment.anchor?.quote || "");
      const key = commentSearchKey(quote);
      if (!key) continue;

      const requestedOccurrence = Number(comment.anchor?.quote_occurrence);
      let searchIndex = -1;
      if (Number.isInteger(requestedOccurrence) && requestedOccurrence >= 0) {
        let searchFrom = 0;
        for (let occurrence = 0; occurrence <= requestedOccurrence; occurrence += 1) {
          searchIndex = searchableText.indexOf(key, searchFrom);
          if (searchIndex < 0) break;
          searchFrom = searchIndex + 1;
        }
      } else {
        const firstMatch = searchableText.indexOf(key);
        searchIndex = firstMatch >= 0 && firstMatch === searchableText.lastIndexOf(key)
          ? firstMatch
          : -1;
      }
      if (searchIndex < 0) continue;

      const startPosition = positions[searchIndex];
      const endPosition = positions[searchIndex + key.length - 1];
      const startNodeIndex = startPosition ? nodeOrder.get(startPosition.node) : undefined;
      const endNodeIndex = endPosition ? nodeOrder.get(endPosition.node) : undefined;
      if (!startPosition || !endPosition || startNodeIndex === undefined || endNodeIndex === undefined) continue;

      const candidateSegments = [];
      for (let index = startNodeIndex; index <= endNodeIndex; index += 1) {
        const node = textNodes[index];
        const start = node === startPosition.node ? startPosition.offset : 0;
        const end = node === endPosition.node ? endPosition.endOffset : node.value.length;
        if (end <= start) continue;
        candidateSegments.push({
          node,
          start,
          end,
          commentId: comment.id,
          quote,
          active: activeCommentId === comment.id,
          accessibleLabel: commentMetadata.get(comment.id)?.accessibleLabel
            || commentActionLabel(comment, quote, commentLabel, commentIndex + 1),
        });
      }
      if (addCandidateSegments(candidateSegments)) markedCommentIds.add(comment.id);
    }

    // Exact source offsets remain a fallback for plain Markdown text nodes.
    // Nodes whose source span includes Markdown syntax (for example backticks)
    // are deliberately skipped so raw offsets cannot highlight the wrong text.
    ranges.forEach((range, rangeIndex) => {
      if (markedCommentIds.has(range.id)) return;
      const candidateSegments = [];
      textNodes.forEach((node) => {
        const nodeStart = node.position?.start?.offset;
        const nodeEnd = node.position?.end?.offset;
        if (
          nodeStart === undefined
          || nodeEnd === undefined
          || nodeEnd - nodeStart !== node.value.length
          || range.start >= nodeEnd
          || range.end <= nodeStart
        ) return;
        const start = Math.max(0, Math.min(node.value.length, range.start - nodeStart));
        const end = Math.max(start, Math.min(node.value.length, range.end - nodeStart));
        if (end <= start) return;
        candidateSegments.push({
          node,
          start,
          end,
          commentId: range.id,
          quote: range.quote || node.value.slice(start, end),
          active: activeCommentId === range.id,
          accessibleLabel: range.accessibleLabel
            || commentMetadata.get(range.id)?.accessibleLabel
            || commentActionLabel(
              null,
              range.quote || node.value.slice(start, end),
              commentLabel,
              comments.length + rangeIndex + 1,
            ),
        });
      });
      addCandidateSegments(candidateSegments);
    });

    const segmentsByNode = new Map();
    segments.forEach((segment) => {
      const nodeSegments = segmentsByNode.get(segment.node) || [];
      nodeSegments.push(segment);
      segmentsByNode.set(segment.node, nodeSegments);
    });
    const overlapActionsByNode = new Map();
    overlapActions.forEach((segment) => {
      const nodeActions = overlapActionsByNode.get(segment.node) || [];
      nodeActions.push(segment);
      overlapActionsByNode.set(segment.node, nodeActions);
    });
    const focusAssignedCommentIds = new Set();
    const commentActionsByNode = new Map();

    const commentAction = (segment) => ({
      type: "element",
      tagName: "button",
      properties: {
        type: "button",
        className: ["document-comment-mark", "document-comment-anchor-action", ...(segment.active ? ["is-active"] : [])],
        "data-comment-id": segment.commentId,
        "aria-label": segment.accessibleLabel,
        "aria-pressed": segment.active,
        title: segment.quote,
      },
      children: [],
    });

    const applySegments = (node) => {
      if (!node.children) return;
      const children = [];
      node.children.forEach((child) => {
        const nodeSegments = isTextNode(child) ? segmentsByNode.get(child) : undefined;
        if (!nodeSegments?.length) {
          applySegments(child);
          children.push(child);
          const commentActions = commentActionsByNode.get(child);
          commentActions?.forEach((segment) => children.push(commentAction(segment)));
          return;
        }

        let cursor = 0;
        nodeSegments
          .sort((a, b) => a.start - b.start)
          .forEach((segment) => {
            if (segment.start > cursor) {
              children.push({ ...child, value: child.value.slice(cursor, segment.start) });
            }
            const properties = {
              className: ["document-comment-mark", ...(segment.active ? ["is-active"] : [])],
              "data-comment-id": segment.commentId,
              title: segment.quote,
            };
            const linkAncestor = linkByTextNode.get(child);
            const receivesFocus = !focusAssignedCommentIds.has(segment.commentId);
            if (linkAncestor && receivesFocus) {
              const commentActions = commentActionsByNode.get(linkAncestor) || [];
              commentActions.push(segment);
              commentActionsByNode.set(linkAncestor, commentActions);
              focusAssignedCommentIds.add(segment.commentId);
            } else if (!linkAncestor && receivesFocus) {
              properties.type = "button";
              properties["aria-label"] = segment.accessibleLabel;
              properties["aria-pressed"] = segment.active;
              focusAssignedCommentIds.add(segment.commentId);
            }
            children.push({
              type: "element",
              tagName: !linkAncestor && receivesFocus ? "button" : "span",
              properties,
              children: [{ ...child, value: child.value.slice(segment.start, segment.end) }],
            });
            cursor = segment.end;
          });
        if (cursor < child.value.length) children.push({ ...child, value: child.value.slice(cursor) });
        const overlapNodeActions = overlapActionsByNode.get(child) || [];
        overlapNodeActions.forEach((segment) => {
          const linkAncestor = linkByTextNode.get(child);
          if (linkAncestor) {
            const commentActions = commentActionsByNode.get(linkAncestor) || [];
            commentActions.push(segment);
            commentActionsByNode.set(linkAncestor, commentActions);
          } else {
            children.push(commentAction(segment));
          }
          focusAssignedCommentIds.add(segment.commentId);
        });
      });
      node.children = children;
    };
    applySegments(tree);
  };
}
