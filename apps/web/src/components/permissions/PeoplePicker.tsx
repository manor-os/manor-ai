/**
 * PeoplePicker — typeahead for selecting team members from the entity's
 * staff roster.
 *
 * Enterprise-grade replacement for "type a free-text email address". Users
 * pick from a real list of people; downstream code gets a structured
 * StaffOption ({ id, name, email, user_id, role, department }) so grant
 * creation can resolve the proper subject_id without an extra lookup.
 *
 * Behavior:
 *   - On focus / typing, queries api.staff.list() once (entity-scoped),
 *     caches the result for the dialog session.
 *   - Local filter on name/email/department — fast feedback, no backend
 *     round-trip per keystroke.
 *   - Falls back to allowing free-text email entry (e.g. external partner
 *     not yet in the staff roster); marked as ``external`` in the result.
 *   - Keyboard nav: ↑/↓ to move, Enter to select, Escape to close.
 *
 * UI: built on project primitives (<Input>, <UserAvatar>) so visuals stay
 * consistent with the rest of Manor.
 *
 * Used in:
 *   - ShareDialog "Add people" section (primary)
 *   - Future: WorkspaceDetail member-add modal (already gated on staff
 *     picker, can swap in this component for consistency)
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Input from "../ui/Input";
import UserAvatar from "../ui/UserAvatar";

export interface StaffOption {
  id: string;                  // staff.id (ULID)
  user_id: string | null;      // resolved user account (null = no login yet)
  name: string;
  email: string | null;
  department: string | null;
  title: string | null;
  avatar_url: string | null;
  /** "match" reason — what the user typed against. Used to highlight. */
  matched_field?: "name" | "email" | "department";
}

interface Props {
  /** Called when the user picks someone from the list or commits a free
   *  email. `kind` tells the caller whether to expect a staff_id or to
   *  treat the input as an external email. */
  onPick: (
    pick:
      | { kind: "staff"; staff: StaffOption }
      | { kind: "external_email"; email: string },
  ) => void;
  /** ULIDs to exclude from suggestions (e.g. already-granted people). */
  excludeStaffIds?: string[];
  /** Allow free-text email when nothing matches. Default true. */
  allowExternalEmail?: boolean;
  placeholder?: string;
  disabled?: boolean;
  /** Auto-focus the input on mount. Default false. */
  autoFocus?: boolean;
}

function _emailRegex(value: string): boolean {
  // Pragmatic — RFC 5322 is overkill for a UI sanity check.
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

export default function PeoplePicker({
  onPick,
  excludeStaffIds = [],
  allowExternalEmail = true,
  placeholder,
  disabled = false,
  autoFocus = false,
}: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const inputWrapRef = useRef<HTMLDivElement | null>(null);
  const dropdownRef = useRef<HTMLDivElement | null>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [highlightIndex, setHighlightIndex] = useState(0);
  const [allStaff, setAllStaff] = useState<StaffOption[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // One-shot fetch on first focus. Manor entities are small (rarely
  // >1000 staff) so loading the full list client-side and filtering
  // locally is faster + simpler than a debounced server call.
  useEffect(() => {
    if (allStaff !== null) return;
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const rows = await api.staff.list();
        if (cancelled) return;
        const mapped: StaffOption[] = (rows as any[]).map((r) => ({
          id: r.id,
          user_id: r.user_id || null,
          name: r.name || r.email || "—",
          email: r.email || null,
          department: r.department || null,
          title: r.title || null,
          avatar_url: r.avatar_url || null,
        }));
        setAllStaff(mapped);
      } catch (e: any) {
        if (cancelled) return;
        setLoadError(e?.message || t("permissions.picker.load_failed"));
        setAllStaff([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, allStaff]);

  // Close on outside click.
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (wrapRef.current?.contains(e.target as Node)) return;
      setOpen(false);
    }
    if (open) {
      document.addEventListener("mousedown", onDown);
      return () => document.removeEventListener("mousedown", onDown);
    }
  }, [open]);

  useEffect(() => {
    if (autoFocus) {
      inputWrapRef.current?.querySelector("input")?.focus();
    }
  }, [autoFocus]);

  const filtered = useMemo(() => {
    if (!allStaff) return [];
    const q = query.trim().toLowerCase();
    const excluded = new Set(excludeStaffIds);
    let candidates = allStaff.filter((s) => !excluded.has(s.id));
    if (!q) return candidates.slice(0, 50);
    return candidates
      .map((s): { s: StaffOption; score: number; matched: StaffOption["matched_field"] } => {
        const name = (s.name || "").toLowerCase();
        const email = (s.email || "").toLowerCase();
        const dept = (s.department || "").toLowerCase();
        if (name.startsWith(q)) return { s, score: 0, matched: "name" };
        if (email.startsWith(q)) return { s, score: 1, matched: "email" };
        if (name.includes(q)) return { s, score: 2, matched: "name" };
        if (email.includes(q)) return { s, score: 3, matched: "email" };
        if (dept.includes(q)) return { s, score: 4, matched: "department" };
        return { s, score: 999, matched: undefined };
      })
      .filter((m) => m.score < 999)
      .sort((a, b) => a.score - b.score)
      .slice(0, 50)
      .map((m) => ({ ...m.s, matched_field: m.matched }));
  }, [allStaff, query, excludeStaffIds]);

  const showExternalOption =
    allowExternalEmail &&
    query.trim().length > 0 &&
    _emailRegex(query.trim()) &&
    !filtered.some(
      (s) => (s.email || "").toLowerCase() === query.trim().toLowerCase(),
    );

  const totalItems = filtered.length + (showExternalOption ? 1 : 0);

  const commit = (pick: { kind: "staff"; staff: StaffOption } | { kind: "external_email"; email: string }) => {
    onPick(pick);
    setQuery("");
    setOpen(false);
    setHighlightIndex(0);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightIndex((i) => Math.min(i + 1, Math.max(0, totalItems - 1)));
      setOpen(true);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlightIndex < filtered.length) {
        commit({ kind: "staff", staff: filtered[highlightIndex] });
      } else if (showExternalOption) {
        commit({ kind: "external_email", email: query.trim() });
      }
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div
      ref={wrapRef}
      style={{ position: "relative", width: "100%" }}
      onKeyDown={onKeyDown}
    >
      <div
        ref={inputWrapRef}
        onFocusCapture={() => setOpen(true)}
        onClick={() => setOpen(true)}
      >
        <Input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
            setHighlightIndex(0);
          }}
          placeholder={placeholder ?? t("permissions.picker.placeholder")}
          disabled={disabled}
        />
      </div>

      {open && (
        <div
          ref={dropdownRef}
          id="people-picker-listbox"
          role="listbox"
          style={{
            position: "absolute",
            top: "calc(100% + 4px)",
            left: 0,
            right: 0,
            maxHeight: 280,
            overflowY: "auto",
            background: "#ffffff",
            border: "1px solid rgba(28,25,23,0.06)",
            borderRadius: 8,
            boxShadow: "0 8px 24px rgba(28,25,23,0.08)",
            zIndex: 1000,
          }}
        >
          {loadError ? (
            <div style={EMPTY_STATE_STYLE}>{loadError}</div>
          ) : allStaff === null ? (
            <div style={EMPTY_STATE_STYLE}>{t("permissions.picker.loading")}</div>
          ) : filtered.length === 0 && !showExternalOption ? (
            <div style={EMPTY_STATE_STYLE}>
              {query.trim() ? t("permissions.picker.no_match") : t("permissions.picker.empty_roster")}
            </div>
          ) : (
            <>
              {filtered.map((s, i) => (
                <button
                  type="button"
                  key={s.id}
                  role="option"
                  aria-selected={i === highlightIndex}
                  onClick={() => commit({ kind: "staff", staff: s })}
                  onMouseEnter={() => setHighlightIndex(i)}
                  style={{
                    ...ROW_STYLE,
                    background:
                      i === highlightIndex ? "rgba(67,107,101,0.08)" : "transparent",
                  }}
                >
                  <UserAvatar name={s.name} avatarUrl={s.avatar_url} size={28} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "#292524", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {s.name}
                      {!s.user_id && (
                        <span style={{ marginLeft: 6, fontSize: 10, color: "#a8a29e", fontWeight: 500 }}>
                          {t("permissions.picker.no_account_hint")}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 11, color: "#78716c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {s.email || t("permissions.picker.no_email")}
                      {s.department && ` · ${s.department}`}
                      {s.title && ` · ${s.title}`}
                    </div>
                  </div>
                </button>
              ))}

              {showExternalOption && (
                <button
                  type="button"
                  role="option"
                  aria-selected={highlightIndex === filtered.length}
                  onClick={() => commit({ kind: "external_email", email: query.trim() })}
                  onMouseEnter={() => setHighlightIndex(filtered.length)}
                  style={{
                    ...ROW_STYLE,
                    borderTop: filtered.length > 0 ? "1px solid rgba(231,229,228,0.6)" : undefined,
                    background:
                      highlightIndex === filtered.length ? "rgba(67,107,101,0.08)" : "transparent",
                  }}
                >
                  <UserAvatar name={query.trim()} avatarUrl={null} size={28} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "#292524" }}>
                      {t("permissions.picker.invite_external", { email: query.trim() })} <span style={{ color: "#a8a29e", fontWeight: 500 }}>{t("permissions.picker.external_tag")}</span>
                    </div>
                    <div style={{ fontSize: 11, color: "#a8a29e" }}>
                      {t("permissions.picker.external_hint")}
                    </div>
                  </div>
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

const ROW_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "8px 12px",
  width: "100%",
  textAlign: "left",
  background: "transparent",
  border: "none",
  cursor: "pointer",
};

const EMPTY_STATE_STYLE: React.CSSProperties = {
  padding: "16px 14px",
  fontSize: 12,
  color: "#a8a29e",
  textAlign: "center",
};
