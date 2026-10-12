import type { ChatRoomMember } from "../../lib/chatRoomTypes";
import UserAvatar from "./UserAvatar";

export default function GroupChatAvatar({ title, members, currentUserId, size = 30 }: {
  title: string;
  members: ChatRoomMember[];
  currentUserId?: string;
  size?: number;
}) {
  const visibleMembers = [
    ...members.filter((member) => member.user_id !== currentUserId),
    ...members.filter((member) => member.user_id === currentUserId),
  ].slice(0, 3);

  if (visibleMembers.length < 2) {
    return <span aria-hidden="true" className="inline-flex shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--surface-sunken)] font-semibold text-[var(--text-default)]" style={{ width: size, height: size, fontSize: Math.round(size * 0.43) }}>{Array.from(title.trim())[0]?.toLocaleUpperCase() || "#"}</span>;
  }

  const avatarSize = Math.round(size * (visibleMembers.length === 2 ? 0.7 : 0.62));
  const positions = visibleMembers.length === 2
    ? [[0, 0], [size - avatarSize, size - avatarSize]]
    : [[0, 0], [size - avatarSize, 0], [Math.round((size - avatarSize) / 2), size - avatarSize]];

  return <div aria-hidden="true" className="relative inline-flex shrink-0" style={{ width: size, height: size }}>
    {visibleMembers.map((member, index) => <div key={member.user_id} className="absolute rounded-full" style={{ left: positions[index][0], top: positions[index][1], zIndex: index + 1 }}>
      <UserAvatar name={Array.from(member.name.trim())[0] || "?"} avatarUrl={member.avatar_url} size={avatarSize} style={{ background: index % 2 === 0 ? "var(--surface-sunken)" : "var(--surface-muted)", color: "var(--text-strong)", border: "2px solid var(--surface-panel)", fontSize: Math.max(9, Math.round(avatarSize * 0.45)) }} />
    </div>)}
  </div>;
}
