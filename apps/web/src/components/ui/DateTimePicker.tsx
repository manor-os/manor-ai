import { useState, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { IconCalendar, IconClock, IconChevronLeft, IconChevronRight } from "../icons";
import { t } from "../../lib/i18n";

/* ══════════════════════════════════════════════════════════════
   DateTimePicker — glassmorphism date & time picker
   ══════════════════════════════════════════════════════════════
   Modes:
     "date"      — date only (YYYY-MM-DD)
     "time"      — time only (HH:mm)
     "datetime"  — date + time  (YYYY-MM-DDTHH:mm)

   Usage:
     <DateTimePicker value="2026-04-23" onChange={setDate} />
     <DateTimePicker mode="time" value="14:30" onChange={setTime} />
     <DateTimePicker mode="datetime" value="2026-04-23T14:30" onChange={setDt} />
   ══════════════════════════════════════════════════════════════ */

type Mode = "date" | "time" | "datetime";
type Panel = "calendar" | "time";

interface DateTimePickerProps {
  value: string;
  onChange: (value: string) => void;
  mode?: Mode;
  placeholder?: string;
  min?: string;
  max?: string;
  style?: React.CSSProperties;
  disabled?: boolean;
  /** Force popup direction. "auto" checks available space. */
  dropDirection?: "up" | "down" | "auto";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

function pad(n: number) { return n < 10 ? `0${n}` : `${n}`; }

function parseValue(value: string, mode: Mode) {
  const now = new Date();
  let year = now.getFullYear(), month = now.getMonth(), day = now.getDate();
  let hour = 12, minute = 0;

  if (mode === "time") {
    const m = value.match(/^(\d{1,2}):(\d{2})$/);
    if (m) { hour = Number(m[1]); minute = Number(m[2]); }
  } else {
    const m = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) { year = Number(m[1]); month = Number(m[2]) - 1; day = Number(m[3]); }
    const t = value.match(/T(\d{2}):(\d{2})/);
    if (t) { hour = Number(t[1]); minute = Number(t[2]); }
  }
  return { year, month, day, hour, minute };
}

function formatDisplay(value: string, mode: Mode, placeholder: string) {
  if (!value) return placeholder;
  if (mode === "time") {
    const m = value.match(/^(\d{1,2}):(\d{2})$/);
    if (!m) return placeholder;
    const h = Number(m[1]), min = m[2];
    const ampm = h >= 12 ? "PM" : "AM";
    const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
    return `${h12}:${min} ${ampm}`;
  }
  const p = parseValue(value, mode);
  const dateStr = `${MONTHS[p.month]} ${p.day}, ${p.year}`;
  if (mode === "datetime") {
    const ampm = p.hour >= 12 ? "PM" : "AM";
    const h12 = p.hour === 0 ? 12 : p.hour > 12 ? p.hour - 12 : p.hour;
    return `${dateStr} ${h12}:${pad(p.minute)} ${ampm}`;
  }
  return dateStr;
}

export default function DateTimePicker({
  value, onChange, mode = "date", placeholder = "Select...", min, max, style, disabled,
  dropDirection = "auto",
}: DateTimePickerProps) {
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<Panel>(mode === "time" ? "time" : "calendar");
  const ref = useRef<HTMLDivElement>(null);
  // Ref for the portaled popup node so outside-click detection still
  // works once the popup leaves the trigger's DOM subtree.
  const popupNodeRef = useRef<HTMLDivElement>(null);
  // Popup is portaled to body to escape ancestor ``overflow``/``backdrop-filter``
  // clipping (e.g., the 380px task drawer with overflow:auto). Coords
  // are recomputed from the trigger rect on open + window resize.
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);

  const parsed = parseValue(value, mode);
  const [viewYear, setViewYear] = useState(parsed.year);
  const [viewMonth, setViewMonth] = useState(parsed.month);
  const [selHour, setSelHour] = useState(parsed.hour);
  const [selMinute, setSelMinute] = useState(parsed.minute);

  // Sync internal state when value prop changes
  useEffect(() => {
    const p = parseValue(value, mode);
    setViewYear(p.year);
    setViewMonth(p.month);
    setSelHour(p.hour);
    setSelMinute(p.minute);
  }, [value, mode]);

  // Compute popup position (viewport-relative since we portal to body)
  // and close on outside click.
  useEffect(() => {
    if (!open) return;

    const popupWidth = mode === "time" ? 220 : 300;
    const popupHeight = mode === "time" ? 230 : 360;

    const reposition = () => {
      if (!ref.current) return;
      const rect = ref.current.getBoundingClientRect();
      // Vertical: prefer below; flip above if there isn't room.
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      const goUp = dropDirection === "up" || (dropDirection === "auto" && spaceBelow < popupHeight + 12 && spaceAbove > spaceBelow);
      const top = goUp ? rect.top - popupHeight - 6 : rect.bottom + 6;
      // Horizontal: clamp to viewport so neither edge is clipped.
      let left = rect.left;
      if (left + popupWidth > window.innerWidth - 8) {
        // Try right-aligning to trigger right edge first
        left = rect.right - popupWidth;
      }
      if (left < 8) left = 8;
      setCoords({ top, left });
    };

    reposition();

    const handler = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current && ref.current.contains(t)) return;
      if (popupNodeRef.current && popupNodeRef.current.contains(t)) return;
      setOpen(false);
    };
    const onResize = () => reposition();
    document.addEventListener("mousedown", handler);
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onResize, true);  // capture all scroll events
    return () => {
      document.removeEventListener("mousedown", handler);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onResize, true);
    };
  }, [open, dropDirection, mode]);

  const emit = useCallback((y: number, m: number, d: number, h: number, min: number) => {
    if (mode === "time") { onChange(`${pad(h)}:${pad(min)}`); return; }
    const dateStr = `${y}-${pad(m + 1)}-${pad(d)}`;
    if (mode === "datetime") { onChange(`${dateStr}T${pad(h)}:${pad(min)}`); return; }
    onChange(dateStr);
  }, [mode, onChange]);

  const selectDay = (d: number) => {
    emit(viewYear, viewMonth, d, selHour, selMinute);
    if (mode === "datetime") { setPanel("time"); }
    else { setOpen(false); }
  };

  const selectTime = (h: number, m: number) => {
    setSelHour(h);
    setSelMinute(m);
    if (mode === "time") {
      emit(viewYear, viewMonth, parsed.day, h, m);
    } else {
      emit(viewYear, viewMonth, parsed.day, h, m);
    }
  };

  const prevMonth = () => {
    if (viewMonth === 0) { setViewMonth(11); setViewYear(viewYear - 1); }
    else setViewMonth(viewMonth - 1);
  };
  const nextMonth = () => {
    if (viewMonth === 11) { setViewMonth(0); setViewYear(viewYear + 1); }
    else setViewMonth(viewMonth + 1);
  };

  // Calendar grid
  const firstDow = new Date(viewYear, viewMonth, 1).getDay();
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  const today = new Date();
  const isToday = (d: number) => viewYear === today.getFullYear() && viewMonth === today.getMonth() && d === today.getDate();
  const isSelected = (d: number) => viewYear === parsed.year && viewMonth === parsed.month && d === parsed.day;

  const displayText = formatDisplay(value, mode, placeholder);
  const hasValue = !!value;
  const TriggerIcon = mode === "time" ? IconClock : IconCalendar;

  return (
    <div ref={ref} style={{ position: "relative", ...style }}>
      {/* Trigger */}
      <button
        type="button"
        disabled={disabled}
        onClick={() => { if (!disabled) { setOpen(!open); setPanel(mode === "time" ? "time" : "calendar"); } }}
        className="manor-input"
        style={{
          width: "100%", textAlign: "left", cursor: disabled ? "not-allowed" : "pointer",
          display: "flex", alignItems: "center", gap: 8,
          color: hasValue ? "var(--text-default)" : "var(--text-faint)",
          opacity: disabled ? 0.5 : 1,
          ...(open ? { borderColor: "var(--accent)", boxShadow: "0 0 0 3px var(--accent-ring)", background: "var(--surface-panel)" } : {}),
        }}
      >
        <TriggerIcon size={14} style={{ flexShrink: 0, color: "var(--text-faint)" }} />
        <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {displayText}
        </span>
        {hasValue && !disabled && (
          <span
            onClick={(e) => { e.stopPropagation(); onChange(""); }}
            style={{ flexShrink: 0, width: 16, height: 16, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-faint)", cursor: "pointer", fontSize: 11, fontWeight: 700 }}
            title={t("component.date_time_picker.clear")}
          >
            &times;
          </span>
        )}
      </button>

      {/* Popup — portaled to body so ancestor overflow/backdrop-filter
          can't clip it (the task drawer was clipping the calendar).
          Z-index 10500 so it sits above modal overlays (10000) and the
          onboarding tour (10001) — needed when the picker is triggered
          from inside a modal form (e.g., create-task dialog deadline). */}
      {open && coords && createPortal(
        <div ref={popupNodeRef} style={{
          position: "fixed", top: coords.top, left: coords.left, zIndex: 10500,
          background: "var(--surface-panel)", backdropFilter: "blur(24px)",
          border: "1px solid var(--border-default)", borderRadius: 16,
          boxShadow: "var(--shadow-lg)",
          width: mode === "time" ? 220 : 300, padding: 0, overflow: "hidden",
          animation: "dialog-in 0.15s ease-out",
        }}>
          {/* Tabs for datetime mode */}
          {mode === "datetime" && (
            <div style={{ display: "flex", borderBottom: "1px solid var(--border-default)" }}>
              {(["calendar", "time"] as Panel[]).map((p) => (
                <button key={p} type="button"
                  onClick={() => setPanel(p)}
                  style={{
                    flex: 1, padding: "10px 0", fontSize: 12, fontWeight: 600, border: "none", cursor: "pointer",
                    background: panel === p ? "var(--surface-panel)" : "var(--surface-muted)",
                    color: panel === p ? "var(--accent)" : "var(--text-faint)",
                    borderBottom: panel === p ? "2px solid var(--accent)" : "2px solid transparent",
                    display: "flex", alignItems: "center", justifyContent: "center", gap: 5,
                    transition: "all 0.15s",
                  }}>
                  {p === "calendar" ? <IconCalendar size={12} /> : <IconClock size={12} />}
                  {p === "calendar" ? "Date" : "Time"}
                </button>
              ))}
            </div>
          )}

          {/* ── Calendar panel ── */}
          {panel === "calendar" && (
            <div style={{ padding: 12 }}>
              {/* Month/year nav */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
                <button type="button" onClick={prevMonth}
                  style={{ width: 28, height: 28, borderRadius: 8, border: "none", background: "var(--surface-muted)", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-default)" }}>
                  <IconChevronLeft size={14} />
                </button>
                <span style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>
                  {MONTHS[viewMonth]} {viewYear}
                </span>
                <button type="button" onClick={nextMonth}
                  style={{ width: 28, height: 28, borderRadius: 8, border: "none", background: "var(--surface-muted)", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-default)" }}>
                  <IconChevronRight size={14} />
                </button>
              </div>
              {/* Day headers */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 2, marginBottom: 4 }}>
                {DAYS.map((d) => (
                  <div key={d} style={{ textAlign: "center", fontSize: 10, fontWeight: 700, color: "var(--text-faint)", padding: "4px 0", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                    {d}
                  </div>
                ))}
              </div>
              {/* Day grid */}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 2 }}>
                {/* Empty cells for first row offset */}
                {Array.from({ length: firstDow }).map((_, i) => (
                  <div key={`e${i}`} />
                ))}
                {Array.from({ length: daysInMonth }).map((_, i) => {
                  const d = i + 1;
                  const sel = isSelected(d);
                  const td = isToday(d);
                  return (
                    <button key={d} type="button"
                      onClick={() => selectDay(d)}
                      style={{
                        width: "100%", aspectRatio: "1", border: "none", borderRadius: 10, cursor: "pointer",
                        fontSize: 12, fontWeight: sel ? 700 : td ? 600 : 400,
                        color: sel ? "#fff" : td ? "var(--accent)" : "var(--text-default)",
                        background: sel ? "var(--accent)" : "transparent",
                        boxShadow: td && !sel ? "inset 0 0 0 1.5px var(--accent)" : "none",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        transition: "all 0.1s",
                      }}
                      onMouseEnter={(e) => { if (!sel) e.currentTarget.style.background = "var(--surface-muted)"; }}
                      onMouseLeave={(e) => { if (!sel) e.currentTarget.style.background = "transparent"; }}
                    >
                      {d}
                    </button>
                  );
                })}
              </div>
              {/* Today shortcut */}
              <div style={{ marginTop: 8, textAlign: "center" }}>
                <button type="button"
                  onClick={() => { setViewYear(today.getFullYear()); setViewMonth(today.getMonth()); selectDay(today.getDate()); }}
                  style={{ fontSize: 11, fontWeight: 600, color: "var(--accent)", background: "none", border: "none", cursor: "pointer", padding: "4px 12px", borderRadius: 6 }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = "var(--accent-soft)"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "none"; }}
                >
                  Today
                </button>
              </div>
            </div>
          )}

          {/* ── Time panel ── */}
          {panel === "time" && (
            <TimePanel hour={selHour} minute={selMinute} onChange={(h, m) => selectTime(h, m)} onDone={() => setOpen(false)} />
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}


/* ── Time picker sub-panel ── */
function TimePanel({ hour, minute, onChange, onDone }: {
  hour: number; minute: number;
  onChange: (h: number, m: number) => void;
  onDone: () => void;
}) {
  const hourRef = useRef<HTMLDivElement>(null);
  const minuteRef = useRef<HTMLDivElement>(null);

  // Scroll to selected on mount
  useEffect(() => {
    hourRef.current?.querySelector("[data-selected]")?.scrollIntoView({ block: "center" });
    minuteRef.current?.querySelector("[data-selected]")?.scrollIntoView({ block: "center" });
  }, []);

  const ampm = hour >= 12 ? "PM" : "AM";
  const h12 = hour === 0 ? 12 : hour > 12 ? hour - 12 : hour;

  return (
    <div style={{ padding: 12 }}>
      {/* Current time display */}
      <div style={{ textAlign: "center", marginBottom: 12, padding: "10px 0", background: "var(--surface-muted)", borderRadius: 10 }}>
        <span style={{ fontSize: 28, fontWeight: 800, color: "var(--text-strong)", letterSpacing: "-0.02em" }}>
          {pad(h12)}:{pad(minute)}
        </span>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-muted)", marginLeft: 6 }}>{ampm}</span>
      </div>

      {/* Hour + Minute columns */}
      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        {/* Hours */}
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-faint)", textAlign: "center", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.05em" }}>{t("component.date_time_picker.hour")}</div>
          <div ref={hourRef} style={{ maxHeight: 168, overflowY: "auto", borderRadius: 10, border: "1px solid var(--border-default)", background: "var(--surface-muted)" }}>
            {Array.from({ length: 24 }).map((_, h) => {
              const sel = h === hour;
              const display12 = h === 0 ? "12 AM" : h < 12 ? `${h} AM` : h === 12 ? "12 PM" : `${h - 12} PM`;
              return (
                <button key={h} type="button"
                  {...(sel ? { "data-selected": true } : {})}
                  onClick={() => onChange(h, minute)}
                  style={{
                    display: "block", width: "100%", padding: "7px 10px", border: "none", cursor: "pointer",
                    fontSize: 12, fontWeight: sel ? 700 : 400, textAlign: "center",
                    color: sel ? "#fff" : "var(--text-default)",
                    background: sel ? "var(--accent)" : "transparent",
                    borderRadius: sel ? 6 : 0,
                    transition: "all 0.1s",
                  }}
                  onMouseEnter={(e) => { if (!sel) e.currentTarget.style.background = "var(--surface-panel)"; }}
                  onMouseLeave={(e) => { if (!sel) e.currentTarget.style.background = "transparent"; }}
                >
                  {display12}
                </button>
              );
            })}
          </div>
        </div>
        {/* Minutes */}
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-faint)", textAlign: "center", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.05em" }}>{t("component.date_time_picker.min")}</div>
          <div ref={minuteRef} style={{ maxHeight: 168, overflowY: "auto", borderRadius: 10, border: "1px solid var(--border-default)", background: "var(--surface-muted)" }}>
            {[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map((m) => {
              const sel = m === minute;
              return (
                <button key={m} type="button"
                  {...(sel ? { "data-selected": true } : {})}
                  onClick={() => onChange(hour, m)}
                  style={{
                    display: "block", width: "100%", padding: "7px 10px", border: "none", cursor: "pointer",
                    fontSize: 12, fontWeight: sel ? 700 : 400, textAlign: "center",
                    color: sel ? "#fff" : "var(--text-default)",
                    background: sel ? "var(--accent)" : "transparent",
                    borderRadius: sel ? 6 : 0,
                    transition: "all 0.1s",
                  }}
                  onMouseEnter={(e) => { if (!sel) e.currentTarget.style.background = "var(--surface-panel)"; }}
                  onMouseLeave={(e) => { if (!sel) e.currentTarget.style.background = "transparent"; }}
                >
                  :{pad(m)}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Done button */}
      <button type="button" className="btn-manor" onClick={onDone}
        style={{ width: "100%", fontSize: 12, height: 32 }}>
        Done
      </button>
    </div>
  );
}
