/**
 * AvatarStack — overlapping people avatars with a "+N" overflow chip.
 *
 * Usage:
 *   <AvatarStack people={[{ id, name, avatarUrl }]} total={5} label="Shared with Bob, Ana" />
 *
 * Each avatar is ringed in the panel surface colour so overlaps read in both
 * themes; pass `total` when `people` is only a preview of a longer list.
 */
import UserAvatar from "./UserAvatar";

export interface AvatarStackPerson {
  id: string;
  name: string;
  avatarUrl?: string | null;
}

const RING = "2px solid var(--surface-panel)";

export default function AvatarStack({
  people,
  total,
  size = 20,
  label,
}: {
  people: AvatarStackPerson[];
  total?: number;
  size?: number;
  label?: string;
}) {
  const overlap = Math.round(size * 0.3);
  const extra = Math.max(0, (total ?? people.length) - people.length);
  return (
    <span
      className="inline-flex shrink-0 items-center"
      title={label}
      aria-label={label}
      role={label ? "img" : undefined}
    >
      {people.map((person, index) => (
        <UserAvatar
          key={person.id}
          name={person.name}
          avatarUrl={person.avatarUrl}
          size={size}
          style={{ marginLeft: index === 0 ? 0 : -overlap, border: RING, position: "relative" }}
        />
      ))}
      {extra > 0 && (
        <span
          className="mono inline-flex items-center justify-center rounded-full"
          style={{
            width: size,
            height: size,
            marginLeft: people.length ? -overlap : 0,
            border: RING,
            background: "var(--chip-neutral-bg)",
            color: "var(--chip-neutral-fg)",
            fontSize: Math.max(9, Math.round(size * 0.42)),
            position: "relative",
          }}
        >
          +{extra}
        </span>
      )}
    </span>
  );
}
