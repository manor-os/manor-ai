import { startVisiblePolling } from "../lib/visiblePolling";
import { displayReadInterval } from "../lib/readRefreshPolicy";
import ApprovalModeTrigger from "./approvals/ApprovalModeTrigger";
import { getPreferredTimeZone } from "../lib/format";
import { Fragment, Suspense, lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { ChatRoom, ChatRoomAgentRun, ChatRoomFile, ChatRoomInvite, ChatRoomMessage } from "../lib/chatRoomTypes";
import type { Agent } from "../lib/types";
import Button from "./ui/Button";
import Checkbox from "./ui/Checkbox";
import UserAvatar from "./ui/UserAvatar";
import GroupChatAvatar from "./ui/GroupChatAvatar";
import EmptyState from "./ui/EmptyState";
import AnchoredPopover from "./ui/AnchoredPopover";
import Modal from "./ui/Modal";
import ConfirmDialog from "./ui/ConfirmDialog";
import Input from "./ui/Input";
import TabSwitcher from "./ui/TabSwitcher";
import Toggle from "./ui/Toggle";
import ChatInputFooter, { type AttachedItem, type MentionOption } from "./ChatInputFooter";
import AssistantMessageBlocks from "./AssistantMessageBlocks";
import ChatMessageActions from "./chat/ChatMessageActions";
import ChatActionButton from "./chat/ChatActionButton";
import ChatTimestamp, { formatChatDateTime } from "./chat/ChatTimestamp";
import MessageBubble from "./chat/MessageBubble";
import { ArtifactSummaryCards, OutputPanel, type OutputArtifact } from "./EmbeddedChat";
import { getFileReferenceIcon } from "./contentTypeIcons";
import ResizablePaneGroup from "./ui/ResizablePaneGroup";
import { IconChevronDown, IconDownload, IconMoreHorizontal, IconPaperclip, IconReply, IconSmilePlus, IconTrash, IconUsers } from "./icons";
import { getLocale, t } from "../lib/i18n";
import { useDebounced } from "../lib/chatStream";
import { fileReferenceKind } from "../lib/fileReferences";
import { createFilePreviewSource } from "../lib/filePreviewSource";
import { saveRoomFileSelection } from "../lib/roomFileSelectionDownload";
import { ROOM_FILE_BATCH_MAX_BYTES, ROOM_FILE_PREVIEW_MAX_BYTES } from "../lib/roomFileLimits";
import ImagePreview from "./file-preview/ImagePreview";
import { useWebSocket } from "../lib/websocket";
import { useEmailAccounts } from "../lib/emailAccounts";
import DefaultEmailSender from "./integrations/DefaultEmailSender";
import GroupPublicInvite from "./GroupPublicInvite";

const fieldClass = "w-full rounded-xl bg-[var(--surface-muted)] px-3 py-2.5 text-sm text-[var(--text-strong)] outline-none focus:ring-2 focus:ring-[var(--accent-ring)]";
const tr = (zh: string, en: string) => getLocale() === "zh" ? zh : en;
const RoomEmojiPicker = lazy(() => import("./chat/RoomEmojiPicker"));

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : tr("操作失败，请重试", "Something went wrong. Please try again.");
}

function redactRoomMessages(items: ChatRoomMessage[], removedIds: Set<string>): ChatRoomMessage[] {
  return items.map((item) => {
    const removeBody = removedIds.has(item.id) && !item.deleted_at;
    const removeQuote = Boolean(item.reply_to && removedIds.has(item.reply_to.id)
      && item.reply_to.content !== tr("消息已撤回", "Message removed"));
    if (!removeBody && !removeQuote) return item;
    return {
      ...item,
      ...(removeBody ? {
        content: "", deleted_at: new Date().toISOString(),
        mentioned_user_ids: [], agent_runs: [], attachments: [], reactions: [],
      } : {}),
      reply_to: removeQuote && item.reply_to
        ? { ...item.reply_to, content: tr("消息已撤回", "Message removed") }
        : item.reply_to,
    };
  });
}

function inviteLink(token: string): string {
  return `${window.location.origin}/chat?invite=${encodeURIComponent(token)}`;
}

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

function RoomFileTypeIcon({ file }: { file: ChatRoomFile }) {
  const Icon = getFileReferenceIcon(fileReferenceKind(file.name, file.mime_type));
  return <Icon size={19} />;
}

function roomFileArtifact(roomId: string, file: ChatRoomFile, blob?: Blob): OutputArtifact {
  const referenceKind = fileReferenceKind(file.name, file.mime_type);
  const kind = referenceKind === "archive" ? "file" : referenceKind;
  return { id: file.id, kind, title: file.name, status: "done", blob,
    data: { room_id: roomId, room_file_id: file.id, mime_type: file.mime_type, file_size: file.size } };
}

export function RoomMessageAttachment({ roomId, file, onOpen }: {
  roomId: string;
  file: ChatRoomFile;
  onOpen: (file: ChatRoomFile) => void;
}) {
  const mime = file.mime_type.split(";", 1)[0].trim().toLowerCase();
  const previewTooLarge = file.size > ROOM_FILE_PREVIEW_MAX_BYTES;
  const kind = previewTooLarge ? null : /^(image\/(png|jpeg|gif|webp|avif))$/.test(mime) ? "image"
    : mime.startsWith("video/") ? "video"
    : mime.startsWith("audio/") ? "audio" : null;
  const containerRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [source, setSource] = useState("");
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!kind) return;
    const element = containerRef.current;
    if (!element || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setVisible(true);
        observer.disconnect();
      }
    }, { rootMargin: "240px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, [kind]);
  useEffect(() => {
    if (!visible || !kind) return;
    const controller = new AbortController();
    let objectUrl = "";
    setSource("");
    setFailed(false);
    void createFilePreviewSource({
      fetchBinary: (signal) => api.chatRooms.downloadFile(roomId, file.id, { signal, force: attempt > 0 }),
    }).getBlob(controller.signal).then((blob) => {
      objectUrl = URL.createObjectURL(new Blob([blob], { type: mime }));
      if (controller.signal.aborted) URL.revokeObjectURL(objectUrl);
      else setSource(objectUrl);
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [file.id, kind, mime, roomId, visible, attempt]);
  const mediaFailed = () => { setSource(""); setFailed(true); };
  return <div ref={containerRef} className="mt-2 max-w-sm overflow-hidden rounded-xl bg-[var(--surface-muted)]">
    {previewTooLarge && <p className="m-0 px-3 py-2 text-xs text-[var(--text-muted)]">{t("component.group_chat.large_file_preview")}</p>}
    {kind && !source && <div className="flex items-center justify-between gap-2 px-3 py-2 text-xs text-[var(--text-muted)]">
      <span role="status">{failed ? tr("预览加载失败", "Preview could not load") : tr("正在加载预览…", "Loading preview…")}</span>
      {failed && <Button type="button" variant="ghost" size="sm" onClick={() => setAttempt((value) => value + 1)}>{tr("重试", "Retry")}</Button>}
    </div>}
    {kind === "image" && source && <ImagePreview src={source} alt={file.name} onError={mediaFailed} imageClassName="max-h-72 w-full object-contain" />}
    {kind === "video" && source && <video src={source} onError={mediaFailed} controls preload="none" className="max-h-72 w-full" aria-label={file.name} />}
    {kind === "audio" && source && <audio src={source} onError={mediaFailed} controls preload="none" className="w-full" aria-label={file.name} />}
    <button type="button" onClick={() => onOpen(file)} className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left text-xs text-[var(--text-default)] hover:bg-[var(--surface-sunken)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"><RoomFileTypeIcon file={file} /><span className="min-w-0 flex-1 truncate">{file.name}</span><span className="shrink-0 text-[var(--text-muted)]">{fileSize(file.size)}</span></button>
  </div>;
}

function foldLegacyGeneratedFiles(messages: ChatRoomMessage[]): ChatRoomMessage[] {
  const result: ChatRoomMessage[] = [];
  for (let i = 0; i < messages.length; i += 1) {
    const message = messages[i];
    if (message.content !== "Generated a file" || !message.sender_agent_binding_id || !message.attachments.length) {
      result.push(message);
      continue;
    }
    const files = [...message.attachments];
    let next = i + 1;
    while (next < messages.length && messages[next].content === "Generated a file"
      && messages[next].sender_agent_binding_id === message.sender_agent_binding_id
      && messages[next].reply_to_message_id === message.reply_to_message_id) {
      files.push(...messages[next].attachments);
      next += 1;
    }
    const reply = messages[next];
    if (reply?.sender_agent_binding_id === message.sender_agent_binding_id
      && reply.reply_to_message_id === message.reply_to_message_id
      && reply.agent_run_status === "succeeded" && reply.content !== "Generated a file") {
      result.push({ ...reply, attachments: [...reply.attachments, ...files] });
      i = next;
    } else {
      result.push(message);
    }
  }
  return result;
}

function useGroupInviteSender(_userId: string, enabled: boolean) {
  const { data, isFetching, isPending, error, refetch } = useEmailAccounts(enabled);
  const senders = data?.accounts || [];
  const senderId = data?.default_sender_id || "";
  const loading = isFetching || isPending;
  return { senders, senderId, loading, error, refetch, blocked: loading || !!error };
}

function GroupInviteSenderPicker({ sender }: { sender: ReturnType<typeof useGroupInviteSender>; disabled: boolean }) {
  if (sender.loading) return <p role="status" className="m-0 text-xs text-[var(--text-muted)]">{tr("正在加载发送邮箱…", "Loading sending accounts…")}</p>;
  if (sender.error) return <div className="space-y-2"><p role="alert" className="m-0 text-sm text-[var(--text-default)]">{tr("无法加载发送邮箱，请重试。", "Sending accounts could not be loaded. Please retry.")}</p><Button type="button" variant="outline" onClick={() => void sender.refetch()}>{tr("重试", "Retry")}</Button></div>;
  if (!sender.senders.length) return <p className="m-0 text-xs leading-5 text-[var(--text-muted)]">{tr("尚未连接发送邮箱。邀请仍会创建，你可以复制链接分享，或先", "No sending account is connected. You can create the invitation and share its link, or first")} <Link className="underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]" to="/integrations?provider=email">{tr("连接邮箱", "connect an email account")}</Link>{tr("。", ".")}</p>;
  return <DefaultEmailSender account={sender.senders.find((account) => account.id === sender.senderId)} />;
}

function InviteLink({ token, email, nextStep, emailSent }: { token: string; email: string; nextStep: "accept" | "sign_up" | "verify_email" | null; emailSent?: boolean | null }) {
  const [copied, setCopied] = useState(false);
  const link = inviteLink(token);
  return (
    <div className="rounded-xl bg-[var(--surface-muted)] p-3 text-sm text-[var(--text-default)]">
      <p className="m-0 mb-2"><strong>{email}</strong> · {nextStep === null ? tr("请将邀请链接发给对方。", "Share the invitation link with the recipient.") : emailSent ? nextStep === "accept" ? tr("邀请邮件和站内邀请已发送，待对方同意。", "Email and in-app invitations sent; awaiting their acceptance.") : tr("邀请邮件已发送，待对方注册或验证邮箱后同意。", "Invitation email sent; awaiting signup or email verification and acceptance.") : nextStep === "accept" ? tr("站内邀请已发送，但邮件未发送。请复制链接发给对方。", "In-app invitation sent, but email was not sent. Please share the link yourself.") : tr("邮件未发送，请复制链接发给对方。", "Email was not sent; please share the link yourself.")}</p>
      <div className="flex gap-2">
        <input aria-label={tr("邀请链接", "Invitation link")} readOnly value={link} className={`${fieldClass} min-w-0 flex-1`} />
        <Button type="button" variant="outline" onClick={async () => {
          await navigator.clipboard.writeText(link);
          setCopied(true);
        }}>{copied ? tr("已复制", "Copied") : tr("复制链接", "Copy link")}</Button>
      </div>
    </div>
  );
}

export function ChatRoomNewConversation({ initialKind = "dm", rooms, agents, agentsLoading = false, currentUserId, onClose, onCreated, onSelectAgent }: {
  initialKind?: "dm" | "group";
  rooms: ChatRoom[];
  agents: Agent[];
  agentsLoading?: boolean;
  currentUserId: string;
  onClose: () => void;
  onCreated: (room: ChatRoom) => void;
  onSelectAgent: (agent: Agent) => void;
}) {
  const queryClient = useQueryClient();
  const { data: people = [], isLoading: peopleLoading, error: peopleError } = useQuery({ queryKey: ["user-directory"], queryFn: api.users.directory });
  const [recipients, setRecipients] = useState<{ email: string; name: string }[]>([]);
  const [query, setQuery] = useState("");
  const [makeGroup, setMakeGroup] = useState(initialKind === "group");
  const [ownerScope, setOwnerScope] = useState<"personal" | "entity">("personal");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [createdRoom, setCreatedRoom] = useState<ChatRoom | null>(null);
  const [invites, setInvites] = useState<{ token: string; email: string; nextStep: "accept" | "sign_up" | "verify_email" | null; emailSent?: boolean | null }[]>([]);
  const kind = makeGroup || recipients.length > 1 ? "group" : "dm";
  const inviteSender = useGroupInviteSender(currentUserId, kind === "group");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const matchingPeople = recipients.length && !normalizedQuery ? [] : people.filter((person) => person.id !== currentUserId && !recipients.some((recipient) => recipient.email === person.email.toLocaleLowerCase()) && (!normalizedQuery || `${person.display_name || ""} ${person.email}`.toLocaleLowerCase().includes(normalizedQuery))).slice(0, 8);
  const showAgentResults = kind === "dm" && recipients.length === 0;
  const showManorAi = showAgentResults && (!normalizedQuery || "manor ai".includes(normalizedQuery));
  const matchingAgents = showAgentResults ? agents.filter((agent) => agent.id && agent.name && (!normalizedQuery || agent.name.toLocaleLowerCase().includes(normalizedQuery))).slice(0, normalizedQuery ? 8 : 4) : [];
  const matchingRooms = recipients.length ? [] : rooms.filter((room) => !normalizedQuery || room.title.toLocaleLowerCase().includes(normalizedQuery)).slice(0, 5);
  const canAddEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedQuery) && !recipients.some((recipient) => recipient.email === normalizedQuery) && !people.some((person) => person.email.toLocaleLowerCase() === normalizedQuery);
  const recentRoomsSection = matchingRooms.length > 0 && <section>
    <h2 className="m-0 mb-1 px-2 text-xs font-medium text-[var(--text-muted)]">{tr("最近对话", "Recent chats")}</h2>
    {matchingRooms.map((room) => <button key={room.id} type="button" onClick={() => onCreated(room)} className="flex min-h-12 w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]">
      {room.kind === "group" ? <GroupChatAvatar title={room.title} members={room.members} currentUserId={currentUserId} size={30} /> : <UserAvatar name={room.title} avatarUrl={room.members.find((member) => member.user_id !== currentUserId)?.avatar_url} size={30} />}
      <span className="min-w-0"><span className="block truncate text-sm font-medium text-[var(--text-strong)]">{room.title}</span>{room.kind === "group" && room.member_count > 1 && <span className="block truncate text-xs text-[var(--text-muted)]">{room.member_count} {tr("位成员", "members")}</span>}</span>
    </button>)}
  </section>;

  const addRecipient = (email: string, name: string) => {
    setRecipients((current) => [...current, { email: email.toLocaleLowerCase(), name }]);
    setQuery("");
    setError("");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!recipients.length) return;
    if (kind === "group" && inviteSender.blocked) return;
    setBusy(true);
    setError("");
    try {
      const room = await api.chatRooms.create({
        kind, recipient_email: recipients[0].email,
        ...(kind === "group" ? { title: title.trim(), owner_scope: ownerScope, sender_integration_id: inviteSender.senderId || undefined } : {}),
      });
      setCreatedRoom(room);
      const createdInvites: { token: string; email: string; nextStep: "accept" | "sign_up" | "verify_email" | null; emailSent?: boolean | null }[] = [];
      let inviteFailure = false;
      if (room.invite_token) createdInvites.push({ token: room.invite_token, email: recipients[0].email, nextStep: room.invite_next_step || null, emailSent: room.invite_email_sent });
      if (kind === "group") {
        for (const recipient of recipients.slice(1)) {
          try {
            const invite = await api.chatRooms.invite(room.id, recipient.email, inviteSender.senderId || undefined);
            if (invite.outcome === "invited") createdInvites.push({ token: invite.invite_token, email: invite.email, nextStep: invite.invite_next_step, emailSent: invite.email_sent });
          } catch (cause) {
            inviteFailure = true;
            setError(tr(`已创建群聊，但 ${recipient.email} 邀请失败：${errorText(cause)}`, `The group was created, but ${recipient.email} could not be invited: ${errorText(cause)}`));
            break;
          }
        }
      }
      setInvites(createdInvites);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-rooms"] }),
        queryClient.invalidateQueries({ queryKey: ["chat-room", room.id] }),
        queryClient.invalidateQueries({ queryKey: ["chat-room-invites", room.id] }),
      ]);
      if (!createdInvites.length && !inviteFailure) onCreated(room);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  return <div className="flex min-h-0 flex-1 flex-col bg-[var(--chat-surface)]">
    <header className="embedded-chat-header embedded-chat-header--workspace"><div className="workspace-chat-header-main"><h1 className="workspace-chat-title m-0 text-sm font-semibold">{createdRoom ? tr("对话已创建", "Conversation created") : initialKind === "group" ? tr("新建群聊", "New group chat") : tr("新建消息", "New message")}</h1></div><Button type="button" variant="ghost" onClick={onClose}>{tr("关闭", "Close")}</Button></header>
    {createdRoom ? <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-4 overflow-y-auto px-6 py-8"><h2 className="m-0 text-lg font-semibold text-[var(--text-strong)]">{createdRoom.title}</h2><p className="m-0 text-sm text-[var(--text-muted)]">{invites.length ? tr("受邀人同意前不能查看群消息。邀请链接也可手动分享。", "Invitees cannot see group messages until they accept. You can also share each link manually.") : tr("可以开始对话了。", "You're ready to chat.")}</p>{invites.map((invite) => <InviteLink key={invite.email} {...invite} />)}{error && <p role="alert" className="text-sm text-red-700">{error}</p>}<Button className="self-start" onClick={() => onCreated(createdRoom)}>{tr("打开对话", "Open conversation")}</Button></div> : <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
      <div className="px-5 pt-5 sm:px-8"><div className="mx-auto flex min-h-12 w-full max-w-2xl flex-wrap items-center gap-2 rounded-xl bg-[var(--surface-muted)] px-3 py-2 focus-within:ring-2 focus-within:ring-[var(--accent-ring)]"><label htmlFor="new-chat-recipient" className="shrink-0 text-sm font-semibold text-[var(--text-strong)]">{tr("收件人：", "To:")}</label>{recipients.map((recipient) => <span key={recipient.email} className="inline-flex max-w-full items-center gap-1 rounded-lg bg-[var(--surface-sunken)] px-2.5 py-1 text-sm text-[var(--text-strong)]"><span className="truncate">{recipient.name}</span><button type="button" className="rounded px-1 text-[var(--text-muted)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]" aria-label={`${tr("移除", "Remove")} ${recipient.name}`} onClick={() => setRecipients((current) => current.filter((item) => item.email !== recipient.email))}>×</button></span>)}<input id="new-chat-recipient" autoFocus autoComplete="off" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        if (canAddEmail) { addRecipient(normalizedQuery, normalizedQuery); return; }
        if (!normalizedQuery) return;
        if (matchingPeople.length + matchingAgents.length + matchingRooms.length + Number(showManorAi) !== 1) return;
        if (matchingPeople[0]) addRecipient(matchingPeople[0].email, matchingPeople[0].display_name || matchingPeople[0].email);
        else if (matchingAgents[0]) onSelectAgent(matchingAgents[0]);
        else if (showManorAi) onClose();
        else if (matchingRooms[0]) onCreated(matchingRooms[0]);
      }} placeholder={recipients.length ? tr("添加更多人…", "Add more people…") : tr("姓名、邮箱或 Agent", "Name, email, or Agent")} className="min-w-[180px] flex-1 bg-transparent py-1 text-sm text-[var(--text-strong)] outline-none placeholder:text-[var(--text-faint)]" /></div></div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-8 pt-4 sm:px-8"><div className="mx-auto w-full max-w-2xl space-y-3">
        {!normalizedQuery && recentRoomsSection}
        {(matchingPeople.length > 0 || peopleLoading || peopleError) && <section><h2 className="m-0 mb-1 px-2 text-xs font-medium text-[var(--text-muted)]">{tr("团队成员", "Team members")}</h2>{peopleLoading ? <p className="px-2 text-sm text-[var(--text-muted)]">{tr("正在加载…", "Loading…")}</p> : peopleError ? <p role="alert" className="px-2 text-sm text-[var(--text-muted)]">{tr("暂时无法加载团队成员，仍可输入邮箱邀请。", "Team members are unavailable; you can still invite by email.")}</p> : matchingPeople.map((person) => <button key={person.id} type="button" onClick={() => addRecipient(person.email, person.display_name || person.email)} className="flex min-h-12 w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"><UserAvatar name={person.display_name || person.email} avatarUrl={person.avatar_url} size={30} /><span className="min-w-0"><span className="block truncate text-sm font-medium text-[var(--text-strong)]">{person.display_name || person.email}</span><span className="block truncate text-xs text-[var(--text-muted)]">{person.email}</span></span></button>)}</section>}
        {canAddEmail && <button type="button" onClick={() => addRecipient(normalizedQuery, normalizedQuery)} className="flex min-h-12 w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"><UserAvatar name={normalizedQuery} size={30} /><span className="min-w-0 truncate text-sm text-[var(--text-strong)]">{tr("邀请", "Invite")} <strong>{normalizedQuery}</strong></span></button>}
        {(showManorAi || matchingAgents.length > 0 || (showAgentResults && agentsLoading && !!normalizedQuery)) && <section><h2 className="m-0 mb-1 px-2 text-xs font-medium text-[var(--text-muted)]">{tr("AI 助手", "AI agents")}</h2>
          {showManorAi && <button type="button" onClick={onClose} className="flex min-h-12 w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"><UserAvatar type="manor" size={30} /><span className="min-w-0"><span className="block truncate text-sm font-medium text-[var(--text-strong)]">Manor AI</span><span className="block truncate text-xs text-[var(--text-muted)]">{tr("你的 AI 助手", "Your AI assistant")}</span></span></button>}
          {matchingAgents.map((agent) => <button key={agent.id} type="button" onClick={() => onSelectAgent(agent)} className="flex min-h-12 w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"><UserAvatar type="agent" name={agent.name} avatarUrl={agent.avatar_url} seed={agent.id} size={30} /><span className="min-w-0"><span className="block truncate text-sm font-medium text-[var(--text-strong)]">{agent.name}</span><span className="block truncate text-xs text-[var(--text-muted)]">{agent.workspace_name ? tr(`工作空间 · ${agent.workspace_name}`, `Workspace · ${agent.workspace_name}`) : tr("个人 Agent", "Personal Agent")}</span></span></button>)}
          {agentsLoading && normalizedQuery && !matchingAgents.length && !showManorAi && <p className="m-0 px-2 py-2 text-sm text-[var(--text-muted)]">{tr("正在查找 Agent…", "Finding Agents…")}</p>}
        </section>}
        {normalizedQuery && recentRoomsSection}
        {normalizedQuery && !matchingPeople.length && !matchingAgents.length && !showManorAi && !matchingRooms.length && !canAddEmail && !peopleLoading && !peopleError && !agentsLoading && <p role="status" className="m-0 px-2 py-2 text-sm text-[var(--text-muted)]">{tr("没有匹配结果。输入完整邮箱即可邀请。", "No matches. Enter a full email address to invite someone.")}</p>}
        {recipients.length > 0 && <section className="max-w-xl">
          <label className="flex items-center gap-2 py-2 text-sm font-medium text-[var(--text-strong)]"><input type="checkbox" className="accent-[var(--accent)]" checked={kind === "group"} disabled={recipients.length > 1} onChange={(event) => setMakeGroup(event.target.checked)} />{tr("创建群聊", "Make this a group chat")}</label>
          {kind === "group" && <div className="mt-4 space-y-4">
            <div>
              <label htmlFor="new-chat-group-name" className="mb-1.5 block text-sm font-medium text-[var(--text-default)]">{tr("群名称", "Group name")}</label>
              <Input id="new-chat-group-name" appearance="quiet" maxLength={255} required value={title} onChange={(event) => setTitle(event.target.value)} placeholder={tr("例如：项目客户群", "For example: Customer project")} />
            </div>
            <fieldset className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-[var(--text-default)]">
              <legend className="mb-1.5 text-sm font-medium">{tr("群聊归属", "Group owner")}</legend>
              <label className="flex items-center gap-1.5"><input type="radio" className="accent-[var(--accent)]" name="owner-scope" value="personal" checked={ownerScope === "personal"} onChange={() => setOwnerScope("personal")} />{tr("个人", "Personal")}</label>
              <label className="flex items-center gap-1.5"><input type="radio" className="accent-[var(--accent)]" name="owner-scope" value="entity" checked={ownerScope === "entity"} onChange={() => setOwnerScope("entity")} />{tr("当前公司", "Current company")}</label>
            </fieldset>
            <GroupInviteSenderPicker sender={inviteSender} disabled={busy} />
          </div>}
          <div className="mt-5 flex flex-col items-start gap-3">
            <p className="m-0 text-xs leading-5 text-[var(--text-muted)]">{kind === "group" ? tr("新成员只看到加入后的消息；群聊不会授予公司或 Workspace 权限。", "New members only see messages after joining. Group access does not grant company or Workspace access.") : tr("对方接受邀请后才能开始私聊。", "You can message once the recipient accepts the invite.")}</p>
            <Button type="submit" className="w-full sm:w-auto" disabled={busy || (kind === "group" && (!title.trim() || inviteSender.blocked))}>{busy ? tr("创建中…", "Creating…") : kind === "group" ? tr("创建群聊", "Create group") : tr("开始私聊", "Start direct message")}</Button>
          </div>
        </section>}
        {error && <p role="alert" className="m-0 text-sm text-red-700">{error}</p>}
      </div></div>
    </form>}
  </div>;
}

function hasMentionToken(content: string, name: string): boolean {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|\\s)@${escaped}(?=$|\\s|[.,!?;:，。！？；：])`, "u").test(content);
}

function MessageContent({ message, room }: { message: ChatRoomMessage; room: ChatRoom }) {
  const names = [...new Set([
    ...room.members.filter((member) => message.mentioned_user_ids.includes(member.user_id)).map((member) => member.name),
    ...room.agents.filter((agent) => message.mentioned_agent_binding_ids?.includes(agent.id)).map((agent) => agent.name),
  ])].sort((left, right) => right.length - left.length);
  if (!names.length) return <span className="whitespace-pre-wrap break-words">{message.content}</span>;
  const escaped = names.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pieces = message.content.split(new RegExp(`(@(?:${escaped.join("|")}))`, "g"));
  return <span className="whitespace-pre-wrap break-words">{pieces.map((part, index) => names.some((name) => part === `@${name}`) ? <span key={index} className="rounded bg-[var(--surface-sunken)] px-0.5 font-semibold text-[var(--text-strong)]">{part}</span> : part)}</span>;
}

function PresenceStatus({ online }: { online: boolean }) {
  return <span className="inline-flex items-center gap-1.5 text-xs text-[var(--text-muted)]"><span aria-hidden="true" className={online ? "chat-status-dot chat-status-dot--online" : "chat-status-dot bg-[var(--text-faint)]"} />{online ? tr("在线", "Online") : tr("离线", "Offline")}</span>;
}

function RoomAgentRunIndicator({ run, onReview }: { run: ChatRoomAgentRun; onReview?: () => void }) {
  const status = run.status === "failed"
    ? run.error_code === "llm_configuration_missing"
      ? tr("尚未配置 AI 模型服务，请联系管理员配置", "AI model provider is not configured. Ask an admin to configure one")
      : tr("未能回复，请重试或联系管理员", "Could not reply. Try again or ask an admin")
    : run.status === "interrupted" ? tr("回复已停止，请重试", "Reply stopped. Try again") : "";
  return <>
    <AssistantMessageBlocks blocks={run.assistant_blocks?.length ? run.assistant_blocks : [{
      id: `room-process-${run.id}`, type: "process", steps: [],
      lifecycle_status: run.status === "failed" ? "failed" : run.status === "interrupted" ? "cancelled"
        : run.status === "pending" || run.status === "waiting_approval" ? "waiting" : "running",
    }]} keyPrefix={run.id} sourceMessageId={run.id} minimal enableFileCards={false}
      streaming={run.status === "pending" || run.status === "running"}
      queueStatus={run.status === "pending" ? tr("等待执行", "Waiting to execute") : undefined}
      pendingActionKind={run.status === "waiting_approval" ? "approval" : null} />
    {status && <span role="status" className="max-w-full break-words text-xs text-[var(--text-muted)]">{status}</span>}
    {run.status === "waiting_approval" && onReview && <Button type="button" size="sm" variant="ghost" onClick={onReview}
      aria-label={run.approval_kind === "reply" ? tr("回复待审批", "Reply awaiting approval") : tr("文件请求待处理", "File request pending")}>{tr("查看", "Review")}</Button>}
  </>;
}

export type ChatRoomRequestedAction = {
  id: number;
  roomId: string;
  action: "details" | "rename" | "close";
};

export function ChatRoomConversation({ roomId, currentUserId, requestedAction }: { roomId: string; currentUserId: string; requestedAction?: ChatRoomRequestedAction }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [messageQuery, setMessageQuery] = useState("");
  const searchedMessageQuery = useDebounced(messageQuery.trim(), 250);
  const [olderMessages, setOlderMessages] = useState<ChatRoomMessage[]>([]);
  const [anchoredLatestMessages, setAnchoredLatestMessages] = useState<ChatRoomMessage[] | null>(null);
  const [hasMoreMessages, setHasMoreMessages] = useState(true);
  const [loadingOlderMessages, setLoadingOlderMessages] = useState(false);
  const [olderMessagesError, setOlderMessagesError] = useState("");
  const historyRequestRef = useRef(0);
  const { data: room, isLoading, error: roomError } = useQuery({ queryKey: ["chat-room", roomId], queryFn: () => api.chatRooms.get(roomId), refetchInterval: (query) => displayReadInterval(query.state, 120_000) });
  const messageQueryKey = ["chat-room-messages", roomId, searchedMessageQuery];
  const { data: latestMessages = [], error: messagesError } = useQuery({
    queryKey: messageQueryKey,
    queryFn: async () => {
      const latest = await api.chatRooms.messages(roomId, { q: searchedMessageQuery || undefined });
      if (searchedMessageQuery || !latest.length) return latest;
      const cached = queryClient.getQueryData<ChatRoomMessage[]>(messageQueryKey);
      if (!cached?.length) return latest;
      const known = new Set(cached.map((message) => message.id));
      const caughtUp = latest.some((message) => known.has(message.id));
      const newMessages: ChatRoomMessage[] = [];
      if (!caughtUp) {
        // A busy room can receive more than 50 messages between polls. Walk
        // forward from the last visible message instead of jumping over them.
        let cursor = cached[cached.length - 1].id;
        for (let page = 0; page < 10; page += 1) {
          const batch = await api.chatRooms.messages(roomId, { after: cursor, limit: 100 });
          if (!batch.length) break;
          newMessages.push(...batch);
          cursor = batch[batch.length - 1].id;
          if (batch.length < 100 || batch.some((message) => latest.some((item) => item.id === message.id))) break;
        }
      }
      const merged = new Map(cached.map((message) => [message.id, message]));
      for (const message of newMessages) merged.set(message.id, message);
      // The latest page also refreshes reactions and Agent state on existing messages.
      // Only append it once the catch-up pages overlap, so no gap appears.
      if (caughtUp || newMessages.length < 1000) {
        for (const message of latest) merged.set(message.id, message);
      }
      return [...merged.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)).slice(-200);
    },
    enabled: !!room && !anchoredLatestMessages,
    refetchInterval: (query) => searchedMessageQuery || anchoredLatestMessages
      ? false : displayReadInterval(query.state, 120_000),
  });
  const messages = [...olderMessages, ...(anchoredLatestMessages || latestMessages)];
  const displayedMessageIdsRef = useRef<string[]>([]);
  displayedMessageIdsRef.current = messages.map((message) => message.id).slice(-1000);
  const retractionPollRef = useRef<ReturnType<typeof startVisiblePolling> | null>(null);
  const refreshRetractions = useCallback(() => { void retractionPollRef.current?.refresh(); }, []);
  useEffect(() => {
    const poll = startVisiblePolling(async (signal) => {
      const ids = displayedMessageIdsRef.current;
      if (!ids.length) return;
      const { message_ids } = await api.chatRooms.retractions(roomId, ids, signal);
      if (signal.aborted || !message_ids.length) return;
      const removed = new Set(message_ids);
      setOlderMessages((items) => redactRoomMessages(items, removed));
      setAnchoredLatestMessages((items) => items ? redactRoomMessages(items, removed) : null);
      queryClient.setQueriesData<ChatRoomMessage[]>({ queryKey: ["chat-room-messages", roomId] },
        (items) => items ? redactRoomMessages(items, removed) : items);
    }, 120_000, { immediate: false });
    retractionPollRef.current = poll;
    return () => { poll.stop(); retractionPollRef.current = null; };
  }, [roomId, queryClient]);
  const roomRefreshTimerRef = useRef<number | null>(null);
  const roomCatchupTimerRef = useRef<number | null>(null);
  const pendingPreviousIdsRef = useRef(new Set<string>());
  const retractionPendingRef = useRef(false);
  useEffect(() => () => {
    if (roomRefreshTimerRef.current !== null) window.clearTimeout(roomRefreshTimerRef.current);
    if (roomCatchupTimerRef.current !== null) window.clearTimeout(roomCatchupTimerRef.current);
    roomRefreshTimerRef.current = null;
    roomCatchupTimerRef.current = null;
    pendingPreviousIdsRef.current.clear();
    retractionPendingRef.current = false;
  }, [roomId]);
  useWebSocket({
    onConnect: () => {
      void queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] });
      void queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] });
      void queryClient.invalidateQueries({ queryKey: ["chat-room-presence", roomId] });
      refreshRetractions();
    },
    onChatRoomChanged: ({ room_id, kind, message, previous_message_id }) => {
      if (room_id !== roomId) return;
      if (kind === "message" && message?.room_id === roomId) {
        if (!searchedMessageQuery && !anchoredLatestMessages) {
          let missedMessage = false;
          let missingId = previous_message_id || `!${message.id}`;
          queryClient.setQueryData<ChatRoomMessage[]>(messageQueryKey, (cached) => {
            if (!cached) {
              missedMessage = true;
              missingId = `!${message.id}`;
              return cached;
            }
            if (cached.some((item) => item.id === message.id)) return cached;
            const latest = cached[cached.length - 1];
            if (previous_message_id && latest?.id !== previous_message_id
              && (!latest || Date.parse(message.created_at) >= Date.parse(latest.created_at))) {
              missedMessage = true;
            }
            const updated = message.sender_agent_binding_id && message.reply_to_message_id
              ? cached.map((item) => item.id === message.reply_to_message_id ? {
                ...item,
                agent_runs: item.agent_runs?.map((run) => run.binding_id === message.sender_agent_binding_id
                  && (run.status === "pending" || run.status === "running" || run.status === "waiting_approval")
                  ? { ...run, status: "succeeded" as const } : run),
              } : item)
              : cached;
            return [...updated, message].sort((left, right) =>
              Date.parse(left.created_at) - Date.parse(right.created_at) || left.id.localeCompare(right.id)).slice(-200);
          });
          if (missedMessage) {
            pendingPreviousIdsRef.current.add(missingId);
            if (roomCatchupTimerRef.current === null) {
              roomCatchupTimerRef.current = window.setTimeout(() => {
                roomCatchupTimerRef.current = null;
                const cached = queryClient.getQueryData<ChatRoomMessage[]>(messageQueryKey);
                const known = new Set(cached?.map((item) => item.id));
                const missing = !cached || [...pendingPreviousIdsRef.current].some((id) =>
                  !known.has(id.startsWith("!") ? id.slice(1) : id));
                pendingPreviousIdsRef.current.clear();
                if (missing) {
                  void queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] });
                  void queryClient.invalidateQueries({ queryKey: ["chat-rooms"] });
                }
              }, 350);
            }
          }
        }
        if (message.attachments.length) {
          void queryClient.invalidateQueries({ queryKey: ["chat-room-files", roomId] });
        }
        return;
      }
      if (kind === "retracted") retractionPendingRef.current = true;
      if (roomRefreshTimerRef.current !== null) return;
      roomRefreshTimerRef.current = window.setTimeout(() => {
        roomRefreshTimerRef.current = null;
        void queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] });
        void queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] });
        void queryClient.invalidateQueries({ queryKey: ["chat-room-files", roomId] });
        void queryClient.invalidateQueries({ queryKey: ["chat-room-invites", roomId] });
        void queryClient.invalidateQueries({ queryKey: ["chat-room-tool-approvals", roomId] });
        void queryClient.invalidateQueries({ queryKey: ["chat-room-reply-approvals", roomId] });
        if (retractionPendingRef.current) {
          retractionPendingRef.current = false;
          refreshRetractions();
        }
      }, 150);
    },
  });
  const { data: presence, error: presenceError } = useQuery({ queryKey: ["chat-room-presence", roomId], queryFn: () => api.chatRooms.presence(roomId), enabled: !!room, refetchInterval: (query) => displayReadInterval(query.state, 60_000) });
  const [draft, setDraft] = useState("");
  const [replyingTo, setReplyingTo] = useState<ChatRoomMessage | null>(null);
  const [messageToRetract, setMessageToRetract] = useState<ChatRoomMessage | null>(null);
  const [retractingMessageId, setRetractingMessageId] = useState<string | null>(null);
  const [retractError, setRetractError] = useState("");
  const [notificationSettingBusy, setNotificationSettingBusy] = useState(false);
  const [reactingKey, setReactingKey] = useState<string | null>(null);
  const [view, setView] = useState<"messages" | "files">("messages");
  const [fileQuery, setFileQuery] = useState("");
  const debouncedFileQuery = useDebounced(fileQuery.trim(), 250);
  const [fileActionError, setFileActionError] = useState("");
  const [fileToDelete, setFileToDelete] = useState<ChatRoomFile | null>(null);
  const [deletingFileId, setDeletingFileId] = useState<string | null>(null);
  const [deleteFileError, setDeleteFileError] = useState("");
  const [downloadingFileId, setDownloadingFileId] = useState<string | null>(null);
  const [selectedFileIds, setSelectedFileIds] = useState<Set<string>>(() => new Set());
  const [batchDownloadPhase, setBatchDownloadPhase] = useState<"preparing" | "downloading" | null>(null);
  const [fileDownloadNotice, setFileDownloadNotice] = useState("");
  const singleDownloadAbort = useRef<AbortController | null>(null);
  const batchDownloadAbort = useRef<AbortController | null>(null);
  const [previewingFileId, setPreviewingFileId] = useState<string | null>(null);
  const [selectedArtifact, setSelectedArtifact] = useState<OutputArtifact | null>(null);
  const previewRequest = useRef(0);
  const previewAbort = useRef<AbortController | null>(null);
  const displayMessages = useMemo(() => foldLegacyGeneratedFiles(messages), [messages]);
  const chatBodyRef = useRef<HTMLDivElement | null>(null);
  const chatContentRef = useRef<HTMLDivElement | null>(null);
  const autoFollowRef = useRef(true);
  const historyScrollRef = useRef<{ top: number; height: number } | null>(null);
  const scrollToLatest = useCallback(() => {
    const container = chatBodyRef.current;
    if (!container) return;
    const previousScrollBehavior = container.style.scrollBehavior;
    container.style.scrollBehavior = "auto";
    container.scrollTop = container.scrollHeight;
    container.style.scrollBehavior = previousScrollBehavior;
  }, []);
  useLayoutEffect(() => {
    const container = chatBodyRef.current;
    const content = chatContentRef.current;
    if (!container || !content) return;
    autoFollowRef.current = !searchedMessageQuery && !anchoredLatestMessages;
    if (autoFollowRef.current) scrollToLatest();
    const updateFollow = () => {
      autoFollowRef.current = !searchedMessageQuery && !anchoredLatestMessages
        && container.scrollHeight - container.scrollTop - container.clientHeight < 80;
    };
    container.addEventListener("scroll", updateFollow, { passive: true });
    // Images, file cards and the viewport can resize after the messages load.
    const observer = new ResizeObserver(() => {
      if (autoFollowRef.current) scrollToLatest();
    });
    observer.observe(content);
    observer.observe(container);
    return () => {
      container.removeEventListener("scroll", updateFollow);
      observer.disconnect();
    };
  }, [roomId, room?.id, view, searchedMessageQuery, Boolean(anchoredLatestMessages), scrollToLatest]);
  useLayoutEffect(() => {
    const container = chatBodyRef.current;
    const previous = historyScrollRef.current;
    if (container && previous) {
      const previousScrollBehavior = container.style.scrollBehavior;
      container.style.scrollBehavior = "auto";
      container.scrollTop = previous.top + container.scrollHeight - previous.height;
      container.style.scrollBehavior = previousScrollBehavior;
      historyScrollRef.current = null;
      return;
    }
    if (autoFollowRef.current && !searchedMessageQuery && !anchoredLatestMessages) scrollToLatest();
  }, [displayMessages, searchedMessageQuery, anchoredLatestMessages, scrollToLatest]);
  const [retryAttachments, setRetryAttachments] = useState<AttachedItem[]>([]);
  const [retryAttachmentSeed, setRetryAttachmentSeed] = useState(0);
  const [selectedMentionKeys, setSelectedMentionKeys] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [sendStatus, setSendStatus] = useState<
    { kind: "message" } | { kind: "file"; index: number; total: number; name: string } | null
  >(null);
  const [error, setError] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteMode, setInviteMode] = useState("email");
  const [inviteResult, setInviteResult] = useState<ChatRoomInvite | null>(null);
  const inviteSender = useGroupInviteSender(currentUserId, inviteOpen && inviteMode === "email" && room?.kind === "group");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [detailsTab, setDetailsTab] = useState("members");
  const [membersOpen, setMembersOpen] = useState(false);
  const [memberQuery, setMemberQuery] = useState("");
  const [invitesExpanded, setInvitesExpanded] = useState(false);
  const [agentPickerOpen, setAgentPickerOpen] = useState(false);
  const [memberMenuId, setMemberMenuId] = useState<string | null>(null);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameTitle, setRenameTitle] = useState("");
  const [closeOpen, setCloseOpen] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [approvalOpen, setApprovalOpen] = useState(false);
  const [approvalDecisionId, setApprovalDecisionId] = useState<string | null>(null);
  const [approvalError, setApprovalError] = useState("");
  const [aiSettingBusy, setAiSettingBusy] = useState(false);
  const [cancellingInviteId, setCancellingInviteId] = useState<string | null>(null);
  const readThroughRef = useRef<string | null>(null);
  const handledActionId = useRef<number | null>(null);
  useEffect(() => {
    readThroughRef.current = null;
    historyScrollRef.current = null;
    previewRequest.current += 1;
    previewAbort.current?.abort();
    setSelectedArtifact(null);
    setPreviewingFileId(null);
    setView("messages");
    setFileQuery("");
    setFileDownloadNotice("");
    setSelectedFileIds(new Set());
    setFileActionError("");
    setFileToDelete(null);
    setDeleteFileError("");
    setDraft("");
    setMessageQuery("");
    historyRequestRef.current += 1;
    setOlderMessages([]);
    setAnchoredLatestMessages(null);
    setHasMoreMessages(true);
    setOlderMessagesError("");
    setReplyingTo(null);
    setRetryAttachments([]);
    setSelectedMentionKeys([]);
    setError("");
    setSendStatus(null);
  }, [roomId]);
  useEffect(() => {
    setBatchDownloadPhase(null);
    setDownloadingFileId(null);
    return () => {
      singleDownloadAbort.current?.abort();
      singleDownloadAbort.current = null;
      batchDownloadAbort.current?.abort();
      batchDownloadAbort.current = null;
    };
  }, [roomId, view]);
  useEffect(() => {
    if (view !== "messages" || !room || !latestMessages.length || searchedMessageQuery || anchoredLatestMessages) return;
    const latest = latestMessages[latestMessages.length - 1].id;
    let readTimer: number | null = null;
    const markVisibleMessagesRead = () => {
      if (readTimer !== null) window.clearTimeout(readTimer);
      if (document.visibilityState !== "visible" || !document.hasFocus() || readThroughRef.current === latest) return;
      // A burst of WebSocket messages should produce one read receipt, not
      // one HTTP request per delivered message.
      readTimer = window.setTimeout(() => {
        readTimer = null;
        if (document.visibilityState !== "visible" || !document.hasFocus()) return;
        readThroughRef.current = latest;
        void api.chatRooms.markRead(roomId, latest).then(() => {
          if (queryClient.getQueryData<ChatRoomMessage[]>(messageQueryKey)?.at(-1)?.id !== latest) return;
          queryClient.setQueriesData<ChatRoom[]>({ queryKey: ["chat-rooms"] }, (rooms) => rooms?.map((item) =>
            item.id === roomId ? { ...item, unread_count: 0 } : item));
        }).catch(() => { if (readThroughRef.current === latest) readThroughRef.current = null; });
      }, 400);
    };
    markVisibleMessagesRead();
    document.addEventListener("visibilitychange", markVisibleMessagesRead);
    window.addEventListener("focus", markVisibleMessagesRead);
    return () => {
      if (readTimer !== null) window.clearTimeout(readTimer);
      document.removeEventListener("visibilitychange", markVisibleMessagesRead);
      window.removeEventListener("focus", markVisibleMessagesRead);
    };
  }, [latestMessages, queryClient, room, roomId, view, searchedMessageQuery, anchoredLatestMessages]);
  useEffect(() => () => {
    previewRequest.current += 1;
    previewAbort.current?.abort();
  }, []);
  const currentMember = room?.members.find((member) => member.user_id === currentUserId);
  const visibleAgents = room?.agents.filter((agent) => agent.status !== "disabled") || [];
  const canInvite = room?.kind === "group" && !!currentMember;
  const canManageGroup = room?.kind === "group" && (currentMember?.role === "owner" || currentMember?.role === "admin");
  const canReviewAiTools = canManageGroup;
  const canManageRoles = room?.kind === "group" && currentMember?.role === "owner";
  const canReissueDmInvite = room?.kind === "dm" && room.member_count === 1 && currentMember?.role === "owner";
  useEffect(() => {
    if (!requestedAction || !room || room.id !== roomId || requestedAction.roomId !== roomId || handledActionId.current === requestedAction.id) return;
    handledActionId.current = requestedAction.id;
    setError("");
    if (requestedAction.action === "details") {
      setRenameOpen(false);
      setCloseOpen(false);
      setDetailsOpen(true);
    } else if (requestedAction.action === "rename" && canManageGroup) {
      setDetailsOpen(false);
      setCloseOpen(false);
      setRenameTitle(room.title);
      setRenameOpen(true);
    } else if (requestedAction.action === "close" && canManageRoles) {
      setDetailsOpen(false);
      setRenameOpen(false);
      setCloseOpen(true);
    } else {
      setError(tr("你没有管理此群聊的权限。", "You cannot manage this group."));
    }
  }, [requestedAction, room, roomId, canManageGroup, canManageRoles]);
  const mentionOptions = useMemo<MentionOption[]>(() => [
    ...(room?.kind === "group" ? room.agents.filter((agent) => agent.available && agent.status !== "disabled").map((agent) => ({
      id: agent.id,
      type: "agent" as const,
      name: agent.name,
      avatarSeed: agent.id,
      subtitle: agent.is_default
        ? tr("仅本群", "This group only")
        : agent.workspace_name
          ? `${tr("Workspace", "Workspace")} · ${agent.workspace_name}`
          : tr("个人 Agent", "Personal Agent"),
    })) : []),
    ...(room?.members.filter((member) => member.user_id !== currentUserId).map((member) => ({
      id: member.user_id,
      type: "user" as const,
      name: member.name,
      avatarUrl: member.avatar_url,
      subtitle: tr("群成员", "Group member"),
    })) || []),
  ], [room, currentUserId]);
  const selectedMentions = useMemo(() => mentionOptions.filter((mention) =>
    selectedMentionKeys.includes(`${mention.type}:${mention.id}`)), [mentionOptions, selectedMentionKeys]);
  const currentPresence = presenceError ? undefined : presence;
  const onlineIds = new Set(currentPresence?.online_user_ids || []);
  const filteredMembers = room?.members.filter((member) => member.name.toLocaleLowerCase().includes(memberQuery.trim().toLocaleLowerCase())) || [];
  const otherMember = room?.kind === "dm" ? room.members.find((member) => member.user_id !== currentUserId) : null;
  const { data: pendingInvites = [], isLoading: invitesLoading, error: invitesError } = useQuery({
    queryKey: ["chat-room-invites", roomId],
    queryFn: () => api.chatRooms.pendingInvites(roomId),
    enabled: !!room && detailsOpen && !!(canInvite || canReissueDmInvite),
    refetchInterval: (query) => displayReadInterval(query.state, detailsOpen ? 120_000 : false),
  });
  const pendingInviteCount = pendingInvites.filter((invitation) => invitation.status !== "expired").length;
  const { data: toolApprovals = [], isLoading: approvalsLoading, error: approvalsError } = useQuery({
    queryKey: ["chat-room-tool-approvals", roomId],
    queryFn: () => api.chatRooms.toolApprovals(roomId),
    enabled: !!canReviewAiTools && (detailsOpen || approvalOpen),
    refetchInterval: (query) => approvalOpen ? displayReadInterval(query.state, 120_000) : false,
  });
  const pendingToolApprovals = toolApprovals.filter((approval) => approval.status === "pending");
  const { data: pendingReplyApprovals = [], isLoading: repliesLoading, error: repliesError } = useQuery({
    queryKey: ["chat-room-reply-approvals", roomId],
    queryFn: () => api.chatRooms.replyApprovals(roomId),
    enabled: !!canReviewAiTools,
    refetchInterval: (query) => displayReadInterval(query.state, canReviewAiTools ? 120_000 : false),
  });
  const { data: agentCandidates = [], isLoading: agentsLoading, error: agentsError } = useQuery({
    queryKey: ["chat-room-agent-candidates", roomId],
    queryFn: () => api.chatRooms.agentCandidates(roomId),
    enabled: !!room?.can_manage_agents && agentPickerOpen,
  });
  const addableAgents = agentCandidates.filter((candidate) => !room?.agents.some((agent) => agent.subscription_id === candidate.subscription_id && agent.status === "active"));
  const {
    data: filePages, isLoading: filesLoading, error: filesError,
    hasNextPage, fetchNextPage, isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ["chat-room-files", roomId, debouncedFileQuery],
    queryFn: ({ pageParam }) => api.chatRooms.files(roomId, { before: pageParam || undefined, limit: 50, q: debouncedFileQuery || undefined }),
    initialPageParam: "",
    getNextPageParam: (lastPage) => lastPage.length === 50 ? lastPage[lastPage.length - 1]?.id : undefined,
    enabled: !!room && room.kind === "group" && view === "files",
  });
  const roomFiles = useMemo(() => filePages?.pages.flat() || [], [filePages]);
  const selectedFiles = roomFiles.filter((file) => selectedFileIds.has(file.id));
  const fileSelectionBusy = Boolean(batchDownloadPhase) || Boolean(downloadingFileId) || fileQuery.trim() !== debouncedFileQuery;
  useEffect(() => {
    setSelectedFileIds((current) => {
      const available = new Set(roomFiles.map((file) => file.id));
      const remaining = new Set([...current].filter((id) => available.has(id)));
      return remaining.size === current.size ? current : remaining;
    });
  }, [roomFiles]);

  const loadOlderMessages = async () => {
    const page = anchoredLatestMessages || latestMessages;
    const oldest = olderMessages[0] || page[0];
    if (!oldest || loadingOlderMessages) return;
    setLoadingOlderMessages(true);
    setOlderMessagesError("");
    setAnchoredLatestMessages(page);
    const request = ++historyRequestRef.current;
    try {
      const older = await api.chatRooms.messages(roomId, {
        before: oldest.id, q: searchedMessageQuery || undefined,
      });
      if (request !== historyRequestRef.current) return;
      if (chatBodyRef.current) {
        historyScrollRef.current = {
          top: chatBodyRef.current.scrollTop, height: chatBodyRef.current.scrollHeight,
        };
      }
      setOlderMessages((current) => [...older, ...current]);
      setHasMoreMessages(older.length === 50);
    } catch (cause) { if (request === historyRequestRef.current) setOlderMessagesError(errorText(cause)); }
    finally { if (request === historyRequestRef.current) setLoadingOlderMessages(false); }
  };

  const returnToLatestMessages = () => {
    historyRequestRef.current += 1;
    historyScrollRef.current = null;
    setLoadingOlderMessages(false);
    setOlderMessages([]);
    setAnchoredLatestMessages(null);
    setHasMoreMessages(true);
    setOlderMessagesError("");
    void queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] });
  };

  const send = async (text: string, attachments: AttachedItem[] = []) => {
    if ((!text.trim() && !attachments.length) || !room) return;
    setBusy(true); setError("");
    setSendStatus(attachments.length
      ? { kind: "file", index: 1, total: attachments.length, name: attachments[0].name }
      : { kind: "message" });
    let uploadedFiles = 0;
    try {
      const validIds = selectedMentions.filter((mention) =>
        mention.type === "user" && hasMentionToken(text, mention.name)).map((mention) => mention.id);
      const mentionedAgentBindingIds = selectedMentions.filter((mention) =>
        mention.type === "agent" && hasMentionToken(text, mention.name)).map((mention) => mention.id);
      if (attachments.length > 1 && mentionedAgentBindingIds.length) {
        throw new Error(tr("一次只能附上一份文件并 @AI。", "Mention AI with one attachment at a time."));
      }
      if (attachments.length) {
        if (room.kind !== "group") throw new Error(tr("目前只有群聊支持文件", "Files are currently available in group chats only"));
        for (const item of attachments) {
          setSendStatus({ kind: "file", index: uploadedFiles + 1, total: attachments.length, name: item.name });
          const messageText = uploadedFiles === 0 ? text : "";
          const mentionedUserIds = uploadedFiles === 0 ? validIds : [];
          if (item.type === "knowledge" && item.id) {
            await api.chatRooms.attachKnowledgeFile(roomId, item.id, messageText, mentionedUserIds, uploadedFiles === 0 ? replyingTo?.id : undefined, uploadedFiles === 0 ? mentionedAgentBindingIds : []);
          } else if (item.file) {
            await api.chatRooms.uploadFile(roomId, item.file, messageText, mentionedUserIds, uploadedFiles === 0 ? replyingTo?.id : undefined, uploadedFiles === 0 ? mentionedAgentBindingIds : []);
          } else {
            throw new Error(tr("请重新选择文件", "Please select the file again"));
          }
          uploadedFiles += 1;
          if (uploadedFiles === 1) { setDraft(""); setSelectedMentionKeys([]); }
        }
        await queryClient.invalidateQueries({ queryKey: ["chat-room-files", roomId] });
      } else {
        await api.chatRooms.send(roomId, text, validIds, mentionedAgentBindingIds, replyingTo?.id);
      }
      setRetryAttachments([]);
      setDraft(""); setSelectedMentionKeys([]);
      setReplyingTo(null);
      setMessageQuery("");
      returnToLatestMessages();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] }),
        queryClient.invalidateQueries({ queryKey: ["chat-rooms"] }),
      ]);
    } catch (cause) {
      if (attachments.length > uploadedFiles) {
        setRetryAttachments(attachments.slice(uploadedFiles));
        setRetryAttachmentSeed((seed) => seed + 1);
      }
      setError(errorText(cause));
      if (uploadedFiles) {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] }),
          queryClient.invalidateQueries({ queryKey: ["chat-room-files", roomId] }),
        ]);
      }
    }
    finally { setBusy(false); setSendStatus(null); }
  };

  const reactToMessage = async (message: ChatRoomMessage, emoji: string) => {
    const key = `${message.id}:${emoji}`;
    if (reactingKey) return;
    setReactingKey(key);
    setError("");
    try {
      const reacted = message.reactions?.some((reaction) => reaction.emoji === emoji && reaction.reacted_by_me);
      if (reacted) await api.chatRooms.removeReaction(roomId, message.id, emoji);
      else await api.chatRooms.addReaction(roomId, message.id, emoji);
      await queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] });
    } catch (cause) { setError(errorText(cause)); }
    finally { setReactingKey(null); }
  };

  const retractMessage = async () => {
    if (!messageToRetract || retractingMessageId) return;
    const messageId = messageToRetract.id;
    setRetractingMessageId(messageId); setRetractError("");
    try {
      await api.chatRooms.retractMessage(roomId, messageId);
      if (replyingTo?.id === messageId) setReplyingTo(null);
      const removed = new Set([messageId]);
      setOlderMessages((items) => redactRoomMessages(items, removed));
      setAnchoredLatestMessages((items) => items ? redactRoomMessages(items, removed) : null);
      queryClient.setQueriesData<ChatRoomMessage[]>({ queryKey: ["chat-room-messages", roomId] },
        (items) => items ? redactRoomMessages(items, removed) : items);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] }),
        queryClient.invalidateQueries({ queryKey: ["chat-rooms"] }),
      ]);
      setMessageToRetract(null);
    } catch (cause) { setRetractError(errorText(cause)); }
    finally { setRetractingMessageId(null); }
  };

  const toggleNotifications = async () => {
    if (!room || notificationSettingBusy) return;
    setNotificationSettingBusy(true); setError("");
    try {
      await api.chatRooms.updateNotifications(roomId, !room.notifications_muted);
      await queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] });
    } catch (cause) { setError(errorText(cause)); }
    finally { setNotificationSettingBusy(false); }
  };

  const downloadFile = async (file: ChatRoomFile) => {
    if (singleDownloadAbort.current || batchDownloadAbort.current) return;
    const controller = new AbortController();
    singleDownloadAbort.current = controller;
    setError("");
    setFileActionError(""); setFileDownloadNotice(""); setDownloadingFileId(file.id);
    try {
      const result = await saveRoomFileSelection(file.name,
        (signal) => api.chatRooms.prepareFileDownload(roomId, [file.id], false, signal),
        { signal: controller.signal });
      if (!controller.signal.aborted && result !== "cancelled") setFileDownloadNotice(result === "saved"
        ? tr("文件已保存。", "File saved.")
        : tr("已请求下载，请查看浏览器下载列表。", "Download requested. Check your browser’s downloads."));
    } catch (cause) {
      if (!controller.signal.aborted) {
        if (view === "files") setFileActionError(errorText(cause)); else setError(errorText(cause));
      }
    } finally {
      if (singleDownloadAbort.current === controller) {
        singleDownloadAbort.current = null;
        setDownloadingFileId(null);
      }
    }
  };
  const downloadSelectedFiles = async () => {
    if (!selectedFiles.length || fileSelectionBusy || batchDownloadAbort.current) return;
    setFileActionError(""); setFileDownloadNotice("");
    if (selectedFiles.reduce((total, file) => total + file.size, 0) > ROOM_FILE_BATCH_MAX_BYTES) {
      setFileActionError(tr("单次批量下载最多 5 GiB，请减少所选文件。", "Batch downloads are limited to 5 GiB. Select fewer files."));
      return;
    }
    const controller = new AbortController();
    batchDownloadAbort.current = controller;
    setBatchDownloadPhase("preparing");
    try {
      const name = `${(room?.title || "group").replace(/[\\/:*?"<>|\x00-\x1f]/g, "_")}-files.zip`;
      const result = await saveRoomFileSelection(name,
        (signal) => api.chatRooms.prepareFileDownload(roomId, selectedFiles.map((file) => file.id), true, signal),
        { signal: controller.signal, onTransferStarted: () => {
          if (!controller.signal.aborted) setBatchDownloadPhase("downloading");
        } });
      if (!controller.signal.aborted && result !== "cancelled") setFileDownloadNotice(result === "saved"
        ? tr("文件已保存。", "Files saved.")
        : tr("已请求下载，请查看浏览器下载列表。", "Download requested. Check your browser’s downloads."));
    } catch (cause) {
      if (!controller.signal.aborted) setFileActionError(`${tr("批量下载失败：", "Batch download failed: ")}${errorText(cause)}`);
    } finally {
      if (batchDownloadAbort.current === controller) {
        batchDownloadAbort.current = null;
        setBatchDownloadPhase(null);
      }
    }
  };
  const cancelBatchDownload = () => {
    batchDownloadAbort.current?.abort();
    batchDownloadAbort.current = null;
    setBatchDownloadPhase(null);
  };
  const openFile = async (file: ChatRoomFile) => {
    const request = ++previewRequest.current;
    previewAbort.current?.abort();
    const controller = new AbortController();
    previewAbort.current = controller;
    setPreviewingFileId(file.id);
    setError("");
    setFileActionError("");
    if (file.size > ROOM_FILE_PREVIEW_MAX_BYTES) {
      setSelectedArtifact(roomFileArtifact(roomId, file));
      setPreviewingFileId(null);
      return;
    }
    try {
      const blob = await createFilePreviewSource({
        fetchBinary: (signal) => api.chatRooms.downloadFile(roomId, file.id, { signal }),
      }).getBlob(controller.signal);
      if (request !== previewRequest.current) return;
      setSelectedArtifact(roomFileArtifact(roomId, file, blob));
    } catch (cause) {
      if (request === previewRequest.current) {
        if (view === "files") setFileActionError(errorText(cause));
        else setError(errorText(cause));
      }
    } finally {
      if (request === previewRequest.current) setPreviewingFileId(null);
    }
  };
  const closePreview = () => {
    previewRequest.current += 1;
    previewAbort.current?.abort();
    setSelectedArtifact(null);
    setPreviewingFileId(null);
  };
  const deleteFile = async () => {
    if (!fileToDelete || deletingFileId) return;
    const file = fileToDelete;
    setDeletingFileId(file.id);
    setDeleteFileError("");
    try {
      await api.chatRooms.deleteFile(roomId, file.id);
      if (selectedArtifact?.id === file.id) closePreview();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-room-files", roomId] }),
        queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] }),
      ]);
      setFileToDelete(null);
      setFileActionError("");
    } catch (cause) { setDeleteFileError(errorText(cause)); }
    finally { setDeletingFileId(null); }
  };
  const invite = async (event: FormEvent) => {
    event.preventDefault();
    if (room?.kind === "group" && inviteSender.blocked) return;
    setBusy(true); setError("");
    try {
      const result = await api.chatRooms.invite(roomId, inviteEmail.trim(), room?.kind === "group" ? inviteSender.senderId || undefined : undefined);
      setInviteResult(result);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] }),
        queryClient.invalidateQueries({ queryKey: ["chat-rooms"] }),
        queryClient.invalidateQueries({ queryKey: ["chat-room-invites", roomId] }),
      ]);
    }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const cancelInvite = async (inviteId: string) => {
    setCancellingInviteId(inviteId); setError("");
    try {
      await api.chatRooms.cancelInvite(roomId, inviteId);
      await queryClient.invalidateQueries({ queryKey: ["chat-room-invites", roomId] });
    } catch (cause) { setError(errorText(cause)); }
    finally { setCancellingInviteId(null); }
  };
  const remove = async (userId: string) => {
    if (!window.confirm(tr("移除该成员？移除后将无法继续查看群消息。", "Remove this member? They will no longer be able to view group messages."))) return;
    setBusy(true); setError("");
    try {
      await api.chatRooms.removeMember(roomId, userId);
      await Promise.all([queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] }), queryClient.invalidateQueries({ queryKey: ["chat-rooms"] })]);
      setMemberMenuId(null);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const updateRole = async (userId: string, role: "admin" | "member") => {
    setBusy(true); setError("");
    try {
      await api.chatRooms.updateMemberRole(roomId, userId, role);
      await queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] });
      setMemberMenuId(null);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const rename = async (event: FormEvent) => {
    event.preventDefault();
    const title = renameTitle.trim();
    if (!title || title === room?.title) {
      if (title === room?.title) setRenameOpen(false);
      return;
    }
    setBusy(true); setError("");
    try {
      await api.chatRooms.rename(roomId, title);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] }),
        queryClient.invalidateQueries({ queryKey: ["chat-rooms"] }),
      ]);
      setRenameOpen(false);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const closeGroup = async () => {
    setBusy(true); setError("");
    try {
      await api.chatRooms.close(roomId);
      await queryClient.invalidateQueries({ queryKey: ["chat-rooms"] });
      setCloseOpen(false);
      navigate("/chat", { replace: true });
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const leaveGroup = async () => {
    setBusy(true); setError("");
    try {
      await api.chatRooms.leave(roomId);
      await queryClient.invalidateQueries({ queryKey: ["chat-rooms"] });
      setLeaveOpen(false);
      navigate("/chat", { replace: true });
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const bindAgent = async (subscriptionId: string) => {
    setBusy(true); setError("");
    try {
      await api.chatRooms.bindAgent(roomId, subscriptionId);
      await queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] });
      setAgentPickerOpen(false);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const removeAgent = async (bindingId: string) => {
    setBusy(true); setError("");
    try {
      await api.chatRooms.removeAgent(roomId, bindingId);
      await queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] });
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const toggleAiExecution = async () => {
    if (!room?.can_manage_ai_execution || aiSettingBusy) return;
    setAiSettingBusy(true); setError("");
    try {
      await api.chatRooms.updateAiExecution(roomId, !room.ai_tool_execution_enabled, room.ai_tool_execution_revision);
      await queryClient.invalidateQueries({ queryKey: ["chat-room", roomId] });
    } catch (cause) { setError(errorText(cause)); }
    finally { setAiSettingBusy(false); }
  };
  const decideAiTool = async (approvalId: string, approve: boolean) => {
    setApprovalDecisionId(approvalId); setApprovalError("");
    try {
      await api.chatRooms.decideToolApproval(roomId, approvalId, approve);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-room-tool-approvals", roomId] }),
        queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] }),
      ]);
    } catch (cause) {
      setApprovalError(errorText(cause));
      await queryClient.invalidateQueries({ queryKey: ["chat-room-tool-approvals", roomId] });
    } finally { setApprovalDecisionId(null); }
  };
  const decideAiReply = async (runId: string, digest: string, approve: boolean) => {
    setApprovalDecisionId(runId); setApprovalError("");
    try {
      await api.chatRooms.decideReplyApproval(roomId, runId, digest, approve);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["chat-room-reply-approvals", roomId] }),
        queryClient.invalidateQueries({ queryKey: ["chat-room-messages", roomId] }),
      ]);
    } catch (cause) {
      setApprovalError(errorText(cause));
      await queryClient.invalidateQueries({ queryKey: ["chat-room-reply-approvals", roomId] });
    } finally { setApprovalDecisionId(null); }
  };

  const renderMemberRow = (member: ChatRoom["members"][number]) => {
    const canManageMember = room?.kind === "group" && member.role !== "owner" && (canManageRoles || (canManageGroup && member.role === "member"));
    return <div role="listitem" key={member.user_id} className="rounded-lg px-2 py-1.5 hover:bg-[var(--surface-muted)]">
      <div className="flex min-h-7 items-center gap-2">
        <UserAvatar name={member.name} avatarUrl={member.avatar_url} size={28} />
        <span className="min-w-0 flex-1 truncate text-[var(--text-default)]" title={member.name}>{member.name}</span>
        {member.role !== "member" && <span className="shrink-0 text-xs text-[var(--text-muted)]">{member.role === "owner" ? tr("群主", "Owner") : tr("管理员", "Admin")}</span>}
        {currentPresence && <span role="img" aria-label={`${member.name} · ${onlineIds.has(member.user_id) ? tr("在线", "Online") : tr("离线", "Offline")}`} title={onlineIds.has(member.user_id) ? tr("在线", "Online") : tr("离线", "Offline")}><span aria-hidden="true" className={onlineIds.has(member.user_id) ? "chat-status-dot chat-status-dot--online" : "chat-status-dot bg-[var(--text-faint)]"} /></span>}
        {canManageMember && <button type="button" aria-expanded={memberMenuId === member.user_id} aria-label={`${tr("管理成员", "Manage member")} ${member.name}`} title={tr("管理成员", "Manage member")} onClick={() => setMemberMenuId((id) => id === member.user_id ? null : member.user_id)} className="rounded p-1 text-[var(--text-muted)] hover:bg-[var(--surface-sunken)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"><IconMoreHorizontal size={16} /></button>}
      </div>
      {canManageMember && memberMenuId === member.user_id && <div className="mt-1 flex flex-wrap items-center justify-end gap-3 pl-9">
        {canManageRoles && <button type="button" disabled={busy} className="rounded text-xs text-[var(--text-default)] hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50" onClick={() => void updateRole(member.user_id, member.role === "admin" ? "member" : "admin")}>{member.role === "admin" ? tr("撤销管理员", "Remove admin") : tr("设为管理员", "Make admin")}</button>}
        <button type="button" disabled={busy} className="rounded text-xs text-[var(--text-muted)] hover:text-[var(--editor-danger-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50" onClick={() => void remove(member.user_id)} aria-label={`${tr("移除", "Remove")} ${member.name}`}>{tr("移除成员", "Remove member")}</button>
      </div>}
    </div>;
  };

  const composerTopSlot = (error || replyingTo || sendStatus) ? <div className="space-y-2">
    {error && <p role="alert" className="m-0 text-sm text-red-700">{error}</p>}
    {sendStatus && <div role="status" aria-live="polite" className="flex min-w-0 items-center gap-2 rounded-lg bg-[var(--surface-muted)] px-3 py-2 text-xs text-[var(--text-default)]">
      <span aria-hidden="true" className="h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--text-muted)] motion-safe:animate-pulse" />
      <span className="min-w-0 truncate">{sendStatus.kind === "file"
        ? `${tr("上传中", "Uploading")} ${sendStatus.index}/${sendStatus.total} · ${sendStatus.name}`
        : tr("发送中…", "Sending…")}</span>
    </div>}
    {replyingTo && <div className="flex items-center gap-2 rounded-lg bg-[var(--surface-muted)] px-3 py-2 text-xs text-[var(--text-default)]"><IconReply size={14} /><span className="min-w-0 flex-1 truncate">{tr("回复", "Replying to")} {replyingTo.sender_agent_name || replyingTo.sender_name}: {replyingTo.content || replyingTo.attachments[0]?.name || tr("附件", "Attachment")}</span><button type="button" onClick={() => setReplyingTo(null)} aria-label={tr("取消回复", "Cancel reply")} className="rounded px-1 text-[var(--text-muted)] hover:bg-[var(--surface-sunken)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]">×</button></div>}
  </div> : undefined;

  if (isLoading) return <div className="flex flex-1 items-center justify-center text-sm text-[var(--text-muted)]">{tr("加载对话…", "Loading conversation…")}</div>;
  if (!room || roomError) return <div role="alert" className="flex flex-1 items-center justify-center text-sm text-[var(--text-muted)]">{tr("无法打开此对话，请确认你仍是成员。", "This conversation is unavailable. Check that you are still a member.")}</div>;
  return <div className="flex min-h-0 flex-1 flex-col bg-[var(--chat-surface)]">
    <div className={`embedded-chat-workbench chat-room-workbench ${selectedArtifact ? "embedded-chat-workbench--output-open" : ""}`}>
      <ResizablePaneGroup storageKey={`room-artifact-panes:${roomId}`} panes={[{
        id: "chat", label: room.title, initialSize: 2, minSize: selectedArtifact ? 360 : undefined,
        className: "embedded-chat-pane embedded-chat-pane--chat",
        children: <div className="embedded-chat-column">
    <header className="embedded-chat-header embedded-chat-header--workspace">
      <div className="workspace-chat-header-main">
        {room.kind === "group" ? <GroupChatAvatar title={room.title} members={room.members} currentUserId={currentUserId} size={36} /> : <UserAvatar name={otherMember?.name || room.title} avatarUrl={otherMember?.avatar_url} size={36} />}
        <div className="min-w-0">
          <h1 className="workspace-chat-title m-0 truncate text-sm font-semibold">{room.title}</h1>
          <p className="workspace-chat-subtitle m-0 flex items-center gap-1.5 truncate">
            <span>{room.kind === "group" ? `${room.member_count} ${tr("位成员", room.member_count === 1 ? "member" : "members")}` : room.member_count < 2 ? tr("等待加入", "Awaiting acceptance") : tr("私聊", "Direct message")}</span>
            {currentPresence && room.kind === "group" && <span>· {onlineIds.size} {tr("人在线", "online")}</span>}
            {currentPresence && otherMember && <><span>·</span><PresenceStatus online={onlineIds.has(otherMember.user_id)} /></>}
            {presenceError && <span>· {tr("在线状态暂不可用", "Presence unavailable")}</span>}
          </p>
        </div>
      </div>
      <div className="workspace-chat-header-actions">
        <AnchoredPopover ariaLabel={room.kind === "group" ? tr("群聊详情", "Group details") : tr("私聊详情", "Direct message details")} open={detailsOpen} width={360} align="right" onOpenChange={(open) => { setDetailsOpen(open); if (open) { setError(""); setDetailsTab("members"); } else { setInvitesExpanded(false); setAgentPickerOpen(false); setMemberMenuId(null); } }} trigger={<Button type="button" variant="ghost" className="workspace-chat-lifecycle-trigger" ariaLabel={room.kind === "group" ? tr("群聊详情", "Group details") : tr("私聊详情", "Direct message details")} title={room.kind === "group" ? tr("群聊详情", "Group details") : tr("私聊详情", "Direct message details")}><IconMoreHorizontal size={18} /></Button>}>
          {({ close }) => <div className="max-h-[min(76vh,600px)] overflow-y-auto p-4 font-sans text-[13px] leading-[1.55]">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="m-0 truncate font-semibold text-[var(--text-strong)]">{room.kind === "group" ? room.title : tr("私聊详情", "Direct message details")}</h2>
                <p className="m-0 mt-0.5 text-xs text-[var(--text-muted)]">{room.kind === "group" ? room.owner_scope === "entity" ? tr("公司群聊", "Company group") : tr("个人群聊", "Personal group") : tr("仅对话成员可见", "Visible only to participants")}</p>
              </div>
              {canManageGroup && <button type="button" className="shrink-0 rounded px-1 py-0.5 text-xs text-[var(--text-default)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]" onClick={() => { close(); setRenameTitle(room.title); setRenameOpen(true); setError(""); }}>{tr("改名", "Rename")}</button>}
            </div>
            {error && <p role="alert" className="m-0 mt-3 text-xs text-red-700">{error}</p>}
            {room.kind === "group" && <TabSwitcher appearance="minimal" size="sm" className="mt-4" ariaLabel={tr("群聊详情分类", "Group details sections")} tabs={[{ key: "members", label: tr("成员", "Members") }, { key: "ai", label: "AI" }, { key: "settings", label: tr("设置", "Settings") }]} value={detailsTab} onChange={setDetailsTab} />}
            {(room.kind !== "group" || detailsTab === "members") && <section aria-label={tr("成员和邀请", "People and invitations")} className="mt-4">
              <div className="mb-1 flex items-center justify-between gap-2"><h3 className="m-0 text-xs font-semibold text-[var(--text-muted)]">{tr("成员", "Members")} · {room.member_count}</h3>{(canInvite || canReissueDmInvite) && <Button type="button" size="sm" variant="outline" onClick={() => { close(); setInviteOpen(true); setInviteResult(null); setInviteEmail(canReissueDmInvite ? room.title : ""); setError(""); }}>{tr("邀请", "Invite")}</Button>}</div>
              <div role="list" className="space-y-0.5">{room.members.slice(0, 4).map(renderMemberRow)}</div>
              {room.member_count > 4 && <button type="button" className="mt-1 w-full rounded-lg px-2 py-2 text-left text-xs text-[var(--text-default)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]" onClick={() => { close(); setMemberQuery(""); setMembersOpen(true); }}>{tr(`查看全部 ${room.member_count} 位成员`, `View all ${room.member_count} members`)}</button>}
              {(canInvite || canReissueDmInvite) && <div className="mt-2" aria-label={tr("邀请状态", "Invitation status")}>
              <button type="button" aria-expanded={invitesExpanded} aria-controls={invitesExpanded ? `room-invitations-${roomId}` : undefined} onClick={() => setInvitesExpanded((expanded) => !expanded)} className="flex w-full items-center justify-between gap-2 rounded-lg bg-[var(--surface-muted)] px-2 py-2 text-left text-xs font-medium text-[var(--text-default)] hover:bg-[var(--surface-sunken)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]">
                <span>{canManageGroup ? tr("邀请", "Invitations") : tr("我发出的邀请", "My invitations")}</span>
                <span className="flex shrink-0 items-center gap-1.5 font-normal text-[var(--text-muted)]">{invitesError ? tr("暂不可用", "Unavailable") : invitesLoading ? tr("加载中…", "Loading…") : `${pendingInviteCount} ${tr("待接受", "pending")}`}<IconChevronDown size={14} className={`transition-transform ${invitesExpanded ? "rotate-180" : ""}`} /></span>
              </button>
              {invitesExpanded && <div id={`room-invitations-${roomId}`} className="mt-1 max-h-44 space-y-1 overflow-y-auto">
                {invitesLoading && <p role="status" className="m-0 px-2 text-xs text-[var(--text-muted)]">{tr("正在加载…", "Loading…")}</p>}
                {invitesError && <p role="alert" className="m-0 px-2 text-xs text-red-700">{tr("邀请状态暂不可用", "Invitation status unavailable")}</p>}
                {!invitesLoading && !invitesError && pendingInvites.length === 0 && <p className="m-0 px-2 py-1 text-xs text-[var(--text-muted)]">{tr("暂无待处理邀请", "No outstanding invitations")}</p>}
                {pendingInvites.map((invitation) => <div key={invitation.id} className="flex items-center gap-2 rounded-lg px-2 py-1.5">
                  <span className="min-w-0 flex-1 truncate text-xs text-[var(--text-default)]" title={invitation.email}>{invitation.email}</span>
                  <span className="shrink-0 text-xs text-[var(--text-muted)]">{invitation.status === "expired" ? tr("已过期", "Expired") : tr("待接受", "Pending")}</span>
                  <button type="button" className="shrink-0 rounded px-1 text-xs text-[var(--text-default)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]" onClick={() => { close(); setInviteOpen(true); setInviteResult(null); setInviteEmail(invitation.email); setError(""); }}>{tr("新链接", "New link")}</button>
                  {room.kind === "group" && invitation.status === "pending" && <button type="button" disabled={cancellingInviteId === invitation.id} className="shrink-0 rounded px-1 text-xs text-[var(--editor-danger-text)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50" onClick={() => void cancelInvite(invitation.id)}>{cancellingInviteId === invitation.id ? tr("取消中…", "Canceling…") : tr("取消邀请", "Cancel invite")}</button>}
                </div>)}
              </div>}
              </div>}
            </section>}
            {room.kind === "group" && detailsTab === "ai" && <section aria-label={tr("群内 AI", "AI in this group")} className="mt-4">
              <div className="mb-1 flex items-center justify-between"><h3 className="m-0 text-xs font-semibold text-[var(--text-muted)]">AI · {visibleAgents.length}</h3>{room.can_manage_agents && <Button type="button" size="sm" variant="outline" ariaExpanded={agentPickerOpen} onClick={() => setAgentPickerOpen((open) => !open)}>{agentPickerOpen ? tr("收起", "Close") : tr("添加 AI", "Add AI")}</Button>}</div>
              <div className="space-y-0.5">{visibleAgents.map((agent) => <div key={agent.id} className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-[var(--surface-muted)]"><UserAvatar type={agent.is_default ? "manor" : "agent"} name={agent.name} seed={agent.id} size={28} /><span className="min-w-0 flex-1"><span className="block truncate text-[var(--text-default)]">{agent.name}</span><span className="block truncate text-xs text-[var(--text-muted)]">{agent.is_default ? tr("仅本群", "This group only") : agent.workspace_name ? `${tr("Workspace", "Workspace")} · ${agent.workspace_name}` : tr("个人 Agent", "Personal Agent")}</span></span>{!agent.available && <span className="text-xs text-[var(--text-muted)]">{tr("不可用", "Unavailable")}</span>}{room.can_manage_agents && !agent.is_default && <button type="button" disabled={busy} onClick={() => void removeAgent(agent.id)} aria-label={`${tr("移除", "Remove")} ${agent.name}`} className="rounded px-1 text-xs text-[var(--text-muted)] hover:text-[var(--editor-danger-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]">{tr("移除", "Remove")}</button>}</div>)}</div>
              {agentPickerOpen && <div className="mt-2 rounded-xl bg-[var(--surface-muted)] p-2">
                {agentsLoading && <p role="status" className="m-0 px-2 py-1 text-xs text-[var(--text-muted)]">{tr("正在加载 AI…", "Loading AI…")}</p>}
                {agentsError && <p role="alert" className="m-0 px-2 py-1 text-xs text-red-700">{tr("无法加载可添加的 Agent", "Available Agents could not be loaded")}</p>}
                {!agentsLoading && !agentsError && addableAgents.length === 0 && <p className="m-0 px-2 py-1 text-xs text-[var(--text-muted)]">{tr("暂无可添加的 Agent。", "No Agents available to add. ")}<Link to="/agents" className="underline underline-offset-2 hover:text-[var(--text-default)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]">{tr("管理 Agent", "Manage Agents")}</Link></p>}
                {addableAgents.map((candidate) => <button key={candidate.subscription_id} type="button" disabled={busy} onClick={() => void bindAgent(candidate.subscription_id)} className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-[var(--surface-panel)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50"><UserAvatar type="agent" name={candidate.name} seed={candidate.subscription_id} size={28} /><span className="min-w-0 flex-1"><span className="block truncate text-[var(--text-strong)]">{candidate.name}</span><span className="block truncate text-xs text-[var(--text-muted)]">{candidate.workspace_name ? `${tr("Workspace", "Workspace")} · ${candidate.workspace_name}` : tr("个人 Agent", "Personal Agent")}</span></span><span className="text-xs text-[var(--text-muted)]">{tr("添加", "Add")}</span></button>)}
              </div>}
              <div className="mt-2 flex items-center justify-between gap-3 rounded-lg bg-[var(--surface-muted)] px-2 py-2">
                <div className="min-w-0">
                  <span className="block text-[var(--text-default)]">{tr("AI 处理群文件", "AI group file access")}</span>
                  <span className="block text-xs text-[var(--text-muted)]">{tr("授权使用群文件；审批方式在下方统一管理。", "Authorize group file access; manage approvals below.")}</span>
                  {aiSettingBusy && <span role="status" className="block text-xs text-[var(--text-muted)]">{tr("保存中…", "Saving…")}</span>}
                </div>
                {room.can_manage_ai_execution
                  ? <Toggle checked={room.ai_tool_execution_enabled} onChange={() => void toggleAiExecution()} disabled={aiSettingBusy} size="sm" aria-label={tr("允许 AI 读写本群文件", "Allow AI to read and manage this group's files")} />
                  : <span className="shrink-0 text-xs text-[var(--text-muted)]">{room.ai_tool_execution_enabled ? tr("已开启", "On") : tr("已关闭", "Off")}</span>}
              </div>
              <div className="mt-3"><ApprovalModeTrigger scope={{ kind: "room", id: roomId }} compact={false} /></div>
              {canReviewAiTools && pendingToolApprovals.length + pendingReplyApprovals.length > 0 && <Button type="button" size="sm" variant="ghost" onClick={() => { close(); setApprovalError(""); setApprovalOpen(true); }}>{tr("待处理请求", "Pending requests")} · {pendingToolApprovals.length + pendingReplyApprovals.length}</Button>}
            </section>}
            {(room.kind !== "group" || detailsTab === "settings") && <section aria-label={tr("通知", "Notifications")} className="mt-4">
              <h3 className="m-0 mb-1 text-xs font-semibold text-[var(--text-muted)]">{tr("通知", "Notifications")}</h3>
              <div className="flex items-center justify-between gap-3 rounded-lg bg-[var(--surface-muted)] px-2 py-2">
                <span className="min-w-0 text-[var(--text-default)]">{tr("提及和回复", "Mentions and replies")}</span>
                <Toggle checked={!room.notifications_muted} onChange={() => void toggleNotifications()} disabled={notificationSettingBusy} size="sm" aria-label={tr("群聊消息提醒", "Chat notifications")} />
              </div>
            </section>}
            {room.kind === "group" && detailsTab === "settings" && <section aria-label={tr("群聊管理", "Group management")} className="mt-5 pt-1">
              <h3 className="m-0 mb-1 text-xs font-semibold text-[var(--text-muted)]">{tr("管理", "Management")}</h3>
              {currentMember?.role === "owner"
                ? <button type="button" className="w-full rounded-lg px-2 py-2 text-left text-xs text-[var(--editor-danger-text)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]" onClick={() => { close(); setError(""); setCloseOpen(true); }}>{tr("解散群聊", "Close group")}</button>
                : <button type="button" className="w-full rounded-lg px-2 py-2 text-left text-xs text-[var(--editor-danger-text)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]" onClick={() => { close(); setError(""); setLeaveOpen(true); }}>{tr("退出群聊", "Leave group")}</button>}
            </section>}
          </div>}
        </AnchoredPopover>
      </div>
    </header>
    {room.kind === "group" && <div className="flex min-w-0 items-center gap-3 px-5 py-1.5 sm:px-6">
      <div className="shrink-0"><TabSwitcher appearance="minimal" size="sm" ariaLabel={tr("群聊视图", "Group chat views")} tabs={[{ key: "messages", label: tr("消息", "Messages") }, { key: "files", label: tr("文件", "Files") }]} value={view} onChange={(next) => { setView(next === "files" ? "files" : "messages"); setFileActionError(""); }} /></div>
      <Input appearance="quiet" ariaLabel={view === "messages" ? tr("搜索群消息", "Search group messages") : tr("搜索群文件", "Search group files")} value={view === "messages" ? messageQuery : fileQuery} onChange={(event) => {
        if (view === "files") { setFileQuery(event.target.value); setSelectedFileIds(new Set()); setFileActionError(""); return; }
        historyRequestRef.current += 1;
        setLoadingOlderMessages(false);
        setMessageQuery(event.target.value);
        setOlderMessages([]);
        setAnchoredLatestMessages(null);
        setHasMoreMessages(true);
        setOlderMessagesError("");
      }} disabled={view === "files" && Boolean(batchDownloadPhase)} maxLength={100} placeholder={view === "messages" ? tr("搜索消息", "Search messages") : tr("搜索文件", "Search files")} className="ml-auto min-w-0 w-56" />
    </div>}
    {room.kind === "group" && view === "files" ? <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-8 pt-5 sm:px-8">
      <div className="mx-auto w-full max-w-3xl space-y-4">
        <div><h2 className="m-0 text-base font-semibold text-[var(--text-strong)]">{tr("群文件", "Group files")}</h2><p className="m-0 mt-0.5 text-xs text-[var(--text-muted)]">{tr("仅显示本群聊天中分享的文件", "Only files shared in this group chat")}</p></div>
        {roomFiles.length > 0 && <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
          <Checkbox checked={selectedFiles.length === roomFiles.length} disabled={fileSelectionBusy} label={tr("全选已加载文件", "Select loaded files")} onChange={(checked) => setSelectedFileIds(new Set(checked ? roomFiles.map((file) => file.id) : []))} style={{ minHeight: 32 }} />
          <Button type="button" variant="outline" size="sm" disabled={!selectedFiles.length || fileSelectionBusy} loading={Boolean(batchDownloadPhase)} onClick={() => void downloadSelectedFiles()}><IconDownload size={14} />{batchDownloadPhase ? tr("准备下载中…", "Preparing download…") : `${tr("下载所选文件", "Download selected")} (${selectedFiles.length})`}</Button>
        </div>}
        {batchDownloadPhase && <div className="flex flex-wrap items-center gap-3 text-xs text-[var(--text-muted)]">
          <span role="status">{batchDownloadPhase === "downloading" ? tr("下载中…", "Downloading…") : tr("准备下载中…", "Preparing download…")}</span>
          <Button type="button" variant="ghost" size="sm" onClick={cancelBatchDownload}>{tr("取消", "Cancel")}</Button>
        </div>}
        {fileDownloadNotice && <p role="status" className="m-0 text-xs text-[var(--text-muted)]">{fileDownloadNotice}</p>}
        {fileActionError && <p role="alert" className="m-0 text-sm text-red-700">{fileActionError}</p>}
        {filesError && <p role="alert" className="m-0 text-sm text-red-700">{tr("无法加载群文件，请重试。", "Group files could not be loaded. Please retry.")}</p>}
        {filesLoading && <p role="status" className="m-0 py-5 text-sm text-[var(--text-muted)]">{tr("正在加载文件…", "Loading files…")}</p>}
        {!filesLoading && !filesError && roomFiles.length === 0 && <EmptyState icon={<IconPaperclip size={24} />} title={fileQuery ? tr("没有匹配的文件", "No matching files") : tr("还没有群文件", "No files yet")} description={fileQuery ? tr("试试其他文件名。", "Try another filename.") : tr("在消息中发送文件后，会显示在这里。Workspace 文件不会自动共享给群成员。", "Files sent in messages appear here. Workspace files are not shared automatically.")} />}
        {roomFiles.length > 0 && <div role="list" className="divide-y divide-[var(--border-subtle)]">{roomFiles.map((file) => <div role="listitem" key={file.id} className="flex min-w-0 items-center gap-3 rounded-lg px-2 py-3 hover:bg-[var(--surface-muted)]">
          <Checkbox checked={selectedFileIds.has(file.id)} disabled={fileSelectionBusy} aria-label={`${tr("选择", "Select")} ${file.name}`} style={{ minWidth: 24, minHeight: 36 }} onChange={(checked) => setSelectedFileIds((current) => {
            const next = new Set(current);
            if (checked) next.add(file.id); else next.delete(file.id);
            return next;
          })} />
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--surface-sunken)] text-[var(--text-muted)]" aria-hidden="true"><RoomFileTypeIcon file={file} /></span>
          <button type="button" disabled={previewingFileId === file.id} onClick={() => void openFile(file)} className="min-w-0 flex-1 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50" aria-label={`${tr("预览", "Preview")} ${file.name}`}><span className="block truncate text-sm font-medium text-[var(--text-strong)]" title={file.name}>{file.name}</span><span className="block truncate text-xs text-[var(--text-muted)]">{file.sender_name} · {fileSize(file.size)} · <time dateTime={file.created_at} title={formatChatDateTime(file.created_at)}>{new Date(file.created_at).toLocaleDateString(undefined, { timeZone: getPreferredTimeZone() })}</time></span></button>
          <button type="button" disabled={Boolean(downloadingFileId) || Boolean(batchDownloadPhase)} onClick={() => void downloadFile(file)} aria-label={`${tr("下载", "Download")} ${file.name}`} title={tr("下载", "Download")} className="shrink-0 rounded-lg p-2 text-[var(--text-muted)] hover:bg-[var(--surface-sunken)] hover:text-[var(--text-strong)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50"><IconDownload size={18} /></button>
          {file.can_delete && <button type="button" disabled={Boolean(batchDownloadPhase)} onClick={() => { setFileToDelete(file); setDeleteFileError(""); }} aria-label={`${tr("删除", "Delete")} ${file.name}`} title={tr("删除", "Delete")} className="shrink-0 rounded-lg p-2 text-[var(--text-muted)] hover:bg-[var(--surface-sunken)] hover:text-[var(--editor-danger-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50"><IconTrash size={18} /></button>}
        </div>)}</div>}
        {hasNextPage && <Button type="button" size="sm" variant="outline" disabled={isFetchingNextPage} onClick={() => void fetchNextPage()}>{isFetchingNextPage ? tr("加载中…", "Loading…") : tr("查看更多", "Load more")}</Button>}
      </div>
    </div> : <>
    <div className="embedded-chat-body-wrap workspace-chat-body-wrap">
      <div ref={chatBodyRef} className={`embedded-chat-body ${messages.length === 0 ? "embedded-chat-body--empty" : ""}`} aria-live="polite">
        <div ref={chatContentRef} className="min-h-full">
        {anchoredLatestMessages && <Button type="button" size="sm" variant="outline" className="self-center" onClick={returnToLatestMessages}>{tr("返回最新消息", "Back to latest")}</Button>}
        {hasMoreMessages && (anchoredLatestMessages || latestMessages).length >= 50 && <Button type="button" size="sm" variant="outline" className="self-center" disabled={loadingOlderMessages} onClick={() => void loadOlderMessages()}>{loadingOlderMessages ? tr("加载中…", "Loading…") : tr("查看更早消息", "Load earlier messages")}</Button>}
        {olderMessagesError && <p role="alert" className="text-sm text-red-700">{olderMessagesError}</p>}
        {messagesError && <p role="alert" className="text-sm text-red-700">{tr("消息加载失败", "Messages failed to load")}</p>}
        {messages.length === 0 && !messagesError && <EmptyState className="mx-auto h-full" icon={room.kind === "group" ? <IconUsers size={24} /> : <UserAvatar name={room.title} avatarUrl={otherMember?.avatar_url} size={36} />} title={searchedMessageQuery ? tr("没有匹配的消息", "No matching messages") : room.kind === "group" ? tr("从这里开始群聊", "Start the group conversation") : tr("开始私聊", "Start your conversation")} description={searchedMessageQuery ? tr("试试其他关键词。", "Try another search term.") : room.kind === "group" ? tr("新成员只能看到加入后的消息。", "New members only see messages sent after joining.") : tr("只有你和对方能看到这段对话。", "Only you and the other person can see this conversation.")} action={!searchedMessageQuery && canInvite ? <Button type="button" size="sm" variant="outline" onClick={() => { setInviteOpen(true); setInviteResult(null); setInviteEmail(""); setError(""); }}>{tr("邀请其他人", "Invite someone")}</Button> : undefined} />}
        {displayMessages.map((message) => {
          const own = message.sender_user_id === currentUserId;
          const agentBinding = room.agents.find((agent) => agent.id === message.sender_agent_binding_id);
          const senderName = message.sender_agent_name || message.sender_name;
          const repliedTo = message.reply_to_message_id ? messages.find((item) => item.id === message.reply_to_message_id) : undefined;
          const activeAgentRuns = (message.agent_runs || []).filter((run) => run.status !== "succeeded");
          const runStatusLabel = message.agent_run_status === "pending" || message.agent_run_status === "running"
            ? tr("AI 正在回复…", "AI is replying…")
            : message.agent_run_status === "failed"
              ? tr("AI 回复失败", "AI could not reply")
              : message.agent_run_status === "interrupted"
                ? tr("AI 回复已停止", "AI reply stopped")
                : null;
          const replyPreview = message.reply_to || (repliedTo ? { id: repliedTo.id, sender_name: repliedTo.sender_agent_name || repliedTo.sender_name, content: repliedTo.content } : null);
          const reactionControls = <span className="inline-flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 max-sm:opacity-100">
            <ChatActionButton onClick={() => setReplyingTo(message)} aria-label={`${tr("回复", "Reply to")} ${senderName}`} title={tr("回复这条消息", "Reply to this message")}><IconReply size={13} /></ChatActionButton>
            {own && !message.sender_agent_binding_id && <ChatActionButton onClick={() => { setMessageToRetract(message); setRetractError(""); }} aria-label={tr("撤回消息", "Retract message")} title={tr("撤回这条消息", "Retract this message")}><IconTrash size={13} /></ChatActionButton>}
            {!own && <AnchoredPopover ariaLabel={tr("选择表情", "Choose reaction")} width={320} panelClassName="room-emoji-popover" trigger={<ChatActionButton disabled={Boolean(reactingKey)} aria-label={`${tr("回应", "React to")} ${senderName}`} title={tr("添加表情回应", "Add reaction")}><IconSmilePlus size={13} /></ChatActionButton>}>
              {({ close }) => <Suspense fallback={<div className="p-4 text-sm text-[var(--text-muted)]">{tr("正在加载表情…", "Loading emoji…")}</div>}><RoomEmojiPicker onSelect={(emoji) => { close(); void reactToMessage(message, emoji); }} /></Suspense>}
            </AnchoredPopover>}
          </span>;
          return <Fragment key={message.id}><div id={`room-message-${message.id}`} className={`group chat-message-row chat-message-shell ${own ? "chat-message-row--user" : ""}`}>
            <UserAvatar name={senderName} type={message.sender_agent_binding_id ? agentBinding?.is_default ? "manor" : "agent" : "user"} avatarUrl={message.sender_agent_binding_id ? message.sender_agent_avatar_url : message.sender_user_avatar_url} seed={message.sender_agent_binding_id || undefined} size={32} />
            <div className={`chat-message-col ${own ? "chat-message-col--user" : ""}`}>
              <span className={`chat-sender-name ${own ? "chat-sender-name--user" : ""}`}>{senderName}</span>
              <MessageBubble role={own ? "user" : "other"} className={`chat-bubble ${own ? "chat-bubble--user" : "chat-bubble--bot"}`}>
                {message.reply_to_message_id && <button type="button" disabled={!repliedTo} onClick={() => document.getElementById(`room-message-${message.reply_to_message_id}`)?.scrollIntoView({ block: "center" })} title={replyPreview ? `${replyPreview.sender_name}: ${replyPreview.content}` : tr("原消息不可见", "Original message unavailable")} className="room-message-reply-quote mb-1 block max-w-64 truncate rounded px-1 text-left text-xs text-[var(--text-muted)] hover:bg-[var(--surface-muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:cursor-default">↳ {replyPreview ? `${replyPreview.sender_name}: ${replyPreview.content || tr("附件", "Attachment")}` : tr("原消息不可见", "Original message unavailable")}</button>}
                {message.deleted_at && <span className="text-sm italic text-[var(--text-muted)]">{tr("消息已撤回", "Message removed")}</span>}
                {!message.deleted_at && message.content && (message.sender_agent_binding_id
                  ? <AssistantMessageBlocks blocks={message.assistant_blocks} content={message.content} keyPrefix={message.id} sourceMessageId={message.id} minimal enableFileCards={false} />
                  : <MessageContent message={message} room={room} />)}
                {!message.deleted_at && !message.content && runStatusLabel && message.sender_agent_binding_id && <span role="status" className="text-sm text-[var(--text-muted)]">{runStatusLabel}</span>}
                {(message.attachments || []).filter((file) => !message.sender_agent_binding_id || /^(image|video|audio)\//.test(file.mime_type)).map((file) => <RoomMessageAttachment key={file.id} roomId={roomId} file={file} onOpen={(item) => void openFile(item)} />)}
              </MessageBubble>
              {message.sender_agent_binding_id && message.attachments.some((file) => !/^(image|video|audio)\//.test(file.mime_type)) && <ArtifactSummaryCards artifacts={message.attachments.filter((file) => !/^(image|video|audio)\//.test(file.mime_type)).map((file) => roomFileArtifact(roomId, file))} onOpen={(artifact) => {
                const file = message.attachments.find((item) => item.id === artifact.id);
                if (file) void openFile(file);
              }} />}
              {!message.deleted_at && (message.reactions || []).length > 0 && <div className="mt-1 flex flex-wrap gap-1">{message.reactions?.map((reaction) => own ? <span key={reaction.emoji} className="rounded-full bg-[var(--surface-muted)] px-2 py-0.5 text-xs text-[var(--text-default)]">{reaction.emoji} {reaction.count}</span> : <button key={reaction.emoji} type="button" disabled={Boolean(reactingKey)} onClick={() => void reactToMessage(message, reaction.emoji)} aria-label={`${reaction.reacted_by_me ? tr("移除", "Remove") : tr("添加", "Add")} ${reaction.emoji} · ${reaction.count}`} aria-pressed={reaction.reacted_by_me} className="rounded-full bg-[var(--surface-muted)] px-2 py-0.5 text-xs text-[var(--text-default)] hover:bg-[var(--surface-sunken)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)] disabled:opacity-50">{reaction.emoji} {reaction.count}</button>)}</div>}
              {message.sender_agent_binding_id ? (
                <div className={`chat-message-meta-row ${message.content ? "chat-message-meta-row--actions" : ""}`}>
                  <ChatTimestamp timestamp={message.created_at} className="chat-timestamp justify-self-start" />
                  <span className="chat-message-meta-actions gap-1">
                    {!message.deleted_at && reactionControls}
                    {message.content && <ChatMessageActions align="right" copyText={message.content} copyLabel={tr("复制回复", "Copy response")} />}
                  </span>
                </div>
              ) : <div className="chat-message-meta-chips"><ChatTimestamp timestamp={message.created_at} />{message.mentioned_user_ids.includes(currentUserId) && <span className="chat-message-meta-chip">{tr("提到了你", "Mentioned you")}</span>}{!message.deleted_at && reactionControls}</div>}
            </div>
          </div>
          {activeAgentRuns.map((run) => {
            const runAgent = room.agents.find((agent) => agent.id === run.binding_id);
            return <div key={run.id} className="chat-message-row chat-message-shell">
              <UserAvatar type={runAgent?.is_default ? "manor" : "agent"} name={run.agent_name} seed={run.binding_id} size={32} />
              <div className="chat-message-col">
                <span className="chat-sender-name chat-sender-name--agent">{run.agent_name}</span>
                <MessageBubble role="other" className="chat-bubble chat-bubble--bot chat-bubble--activity">
                  <RoomAgentRunIndicator run={run} onReview={run.status === "waiting_approval" && canReviewAiTools && (run.approval_kind === "reply" ? pendingReplyApprovals.some((approval) => approval.run_id === run.id) : pendingToolApprovals.some((approval) => approval.run_id === run.id)) ? () => { setApprovalError(""); setApprovalOpen(true); } : undefined} />
                </MessageBubble>
              </div>
            </div>;
          })}
          {!message.sender_agent_binding_id && runStatusLabel && activeAgentRuns.length === 0 && <div className="chat-message-row chat-message-shell" role="status">
            <UserAvatar type="manor" size={32} />
            <div className="chat-message-col"><span className="chat-sender-name chat-sender-name--agent">Manor AI</span><MessageBubble role="other" className="chat-bubble chat-bubble--bot chat-bubble--activity"><span className="text-xs text-[var(--text-muted)]">{runStatusLabel}</span></MessageBubble></div>
          </div>}
          </Fragment>;
        })}
        </div>
      </div>
    </div>
    {room.kind === "dm" && room.member_count < 2 ? <div className="embedded-chat-footer"><p className="m-0 rounded-xl bg-[var(--surface-muted)] p-3 text-sm text-[var(--text-muted)]">{tr("对方接受邀请后即可开始私聊。", "You can start messaging after the recipient accepts the invitation.")}</p></div> : <ChatInputFooter key={roomId} humanChat attachments={room.kind === "group" ? "room-files" : "none"} approvalSlot={room.kind === "group" ? <ApprovalModeTrigger scope={{ kind: "room", id: roomId }} /> : undefined} value={draft} onChange={(value) => setDraft(value.slice(0, 4000))} streaming={busy} onSend={(text, items) => void send(text, items)} onStop={() => {}} showStopButton={false} placeholder={room.kind === "group" ? tr("发消息给大家，@ 提及成员或 AI", "Message everyone, @ to mention people or AI") : tr("发私信，@ 提及对方", "Send a message, @ to mention the other person")} mentions={mentionOptions} selectedMentions={selectedMentions} onMentionSelect={(mention) => setSelectedMentionKeys((keys) => [...new Set([...keys, `${mention.type}:${mention.id}`])])} onMentionRemove={(mention) => setSelectedMentionKeys((keys) => keys.filter((key) => key !== `${mention.type}:${mention.id}`))} seedAttachments={retryAttachments} seedAttachmentsKey={String(retryAttachmentSeed)} topSlot={composerTopSlot} />}
    </>}
        </div>,
      }, ...(selectedArtifact ? [{
        id: "output", label: tr("文件预览", "File preview"), initialSize: 1, minSize: 360,
        className: "embedded-chat-pane embedded-chat-pane--output",
        children: <OutputPanel artifact={selectedArtifact} updating={false} onStop={closePreview} returnTo={`/chat?room=${roomId}`} />,
      }] : [])]} />
    </div>
    <Modal open={inviteOpen} onClose={() => setInviteOpen(false)} title={canReissueDmInvite ? tr("重发邀请", "Reinvite") : tr("邀请成员", "Invite member")} maxWidth="460px">
      {canManageGroup && <TabSwitcher className="mb-4" ariaLabel={tr("邀请方式", "Invitation method")} tabs={[{ key: "email", label: tr("邮箱邀请", "Email invitation") }, { key: "link", label: tr("公开链接", "Public link") }]} value={inviteMode} onChange={setInviteMode} />}
      {canManageGroup && inviteMode === "link" ? <GroupPublicInvite key={roomId} roomId={roomId} /> : inviteResult ? <div className="space-y-4">{inviteResult.outcome === "added" ? <p role="status" className="m-0 text-sm text-[var(--text-default)]"><strong>{inviteResult.email}</strong>{tr(" 已加入群聊。", " has joined the group.")}</p> : <InviteLink token={inviteResult.invite_token} email={inviteResult.email} nextStep={inviteResult.invite_next_step} emailSent={inviteResult.email_sent} />}<Button onClick={() => setInviteOpen(false)}>{tr("完成", "Done")}</Button></div> : <form onSubmit={invite} className="space-y-3">
        <Input type="email" label={tr("邮箱", "Email")} required disabled={canReissueDmInvite} value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} placeholder="name@example.com" />
        {room.kind === "group" && <GroupInviteSenderPicker sender={inviteSender} disabled={busy} />}
        <p className="m-0 text-xs text-[var(--text-muted)]">{room.kind === "group" ? tr("邀请邮件会通过你的发送邮箱发给所有受邀人。已验证的 Manor 用户也会收到站内邀请。对方同意后加入；邀请可取消，不会授予公司或 Workspace 权限。", "Invitation emails are sent to all invitees through your sending account. Verified Manor users also receive an in-app invitation. They join after accepting; invitations can be canceled and grant no company or Workspace access.") : tr("受邀人验证该邮箱后加入；不会获得公司或 Workspace 权限。", "The invitee must verify this email to join. No company or Workspace access is granted.")}</p>
        {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
        <div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => setInviteOpen(false)}>{tr("取消", "Cancel")}</Button><Button type="submit" disabled={busy || (room.kind === "group" && inviteSender.blocked)}>{room.kind === "group" ? tr("发送邀请", "Send invitation") : tr("生成邀请链接", "Create invitation link")}</Button></div>
      </form>}
    </Modal>
    <Modal open={membersOpen && room.kind === "group"} onClose={() => { setMembersOpen(false); setMemberMenuId(null); }} title={`${tr("成员", "Members")} · ${room.member_count}`} maxWidth="520px">
      <div className="space-y-3">
        <Input ariaLabel={tr("搜索群成员", "Search group members")} autoFocus value={memberQuery} onChange={(event) => setMemberQuery(event.target.value)} placeholder={tr("搜索群成员", "Search group members")} />
        {error && <p role="alert" className="m-0 text-xs text-red-700">{error}</p>}
        <div role="list" className="max-h-[min(55vh,420px)] space-y-0.5 overflow-y-auto">{filteredMembers.map(renderMemberRow)}</div>
        {filteredMembers.length === 0 && <p role="status" className="m-0 py-5 text-center text-sm text-[var(--text-muted)]">{tr("没有匹配的成员", "No matching members")}</p>}
      </div>
    </Modal>
    <Modal open={renameOpen} onClose={() => { setRenameOpen(false); setError(""); }} title={tr("修改群名称", "Rename group")} maxWidth="420px">
      <form onSubmit={rename} className="space-y-4">
        <Input label={tr("群名称", "Group name")} appearance="quiet" autoFocus required maxLength={255} value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} />
        {error && <p role="alert" className="m-0 text-sm text-red-700">{error}</p>}
        <div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={() => { setRenameOpen(false); setError(""); }}>{tr("取消", "Cancel")}</Button><Button type="submit" disabled={busy || !renameTitle.trim()}>{busy ? tr("保存中…", "Saving…") : tr("保存", "Save")}</Button></div>
      </form>
    </Modal>
    <Modal open={approvalOpen && !!canReviewAiTools} onClose={() => { setApprovalOpen(false); setApprovalError(""); }} title={tr("AI 待审批请求", "AI requests")} maxWidth="520px">
      <div className="space-y-3">
        {approvalError && <p role="alert" className="m-0 text-xs text-red-700">{approvalError}</p>}
        {(approvalsLoading || repliesLoading) && <p role="status" className="m-0 py-4 text-sm text-[var(--text-muted)]">{tr("正在加载审批…", "Loading requests…")}</p>}
        {(approvalsError || repliesError) && <p role="alert" className="m-0 py-4 text-sm text-red-700">{tr("无法加载审批，请重试。", "Requests could not be loaded. Please retry.")}</p>}
        {!approvalsLoading && !repliesLoading && !approvalsError && !repliesError && pendingToolApprovals.length + pendingReplyApprovals.length === 0 && <p role="status" className="m-0 py-4 text-sm text-[var(--text-muted)]">{tr("暂无待审批操作", "No requests awaiting approval")}</p>}
        <div role="list" className="max-h-[min(55vh,420px)] space-y-2 overflow-y-auto">
          {pendingReplyApprovals.map((approval) => <div role="listitem" key={approval.run_id} className="min-w-0 rounded-xl bg-[var(--surface-muted)] p-3">
            <p className="m-0 text-sm font-medium text-[var(--text-strong)]">{approval.agent_name} · {tr("群聊回复", "Group reply")}</p>
            <p className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-sm text-[var(--text-default)]">{approval.content}</p>
            <div className="mt-3 flex flex-wrap justify-end gap-2"><Button type="button" size="sm" variant="outline" disabled={!!approvalDecisionId} onClick={() => void decideAiReply(approval.run_id, approval.digest, false)}>{tr("拒绝", "Reject")}</Button><Button type="button" size="sm" disabled={!!approvalDecisionId} onClick={() => void decideAiReply(approval.run_id, approval.digest, true)}>{approvalDecisionId === approval.run_id ? tr("处理中…", "Working…") : tr("批准", "Approve")}</Button></div>
          </div>)}
          {pendingToolApprovals.map((approval) => <div role="listitem" key={approval.id} className="min-w-0 rounded-xl bg-[var(--surface-muted)] p-3">
            <p className="m-0 break-words text-sm font-medium text-[var(--text-strong)]">{approval.summary}</p>
            <p className="m-0 mt-1 truncate text-xs text-[var(--text-muted)]">{room.members.find((member) => member.user_id === approval.requester_user_id)?.name || tr("原群成员", "Former member")}</p>
            <div className="mt-3 flex flex-wrap justify-end gap-2"><Button type="button" size="sm" variant="outline" disabled={!!approvalDecisionId} onClick={() => void decideAiTool(approval.id, false)}>{tr("拒绝", "Reject")}</Button><Button type="button" size="sm" disabled={!!approvalDecisionId} onClick={() => void decideAiTool(approval.id, true)}>{approvalDecisionId === approval.id ? tr("处理中…", "Working…") : tr("批准", "Approve")}</Button></div>
          </div>)}
        </div>
      </div>
    </Modal>
    <ConfirmDialog open={closeOpen} onClose={() => { setCloseOpen(false); setError(""); }} onConfirm={() => void closeGroup()} title={tr("解散群聊？", "Close this group?")} message={tr("解散后，所有成员都无法再访问群聊和消息。后台会保留记录供审计，此操作无法在界面撤销。", "Everyone will lose access to this group and its messages. Records are retained for audit, and this cannot be undone in the app.")} confirmLabel={tr("解散群聊", "Close group")} danger loading={busy} closeOnConfirm={false} error={closeOpen ? error : undefined} />
    <ConfirmDialog open={leaveOpen} onClose={() => { setLeaveOpen(false); setError(""); }} onConfirm={() => void leaveGroup()} title={tr("退出群聊？", "Leave group?")} message={tr("退出后将无法再查看本群消息，除非重新获邀加入。", "You will lose access to this group's messages unless you are invited again.")} confirmLabel={tr("退出", "Leave")} danger loading={busy} closeOnConfirm={false} error={leaveOpen ? error : undefined} />
    <ConfirmDialog open={!!messageToRetract} onClose={() => { setMessageToRetract(null); setRetractError(""); }} onConfirm={() => void retractMessage()} title={tr("撤回消息？", "Retract message?")} message={tr("消息内容会从聊天中隐藏并保留审计记录。已分享的群文件仍可在文件列表中单独管理。", "The message will be hidden from chat and retained for audit. Shared group files can still be managed separately in Files.")} confirmLabel={tr("撤回", "Retract")} danger loading={!!retractingMessageId} closeOnConfirm={false} error={retractError} />
    <ConfirmDialog open={!!fileToDelete} onClose={() => { setFileToDelete(null); setDeleteFileError(""); }} onConfirm={() => void deleteFile()} title={tr("删除群文件？", "Remove group file?")} message={tr(`“${fileToDelete?.name || ""}”将从本群消息和文件列表移除，原始 Knowledge 文件不受影响。`, `“${fileToDelete?.name || ""}” will be removed from this group's messages and files. The original Knowledge file will remain.`)} confirmLabel={tr("删除文件", "Remove file")} danger loading={!!deletingFileId} closeOnConfirm={false} error={deleteFileError} />
  </div>;
}

export function ChatInviteAcceptance({ token, onClose, onAccepted }: { token: string; onClose: () => void; onAccepted: (room: ChatRoom) => void }) {
  const queryClient = useQueryClient();
  const { data: emailProof, refetch: refetchEmailProof } = useQuery({ queryKey: ["current-email-verification"], queryFn: api.auth.currentEmailVerification });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [proofSent, setProofSent] = useState(false);
  const [proofBusy, setProofBusy] = useState(false);
  const sendProof = async () => {
    setProofBusy(true); setError("");
    try { const result = await api.auth.sendCurrentEmailVerification(); setProofSent(result.sent); await refetchEmailProof(); }
    catch (cause) { setError(errorText(cause)); }
    finally { setProofBusy(false); }
  };
  const confirmProof = async (event: FormEvent) => {
    event.preventDefault(); setProofBusy(true); setError("");
    try { await api.auth.confirmCurrentEmailVerification(verificationCode.trim()); setProofSent(false); await refetchEmailProof(); }
    catch (cause) { setError(errorText(cause)); }
    finally { setProofBusy(false); }
  };
  return <Modal open onClose={onClose} title={tr("加入对话", "Join conversation")} maxWidth="460px">
    <div className="space-y-4">
      <p className="m-0 text-sm leading-6 text-[var(--text-muted)]">{tr("确认加入后，你只能查看加入后的群消息。请先用受邀邮箱登录并完成邮箱验证。", "You will only see group messages sent after you join. Sign in with the invited email and verify it first.")}</p>
      {emailProof && <div className="rounded-xl bg-[var(--surface-muted)] p-3 text-sm"><span className="text-[var(--text-default)]">{emailProof.email}</span><span className="ml-2 text-xs text-[var(--text-muted)]">{emailProof.email_verified ? tr("已验证", "Verified") : tr("未验证", "Not verified")}</span></div>}
      {emailProof && !emailProof.email_verified && (proofSent ? <form onSubmit={confirmProof} className="flex items-end gap-2"><Input className="min-w-0 flex-1" autoComplete="one-time-code" required value={verificationCode} onChange={(event) => setVerificationCode(event.target.value)} placeholder={tr("邮件验证码", "Email verification code")} ariaLabel={tr("邮件验证码", "Email verification code")} /><Button type="submit" disabled={proofBusy}>{tr("验证", "Verify")}</Button></form> : <Button type="button" variant="outline" disabled={proofBusy} onClick={() => void sendProof()}>{tr("发送邮箱验证码", "Send verification code")}</Button>)}
      {error && <p role="alert" className="m-0 text-sm text-red-700">{error}</p>}
      <div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={onClose}>{tr("取消", "Cancel")}</Button><Button disabled={busy || emailProof?.email_verified === false} onClick={async () => { setBusy(true); setError(""); try { const room = await api.chatRooms.accept(token); await queryClient.invalidateQueries({ queryKey: ["chat-rooms"] }); onAccepted(room); } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); } }}>{busy ? tr("加入中…", "Joining…") : tr("确认加入", "Join")}</Button></div>
    </div>
  </Modal>;
}
