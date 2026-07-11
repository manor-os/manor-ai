import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import type { Comment, CommentAnchor } from "../lib/types";

interface CommentThreadProps {
  resourceType: string;
  resourceId: string;
  canComment?: boolean;
  anchor?: CommentAnchor | null;
  activeCommentId?: string | null;
  onCommentsLoaded?: (comments: Comment[]) => void;
  onSelectComment?: (comment: Comment) => void;
}

type CreateCommentInput = {
  content: string;
  parentId?: string;
};

function getAuthorName(comment: Comment) {
  return comment.user_display_name || comment.display_name || t("component.comment_thread.user");
}

function getInitials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1
    ? `${parts[0][0] || ""}${parts[1][0] || ""}`
    : (parts[0] || "U").slice(0, 2);
  return letters.toUpperCase();
}

function anchorLabel(anchor?: CommentAnchor | null) {
  if (!anchor || Object.keys(anchor).length === 0) return "";
  if (anchor.label) return anchor.label;
  if (anchor.line && anchor.line_end && anchor.line_end !== anchor.line) {
    return t("component.comment_thread.lines_range", { start: anchor.line, end: anchor.line_end });
  }
  if (anchor.line) return t("component.comment_thread.line_number", { line: anchor.line });
  if (anchor.quote) return t("component.comment_thread.selected_text");
  return t("component.comment_thread.document");
}

function Avatar({ comment }: { comment: Comment }) {
  const name = getAuthorName(comment);
  if (comment.user_avatar_url) {
    return (
      <img
        src={comment.user_avatar_url}
        alt=""
        className="comment-thread-avatar-img"
      />
    );
  }
  return (
    <div className="comment-thread-avatar">
      {getInitials(name)}
    </div>
  );
}

function CommentItem({
  comment,
  depth = 0,
  canComment,
  activeCommentId,
  activeReplyId,
  replyText,
  isReplyPending,
  onOpenReply,
  onReplyTextChange,
  onSubmitReply,
  onCancelReply,
  onSelectComment,
}: {
  comment: Comment;
  depth?: number;
  canComment: boolean;
  activeCommentId?: string | null;
  activeReplyId?: string | null;
  replyText: string;
  isReplyPending: boolean;
  onOpenReply: (commentId: string) => void;
  onReplyTextChange: (value: string) => void;
  onSubmitReply: (parentId: string) => void;
  onCancelReply: () => void;
  onSelectComment?: (comment: Comment) => void;
}) {
  const author = getAuthorName(comment);
  const label = anchorLabel(comment.anchor);
  const isActive = activeCommentId === comment.id;

  return (
    <div
      className={[
        "comment-thread-item",
        isActive ? "is-active" : "",
        onSelectComment ? "is-clickable" : "",
      ].join(" ")}
      style={{ marginLeft: depth ? 14 : 0 }}
      onClick={() => onSelectComment?.(comment)}
    >
      <div className="flex items-start gap-2.5">
        <Avatar comment={comment} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1 min-w-0">
            <span className="comment-thread-author">{author}</span>
            <span className="comment-thread-time">
              {comment.created_at ? new Date(comment.created_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""}
            </span>
            {comment.is_edited && (
              <span className="comment-thread-time">{t("component.comment_thread.edited")}</span>
            )}
          </div>
          {label && depth === 0 && (
            <div className="mb-2">
              <span className="comment-thread-anchor" title={label}>
                <span className="truncate">{label}</span>
              </span>
              {comment.anchor?.quote && (
                <p className="comment-thread-quote">
                  "{comment.anchor.quote}"
                </p>
              )}
            </div>
          )}
          <p className="comment-thread-body">{comment.content}</p>
          {canComment && (
            <button
              type="button"
              className="comment-thread-reply-button"
              onClick={(event) => {
                event.stopPropagation();
                onOpenReply(comment.id);
              }}
            >
              {t("component.comment_thread.reply")}
            </button>
          )}
        </div>
      </div>

      {activeReplyId === comment.id && (
        <div className="comment-thread-reply-composer" onClick={(event) => event.stopPropagation()}>
          <textarea
            value={replyText}
            onChange={(event) => onReplyTextChange(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                onSubmitReply(comment.id);
              }
            }}
            placeholder={t("component.comment_thread.reply_placeholder")}
            className="comment-thread-textarea"
            rows={2}
            disabled={isReplyPending}
          />
          <div className="comment-thread-composer-actions">
            <button type="button" className="comment-thread-button comment-thread-button--ghost" onClick={onCancelReply}>
              {t("action.cancel")}
            </button>
            <button
              type="button"
              className="comment-thread-button comment-thread-button--primary"
              disabled={!replyText.trim() || isReplyPending}
              onClick={() => onSubmitReply(comment.id)}
            >
              {t("component.comment_thread.reply")}
            </button>
          </div>
        </div>
      )}

      {comment.replies?.length ? (
        <div className="mt-2 flex flex-col gap-2">
          {comment.replies.map((reply) => (
            <CommentItem
              key={reply.id}
              comment={reply}
              depth={depth + 1}
              canComment={canComment}
              activeCommentId={activeCommentId}
              activeReplyId={activeReplyId}
              replyText={replyText}
              isReplyPending={isReplyPending}
              onOpenReply={onOpenReply}
              onReplyTextChange={onReplyTextChange}
              onSubmitReply={onSubmitReply}
              onCancelReply={onCancelReply}
              onSelectComment={onSelectComment}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function CommentThread({
  resourceType,
  resourceId,
  canComment = true,
  anchor,
  activeCommentId,
  onCommentsLoaded,
  onSelectComment,
}: CommentThreadProps) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [replyText, setReplyText] = useState("");
  const [activeReplyId, setActiveReplyId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const { data: comments = [], isError } = useQuery<Comment[]>({
    queryKey: ["comments", resourceType, resourceId],
    queryFn: () => api.comments.list(resourceType, resourceId),
    enabled: !!resourceId,
    retry: false,
  });

  useEffect(() => {
    onCommentsLoaded?.(comments);
  }, [comments, onCommentsLoaded]);

  const createComment = useMutation({
    mutationFn: ({ content, parentId }: CreateCommentInput) =>
      api.comments.create({
        resource_type: resourceType,
        resource_id: resourceId,
        content,
        parent_id: parentId,
        anchor: parentId ? null : anchor || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["comments", resourceType, resourceId] });
      setText("");
      setReplyText("");
      setActiveReplyId(null);
      setError("");
    },
    onError: (err: any) => {
      setError(err?.message || t("component.comment_thread.post_failed"));
    },
  });

  const currentAnchorLabel = useMemo(() => anchorLabel(anchor), [anchor]);

  const submit = () => {
    const content = text.trim();
    if (!content || !canComment) return;
    createComment.mutate({ content });
  };

  const submitReply = (parentId: string) => {
    const content = replyText.trim();
    if (!content || !canComment) return;
    createComment.mutate({ content, parentId });
  };

  return (
    <div className="comment-thread">
      {isError && (
        <p className="text-xs text-red-500 text-center py-3">{t("component.comment_thread.load_failed")}</p>
      )}
      {!isError && comments.length === 0 && (
        <p className="comment-thread-empty">{t("page.task_detail.no_comments_yet")}</p>
      )}
      {comments.map((comment) => (
        <CommentItem
          key={comment.id}
          comment={comment}
          canComment={canComment}
          activeCommentId={activeCommentId}
          activeReplyId={activeReplyId}
          replyText={replyText}
          isReplyPending={createComment.isPending}
          onOpenReply={(commentId) => {
            setActiveReplyId(commentId);
            setReplyText("");
          }}
          onReplyTextChange={setReplyText}
          onSubmitReply={submitReply}
          onCancelReply={() => {
            setActiveReplyId(null);
            setReplyText("");
          }}
          onSelectComment={onSelectComment}
        />
      ))}
      {error && <p className="text-xs text-red-500 m-0">{error}</p>}
      {canComment ? (
        <div className="comment-thread-composer">
          {currentAnchorLabel && (
            <div className="comment-thread-context" title={currentAnchorLabel}>
              <span className="comment-thread-context-label">{t("component.comment_thread.commenting_on")}</span>
              <span className="comment-thread-context-value">{currentAnchorLabel}</span>
              {anchor?.quote && <p className="comment-thread-context-quote">"{anchor.quote}"</p>}
            </div>
          )}
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                submit();
              }
            }}
            placeholder={t("component.comment_thread.add_a_comment")}
            className="comment-thread-textarea"
            rows={3}
            disabled={createComment.isPending}
          />
          <div className="comment-thread-composer-actions">
          <button
            onClick={submit}
            disabled={!text.trim() || createComment.isPending}
            className="comment-thread-button comment-thread-button--primary"
          >
            {t("component.comment_thread.post")}
          </button>
          </div>
        </div>
      ) : (
        <p className="comment-thread-empty">
          {t("component.comment_thread.read_only")}
        </p>
      )}
    </div>
  );
}
