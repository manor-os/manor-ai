import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { SUPPORTED_LOCALES, t, tForLocale } from "../lib/i18n";
import type { Comment, CommentAnchor } from "../lib/types";
import { useAuthStore } from "../stores/auth";
import ConfirmDialog from "./ui/ConfirmDialog";
import ContextMenu from "./ui/ContextMenu";
import { IconComment, IconEdit, IconMoreHorizontal, IconThumbUp, IconTrash } from "./icons";

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

// Older comments persisted translated UI copy as their anchor label. Ignore it
// in every locale, including when the reader has since changed languages.
const legacySelectionLabels = new Set(
  SUPPORTED_LOCALES.map(({ code }) => tForLocale("component.comment_thread.selected_text", code)),
);

function anchorLabel(anchor?: CommentAnchor | null) {
  if (!anchor || Object.keys(anchor).length === 0) return "";
  if (anchor.label && !legacySelectionLabels.has(anchor.label.trim())) return anchor.label;
  if (anchor.line && anchor.line_end && anchor.line_end !== anchor.line) {
    return t("component.comment_thread.lines_range", { start: anchor.line, end: anchor.line_end });
  }
  if (anchor.line) return t("component.comment_thread.line_number", { line: anchor.line });
  if (anchor.quote) return "";
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

type CommentItemProps = {
  comment: Comment;
  depth?: number;
  canComment: boolean;
  currentUserId?: string;
  activeCommentId?: string | null;
  activeReplyId?: string | null;
  replyText: string;
  isReplyPending: boolean;
  editingId?: string | null;
  editingText: string;
  isEditPending: boolean;
  reactingId?: string | null;
  onOpenReply: (commentId: string) => void;
  onReplyTextChange: (value: string) => void;
  onSubmitReply: (parentId: string) => void;
  onCancelReply: () => void;
  onStartEdit: (comment: Comment) => void;
  onEditingTextChange: (value: string) => void;
  onSubmitEdit: (commentId: string) => void;
  onCancelEdit: () => void;
  onRequestDelete: (comment: Comment) => void;
  onToggleReaction: (commentId: string) => void;
  onSelectComment?: (comment: Comment) => void;
};

function CommentItem({
  comment,
  depth = 0,
  canComment,
  currentUserId,
  activeCommentId,
  activeReplyId,
  replyText,
  isReplyPending,
  editingId,
  editingText,
  isEditPending,
  reactingId,
  onOpenReply,
  onReplyTextChange,
  onSubmitReply,
  onCancelReply,
  onStartEdit,
  onEditingTextChange,
  onSubmitEdit,
  onCancelEdit,
  onRequestDelete,
  onToggleReaction,
  onSelectComment,
}: CommentItemProps) {
  const [menuPosition, setMenuPosition] = useState<{ x: number; y: number } | null>(null);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  const closeMenu = () => {
    setMenuPosition(null);
    menuTriggerRef.current?.focus();
  };
  const author = getAuthorName(comment);
  const label = anchorLabel(comment.anchor);
  const isActive = activeCommentId === comment.id;
  const isDeleted = comment.status === "deleted";
  const isOwner = Boolean(currentUserId && currentUserId === comment.user_id);
  const isEditing = editingId === comment.id;
  const reactionUsers = comment.reactions?.thumbsup || [];
  const hasReacted = Boolean(currentUserId && reactionUsers.includes(currentUserId));
  const reactionActionLabel = hasReacted
    ? t("component.comment_thread.liked")
    : t("component.comment_thread.like");
  const reactionLabel = reactionUsers.length > 0
    ? `${reactionActionLabel} (${reactionUsers.length})`
    : reactionActionLabel;

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
      <div className="comment-thread-header">
        <Avatar comment={comment} />
        <div className="comment-thread-identity">
          <span className="comment-thread-author" title={author}>{author}</span>
          <span className="comment-thread-time" title={comment.created_at ? new Date(comment.created_at).toLocaleString() : undefined}>
            {comment.created_at ? new Date(comment.created_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""}
            {comment.is_edited && !isDeleted && ` · ${t("component.comment_thread.edited")}`}
          </span>
        </div>
        {isOwner && !isEditing && !isDeleted && (
          <span className="comment-thread-more" onClick={(event) => event.stopPropagation()}>
            <button
              ref={menuTriggerRef}
              type="button"
              className="comment-thread-inline-action"
              aria-label={t("action.more")}
              title={t("action.more")}
              aria-haspopup="menu"
              aria-expanded={Boolean(menuPosition)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  event.currentTarget.click();
                }
              }}
              onClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect();
                setMenuPosition({ x: rect.right - 180, y: rect.bottom + 4 });
              }}
            >
              <IconMoreHorizontal size={14} />
            </button>
            {menuPosition && (
              <ContextMenu
                {...menuPosition}
                onClose={closeMenu}
                items={[
                  ...(canComment ? [{ label: t("action.edit"), icon: <IconEdit size={14} />, onClick: () => onStartEdit(comment) }] : []),
                  { label: t("action.delete"), icon: <IconTrash size={14} />, danger: true, onClick: () => onRequestDelete(comment) },
                ]}
              />
            )}
          </span>
        )}
      </div>
      {depth === 0 && (label || comment.anchor?.quote) && (
        <div className="mb-2">
          {label && (
            <span className="comment-thread-anchor" title={label}>
              <span className="truncate">{label}</span>
            </span>
          )}
          {comment.anchor?.quote && (
            <blockquote className={`comment-thread-quote${label ? "" : " is-standalone"}`} title={comment.anchor.quote}>
              {comment.anchor.quote}
            </blockquote>
          )}
        </div>
      )}
      {isEditing && !isDeleted ? (
        <div className="comment-thread-edit-composer" onClick={(event) => event.stopPropagation()}>
          <textarea
            autoFocus
            value={editingText}
            onChange={(event) => onEditingTextChange(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                onSubmitEdit(comment.id);
              }
            }}
            className="comment-thread-textarea"
            rows={2}
            disabled={isEditPending}
            aria-label={t("action.edit")}
          />
          <div className="comment-thread-composer-actions">
            <button type="button" className="comment-thread-button comment-thread-button--ghost" onClick={onCancelEdit}>
              {t("action.cancel")}
            </button>
            <button
              type="button"
              className="comment-thread-button comment-thread-button--primary"
              disabled={!editingText.trim() || isEditPending}
              onClick={() => onSubmitEdit(comment.id)}
            >
              {t("action.save")}
            </button>
          </div>
        </div>
      ) : (
        <p className="comment-thread-body">
          {isDeleted ? t("component.comment_thread.deleted") : comment.content}
        </p>
      )}
      {canComment && !isEditing && !isDeleted && (
        <div className="comment-thread-actions" onClick={(event) => event.stopPropagation()}>
          <button
            type="button"
            className={`comment-thread-inline-action${hasReacted ? " is-active" : ""}`}
            aria-pressed={hasReacted}
            aria-label={reactionLabel}
            title={reactionLabel}
            disabled={reactingId === comment.id}
            onClick={() => onToggleReaction(comment.id)}
          >
            <IconThumbUp size={14} />
            {reactionUsers.length > 0 && (
              <span className="comment-thread-reaction-count">{reactionUsers.length}</span>
            )}
          </button>
          <button
            type="button"
            className="comment-thread-inline-action"
            aria-label={t("component.comment_thread.reply")}
            title={t("component.comment_thread.reply")}
            onClick={() => onOpenReply(comment.id)}
          >
            <IconComment size={14} />
          </button>
        </div>
      )}

      {!isDeleted && activeReplyId === comment.id && (
        <div className="comment-thread-reply-composer" onClick={(event) => event.stopPropagation()}>
          <textarea
            autoFocus
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
              {...{
                comment: reply,
                depth: depth + 1,
                canComment,
                currentUserId,
                activeCommentId,
                activeReplyId,
                replyText,
                isReplyPending,
                editingId,
                editingText,
                isEditPending,
                reactingId,
                onOpenReply,
                onReplyTextChange,
                onSubmitReply,
                onCancelReply,
                onStartEdit,
                onEditingTextChange,
                onSubmitEdit,
                onCancelEdit,
                onRequestDelete,
                onToggleReaction,
                onSelectComment,
              }}
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
  const currentUserId = useAuthStore((state) => state.user?.id);
  const [text, setText] = useState("");
  const [replyText, setReplyText] = useState("");
  const [activeReplyId, setActiveReplyId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Comment | null>(null);
  const [error, setError] = useState("");
  const queryKey = ["comments", resourceType, resourceId] as const;

  const { data: comments = [], isError } = useQuery<Comment[]>({
    queryKey,
    queryFn: () => api.comments.list(resourceType, resourceId),
    enabled: !!resourceId,
    retry: false,
  });

  useEffect(() => {
    onCommentsLoaded?.(comments);
  }, [comments, onCommentsLoaded]);

  const refreshComments = () => queryClient.invalidateQueries({ queryKey });
  const mutationError = (err: any, fallback: string) => setError(err?.message || fallback);

  const createComment = useMutation({
    mutationFn: ({ content, parentId }: CreateCommentInput) => api.comments.create({
      resource_type: resourceType,
      resource_id: resourceId,
      content,
      parent_id: parentId,
      anchor: parentId ? null : anchor || null,
    }),
    onSuccess: () => {
      void refreshComments();
      setText("");
      setReplyText("");
      setActiveReplyId(null);
      setError("");
    },
    onError: (err: any) => mutationError(err, t("component.comment_thread.post_failed")),
  });

  const updateComment = useMutation({
    mutationFn: ({ id, content }: { id: string; content: string }) => api.comments.update(id, content),
    onSuccess: () => {
      void refreshComments();
      setEditingId(null);
      setEditingText("");
      setError("");
    },
    onError: (err: any) => mutationError(err, t("component.comment_thread.edit_failed")),
  });

  const deleteComment = useMutation({
    mutationFn: (id: string) => api.comments.delete(id),
    onSuccess: () => {
      void refreshComments();
      setDeleteTarget(null);
      setError("");
    },
    onError: (err: any) => mutationError(err, t("component.comment_thread.delete_failed")),
  });

  const reactComment = useMutation({
    mutationFn: (id: string) => api.comments.react(id, "thumbsup"),
    onSuccess: () => {
      void refreshComments();
      setError("");
    },
    onError: (err: any) => mutationError(err, t("component.comment_thread.reaction_failed")),
  });

  const currentAnchorLabel = useMemo(() => anchorLabel(anchor), [anchor]);

  const submit = () => {
    const content = text.trim();
    if (content && canComment) createComment.mutate({ content });
  };

  const submitReply = (parentId: string) => {
    const content = replyText.trim();
    if (content && canComment) createComment.mutate({ content, parentId });
  };

  const submitEdit = (commentId: string) => {
    const content = editingText.trim();
    if (content && canComment) updateComment.mutate({ id: commentId, content });
  };

  const openDeleteDialog = (comment: Comment) => {
    deleteComment.reset();
    setError("");
    setDeleteTarget(comment);
  };

  const closeDeleteDialog = () => {
    if (deleteComment.isPending) return;
    deleteComment.reset();
    setError("");
    setDeleteTarget(null);
  };

  const itemActions = {
    canComment,
    currentUserId,
    activeCommentId,
    activeReplyId,
    replyText,
    isReplyPending: createComment.isPending,
    editingId,
    editingText,
    isEditPending: updateComment.isPending,
    reactingId: reactComment.isPending ? reactComment.variables : null,
    onOpenReply: (commentId: string) => {
      setEditingId(null);
      setActiveReplyId(commentId);
      setReplyText("");
    },
    onReplyTextChange: setReplyText,
    onSubmitReply: submitReply,
    onCancelReply: () => {
      setActiveReplyId(null);
      setReplyText("");
    },
    onStartEdit: (comment: Comment) => {
      setActiveReplyId(null);
      setEditingId(comment.id);
      setEditingText(comment.content);
    },
    onEditingTextChange: setEditingText,
    onSubmitEdit: submitEdit,
    onCancelEdit: () => {
      setEditingId(null);
      setEditingText("");
    },
    onRequestDelete: openDeleteDialog,
    onToggleReaction: (commentId: string) => reactComment.mutate(commentId),
    onSelectComment,
  };

  return (
    <>
      <div className="comment-thread">
        {isError && (
          <p className="text-xs text-red-500 text-center py-3">{t("component.comment_thread.load_failed")}</p>
        )}
        {!isError && comments.length === 0 && (
          <p className="comment-thread-empty">{t("page.task_detail.no_comments_yet")}</p>
        )}
        {comments.map((comment) => (
          <CommentItem key={comment.id} comment={comment} {...itemActions} />
        ))}
        {error && <p className="text-xs text-red-500 m-0" role="alert">{error}</p>}
        {canComment ? (
          <div className="comment-thread-composer">
            {(currentAnchorLabel || anchor?.quote) && (
              <div className="comment-thread-context" title={currentAnchorLabel}>
                <span className="comment-thread-context-label">{t("component.comment_thread.commenting_on")}</span>
                {currentAnchorLabel && <span className="comment-thread-context-value">{currentAnchorLabel}</span>}
                {anchor?.quote && <p className="comment-thread-context-quote">"{anchor.quote}"</p>}
              </div>
            )}
            <textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault();
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
                type="button"
                onClick={submit}
                disabled={!text.trim() || createComment.isPending}
                className="comment-thread-button comment-thread-button--primary"
              >
                {t("component.comment_thread.post")}
              </button>
            </div>
          </div>
        ) : (
          <p className="comment-thread-empty">{t("component.comment_thread.read_only")}</p>
        )}
      </div>
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onClose={closeDeleteDialog}
        onConfirm={() => {
          if (deleteTarget) deleteComment.mutate(deleteTarget.id);
        }}
        title={t("component.comment_thread.delete_title")}
        message={t("component.comment_thread.delete_message")}
        confirmLabel={t("action.delete")}
        cancelLabel={t("action.cancel")}
        danger
        loading={deleteComment.isPending}
        closeOnConfirm={false}
        error={deleteComment.isError ? error : undefined}
      />
    </>
  );
}
