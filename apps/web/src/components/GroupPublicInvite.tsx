import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import type { ChatRoom } from "../lib/chatRoomTypes";
import { getLocale } from "../lib/i18n";
import Button from "./ui/Button";
import Input from "./ui/Input";
import Modal from "./ui/Modal";

const tr = (zh: string, en: string) => getLocale() === "zh" ? zh : en;
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : tr("操作失败，请重试。", "Something went wrong. Please try again.");

export default function GroupPublicInvite({ roomId }: { roomId: string }) {
  const queryClient = useQueryClient();
  const queryKey = ["chat-room-public-invite", roomId];
  const { data, isPending, isError, refetch } = useQuery({
    queryKey, queryFn: () => api.chatRooms.publicInvite(roomId), staleTime: 0,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const token = data?.token;
  const url = token ? `${window.location.origin}/chat?join=${encodeURIComponent(token)}` : "";
  const changeLink = async (action: "enable" | "reset" | "disable") => {
    setBusy(true); setError(""); setCopied(false);
    try {
      let result: { token: string | null };
      if (action === "disable") {
        await api.chatRooms.disablePublicInvite(roomId);
        result = { token: null };
      } else {
        result = await api.chatRooms.enablePublicInvite(roomId, action === "reset");
      }
      queryClient.setQueryData(queryKey, result);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  return <section aria-label={tr("公开邀请链接", "Public invitation link")} className="space-y-4">
    <p className="m-0 text-sm leading-6 text-[var(--text-muted)]">{tr("任何拿到链接的人都可以登录或注册后自动加入，无需审批。新成员只能看到加入后的消息，不会获得公司或 Workspace 权限。", "Anyone with the link can sign in or register to join automatically, without approval. New members only see messages sent after joining and gain no company or Workspace access.")}</p>
    {isPending ? <p role="status" className="text-sm text-[var(--text-muted)]">{tr("正在加载…", "Loading…")}</p>
      : isError ? <div className="space-y-2"><p role="alert" className="text-sm text-[var(--text-muted)]">{tr("无法加载邀请链接。", "Could not load the invitation link.")}</p><Button variant="outline" onClick={() => void refetch()}>{tr("重试", "Retry")}</Button></div>
      : <>
        {token ? <>
          <Input label={tr("公开邀请链接", "Public invitation link")} value={url} readOnly onChange={() => {}} onFocus={(event) => event.target.select()} />
          <p className="m-0 text-xs text-[var(--text-muted)]">{tr("链接持续有效，直到关闭或重置；重置后旧链接立即失效。", "The link stays active until disabled or reset. Resetting immediately invalidates the old link.")}</p>
          <div className="flex flex-wrap gap-2">
            <Button disabled={busy} onClick={async () => {
              setError("");
              try { await navigator.clipboard.writeText(url); setCopied(true); }
              catch { setError(tr("无法复制，请选中链接后手动复制。", "Could not copy. Select the link and copy it manually.")); }
            }}>{copied ? tr("已复制", "Copied") : tr("复制链接", "Copy link")}</Button>
            <Button variant="outline" disabled={busy} onClick={() => void changeLink("reset")}>{tr("重置链接", "Reset link")}</Button>
            <Button variant="outline" disabled={busy} onClick={() => void changeLink("disable")}>{tr("关闭链接", "Disable link")}</Button>
          </div>
        </> : <><p role="status" className="m-0 text-sm text-[var(--text-muted)]">{tr("公开邀请链接未开启。", "Public invitations are disabled.")}</p><Button loading={busy} onClick={() => void changeLink("enable")}>{tr("生成公开链接", "Create public link")}</Button></>}
      </>}
    {busy && token && <p role="status" className="m-0 text-xs text-[var(--text-muted)]">{tr("正在更新…", "Updating…")}</p>}
    {error && <p role="alert" className="m-0 text-sm text-[var(--editor-danger-text)]">{error}</p>}
  </section>;
}

export function PublicGroupJoin({ token, onClose, onJoined }: { token: string; onClose: () => void; onJoined: (room: ChatRoom) => void }) {
  const queryClient = useQueryClient();
  const joined = useRef(onJoined);
  joined.current = onJoined;
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setError(""); setUnavailable(false);
    void api.chatRooms.joinPublicInvite(token).then(async (room) => {
      await queryClient.invalidateQueries({ queryKey: ["chat-rooms"] });
      if (!cancelled) joined.current(room);
    }).catch((cause) => {
      if (cancelled) return;
      const invalid = cause instanceof ApiError && (cause.status === 404 || cause.status === 422);
      setUnavailable(invalid);
      setError(invalid ? tr("邀请链接已失效或群聊已关闭，请向群主索取新的链接。", "This invitation is unavailable or the group is closed. Ask the group owner for a new link.") : errorText(cause));
    });
    return () => { cancelled = true; };
  }, [token, attempt, queryClient]);
  return <Modal open onClose={onClose} title={tr("加入群聊", "Join group")} maxWidth="460px">
    <div className="space-y-4">
      {error ? <p role="alert" className="m-0 text-sm text-[var(--editor-danger-text)]">{error}</p>
        : <p role="status" className="m-0 text-sm text-[var(--text-muted)]">{tr("正在加入群聊…", "Joining the group…")}</p>}
      <div className="flex justify-end gap-2"><Button variant="outline" onClick={onClose}>{tr("关闭", "Close")}</Button>{error && !unavailable && <Button onClick={() => setAttempt((value) => value + 1)}>{tr("重试", "Retry")}</Button>}</div>
    </div>
  </Modal>;
}
