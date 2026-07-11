import { useState, useMemo } from "react";
import { IconChevronLeft, IconChevronRight, IconClock, IconExternalLink, IconFlag, IconLink, IconMail, IconManorLogo, IconUser } from "../icons";
import AgentAvatar from "./AgentAvatar";
import { getLocale, t } from "../../lib/i18n";
import { isDeadlineOverdue } from "../../lib/format";

/* ── types ─────────────────────────────────────────────── */

export interface CalendarEvent {
  date: string;
  title: string;
  id: string;
  source?: "task" | "booking" | "external";
  priority?: number;
  status?: string;
  scheduled_at?: string;
  duration_minutes?: number;
  timezone?: string;
  agent_name?: string;
  assignee_name?: string;
  agent_avatar?: string;
  assignee_avatar?: string;
  agent_type?: string;
  agent_id?: string;
  guest_name?: string;
  guest_email?: string;
  note?: string | null;
  location_label?: string | null;
  booking_link_id?: string;
  booking_link_slug?: string;
  calendar_event_url?: string | null;
  meeting_url?: string | null;
  external_provider?: string;
  external_event_id?: string;
  calendar_id?: string;
  calendar_name?: string | null;
  all_day?: boolean;
  description?: string | null;
  organizer_email?: string | null;
  attendee_count?: number | null;
}

export interface CalendarProps {
  month: Date;
  events: CalendarEvent[];
  onDateClick?: (date: Date) => void;
  onEventClick?: (event: CalendarEvent) => void;
  onMonthChange?: (date: Date) => void;
}

/* ── helpers ────────────────────────────────────────────── */

const PRIORITY_COLORS: Record<number, string> = {
  5: "#d65f59", 4: "#d3873f", 3: "#c3a63f", 2: "#8aa9d1", 1: "#a8a29e",
};
const STATUS_COLORS: Record<string, string> = {
  completed: "#4f9c84", in_progress: "#4869ac", pending: "#cf9b44",
  created: "#a8a29e", scheduled: "#5f84bd", waiting_on_customer: "#d3873f",
  on_hold: "#a07fc0", blocked: "#d65f59", cancelled: "#78716c", failed: "#c14a44",
};

function monthLabel(month: number): string {
  return new Intl.DateTimeFormat(getLocale(), { month: "long" }).format(new Date(2026, month, 1));
}

function priorityLabel(priority: number): string {
  const key = {
    5: "critical",
    4: "high",
    3: "medium",
    2: "low",
    1: "minimal",
  }[priority] || "medium";
  return t(`component.priority.${key}`);
}

function statusLabel(status = "pending"): string {
  const key = status === "completed" ? "done" : status;
  const translationKey = `component.status.${key}`;
  const translated = t(translationKey);
  return translated === translationKey ? status.replace(/_/g, " ") : translated;
}

function isSameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}
function getDaysInMonth(y: number, m: number) { return new Date(y, m + 1, 0).getDate(); }
function getStartDow(y: number, m: number) { const d = new Date(y, m, 1).getDay(); return d === 0 ? 6 : d - 1; }
function pillColor(ev: CalendarEvent) {
  if (ev.source === "booking") return "#4f7d75";
  if (ev.source === "external") return "#4869ac";
  if (ev.status && STATUS_COLORS[ev.status]) return STATUS_COLORS[ev.status];
  if (ev.priority && PRIORITY_COLORS[ev.priority]) return PRIORITY_COLORS[ev.priority];
  return "#4f7d75";
}
function formatDayLabel(d: Date) {
  return d.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" });
}

function eventStartValue(ev: CalendarEvent): string {
  return ev.scheduled_at || ev.date;
}

function eventDateKey(ev: CalendarEvent): string {
  const value = eventStartValue(ev);
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.slice(0, 10);
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: ev.timezone || undefined,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(parsed);
    const year = parts.find((part) => part.type === "year")?.value;
    const month = parts.find((part) => part.type === "month")?.value;
    const day = parts.find((part) => part.type === "day")?.value;
    if (year && month && day) return `${year}-${month}-${day}`;
  } catch {
    // Fall back to local browser time when an unavailable IANA timezone slips in.
  }
  return `${parsed.getFullYear()}-${String(parsed.getMonth() + 1).padStart(2, "0")}-${String(parsed.getDate()).padStart(2, "0")}`;
}

function formatEventTime(ev: CalendarEvent): string | null {
  if (ev.all_day) return "All day";
  const parsed = new Date(eventStartValue(ev));
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: ev.timezone || undefined,
  });
}

function formatEventEndTime(ev: CalendarEvent): string | null {
  if (ev.all_day || !ev.duration_minutes) return null;
  const parsed = new Date(eventStartValue(ev));
  if (Number.isNaN(parsed.getTime())) return null;
  return new Date(parsed.getTime() + ev.duration_minutes * 60000).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: ev.timezone || undefined,
  });
}

function formatEventDateTime(ev: CalendarEvent): string {
  const parsed = new Date(eventStartValue(ev));
  const startStr = formatEventTime(ev);
  const endStr = formatEventEndTime(ev);
  if (Number.isNaN(parsed.getTime())) return startStr || eventStartValue(ev);
  const dateStr = parsed.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: ev.timezone || undefined,
  });
  return `${dateStr}${startStr ? `, ${startStr}` : ""}${endStr ? ` - ${endStr}` : ""}`;
}

function externalProviderLabel(provider?: string): string {
  if (provider === "google_calendar") return "Google Calendar";
  if (provider === "ms_calendar") return "Microsoft Calendar";
  return "Calendar";
}

/* ── component ──────────────────────────────────────────── */

export default function Calendar({ month, events, onDateClick, onEventClick, onMonthChange }: CalendarProps) {
  const [selectedDate, setSelectedDate] = useState<Date | null>(null);
  const [hoveredDay, setHoveredDay] = useState<number | null>(null);
  const [selectedEventDetail, setSelectedEventDetail] = useState<CalendarEvent | null>(null);

  const today = useMemo(() => new Date(), []);
  const year = month.getFullYear();
  const mo = month.getMonth();
  const daysInMonth = getDaysInMonth(year, mo);
  const startDay = getStartDow(year, mo);
  const prevDays = getDaysInMonth(year, mo - 1);
  const dayHeaders = useMemo(
    () =>
      [1, 2, 3, 4, 5, 6, 0].map((day) =>
        new Intl.DateTimeFormat(getLocale(), { weekday: "short" }).format(new Date(2026, 0, 4 + day)),
      ),
    [],
  );

  const cells = useMemo(() => {
    const result: { day: number; inMonth: boolean; date: Date }[] = [];
    for (let i = startDay - 1; i >= 0; i--) result.push({ day: prevDays - i, inMonth: false, date: new Date(year, mo - 1, prevDays - i) });
    for (let d = 1; d <= daysInMonth; d++) result.push({ day: d, inMonth: true, date: new Date(year, mo, d) });
    const rem = 7 - (result.length % 7);
    if (rem < 7) for (let d = 1; d <= rem; d++) result.push({ day: d, inMonth: false, date: new Date(year, mo + 1, d) });
    return result;
  }, [year, mo, daysInMonth, startDay, prevDays]);
  const rowCount = Math.ceil(cells.length / 7);
  const cellHeight = rowCount > 5 ? 74 : 82;

  const eventsByDate = useMemo(() => {
    const m: Record<string, CalendarEvent[]> = {};
    for (const ev of events) {
      const k = eventDateKey(ev);
      (m[k] ??= []).push(ev);
    }
    for (const dayEvents of Object.values(m)) {
      dayEvents.sort((a, b) => eventStartValue(a).localeCompare(eventStartValue(b)));
    }
    return m;
  }, [events]);

  const handleDateClick = (date: Date) => { setSelectedDate(date); setSelectedEventDetail(null); onDateClick?.(date); };
  const handleEventClick = (ev: CalendarEvent, date?: Date) => {
    if (ev.source === "booking" || ev.source === "external") {
      if (date) setSelectedDate(date);
      setSelectedEventDetail(ev);
      return;
    }
    onEventClick?.(ev);
  };
  const goToday = () => {
    onMonthChange?.(new Date(today.getFullYear(), today.getMonth(), 1));
    setSelectedDate(today);
    setSelectedEventDetail(null);
  };

  // Selected day's events
  const selKey = selectedDate ? `${selectedDate.getFullYear()}-${String(selectedDate.getMonth() + 1).padStart(2, "0")}-${String(selectedDate.getDate()).padStart(2, "0")}` : null;
  const selEvents = selKey ? eventsByDate[selKey] ?? [] : [];

  // Stats
  const totalEvents = events.length;
  const bookingCount = events.filter((e) => e.source === "booking").length;
  const externalCount = events.filter((e) => e.source === "external").length;
  const taskCount = totalEvents - bookingCount - externalCount;
  const overdueCount = events.filter((e) => (!e.source || e.source === "task") && isDeadlineOverdue(e.date, e.status, today)).length;

  return (
    <div style={{ display: "flex", gap: 20, alignItems: "flex-start", minWidth: 0 }}>
      {/* ── Calendar grid ── */}
      <div style={{
        flex: 1, minWidth: 0, background: "rgba(255,255,255,0.85)", backdropFilter: "blur(16px)",
        borderRadius: 24, border: "1px solid rgba(28,25,23,0.06)",
        boxShadow: "0 8px 24px rgba(0,0,0,0.04)", padding: "20px 24px",
      }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button onClick={() => onMonthChange?.(new Date(year, mo - 1, 1))}
              style={{ width: 32, height: 32, borderRadius: 10, border: "1px solid rgba(28,25,23,0.06)", background: "#fff", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", color: "#78716c" }}>
              <IconChevronLeft size={14} />
            </button>
            <h2 style={{ fontSize: 18, fontWeight: 800, color: "#1c1917", margin: 0, minWidth: 180, textAlign: "center" }}>
              {monthLabel(mo)} {year}
            </h2>
            <button onClick={() => onMonthChange?.(new Date(year, mo + 1, 1))}
              style={{ width: 32, height: 32, borderRadius: 10, border: "1px solid rgba(28,25,23,0.06)", background: "#fff", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", color: "#78716c" }}>
              <IconChevronRight size={14} />
            </button>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {/* Stats pills */}
            <span style={{ fontSize: 11, fontWeight: 600, color: "#57534e", padding: "4px 10px", borderRadius: 20, background: "#f5f5f4" }}>
              {taskCount} task{taskCount !== 1 ? "s" : ""}
              {bookingCount ? ` · ${bookingCount} booking${bookingCount !== 1 ? "s" : ""}` : ""}
              {externalCount ? ` · ${externalCount} calendar` : ""}
            </span>
            {overdueCount > 0 && (
              <span style={{ fontSize: 11, fontWeight: 700, color: "#c14a44", padding: "4px 10px", borderRadius: 20, background: "#f8f0ef", display: "flex", alignItems: "center", gap: 4 }}>
                <IconClock size={10} /> {overdueCount} overdue
              </span>
            )}
            <button onClick={goToday}
              style={{ fontSize: 12, fontWeight: 600, color: "#436b65", padding: "5px 14px", borderRadius: 10, border: "1px solid rgba(28,25,23,0.06)", background: "#fff", cursor: "pointer" }}>
              Today
            </button>
          </div>
        </div>

        {/* Day headers */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, minmax(0, 1fr))", gap: 0, marginBottom: 2 }}>
          {dayHeaders.map((d) => (
            <div key={d} style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: "#a8a29e", textAlign: "center", padding: "6px 0" }}>
              {d}
            </div>
          ))}
        </div>

        {/* Grid */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(7, minmax(0, 1fr))",
          gridAutoRows: cellHeight,
          gap: 0,
          overflow: "hidden",
          border: "1px solid rgba(231,229,228,0.9)",
          borderRadius: 14,
          background: "#fff",
        }}>
          {cells.map((cell, idx) => {
            const dateKey = `${cell.date.getFullYear()}-${String(cell.date.getMonth() + 1).padStart(2, "0")}-${String(cell.date.getDate()).padStart(2, "0")}`;
            const dayEvents = eventsByDate[dateKey] ?? [];
            const isToday = isSameDay(cell.date, today);
            const isSelected = selectedDate && isSameDay(cell.date, selectedDate);
            const isHovered = hoveredDay === idx;
            const hasOverdue = dayEvents.some((e) => (!e.source || e.source === "task") && isDeadlineOverdue(e.date, e.status, today));
            const isLastColumn = (idx + 1) % 7 === 0;
            const isLastRow = idx >= cells.length - 7;

            return (
              <div key={idx} onClick={() => handleDateClick(cell.date)}
                onMouseEnter={() => setHoveredDay(idx)} onMouseLeave={() => setHoveredDay(null)}
                style={{
                  height: "100%", minWidth: 0, boxSizing: "border-box", overflow: "hidden",
                  padding: "4px 6px", cursor: "pointer", position: "relative",
                  transition: "background 0.12s, box-shadow 0.12s",
                  background: !cell.inMonth ? "rgba(250,250,249,0.3)" : isSelected ? "rgba(79,125,117,0.06)" : isHovered ? "rgba(245,245,244,0.8)" : "transparent",
                  borderRight: isLastColumn ? "none" : "1px solid rgba(231,229,228,0.9)",
                  borderBottom: isLastRow ? "none" : "1px solid rgba(231,229,228,0.9)",
                  boxShadow: isSelected
                    ? "inset 0 0 0 2px #4f7d75"
                    : isToday
                      ? "inset 0 0 0 1px rgba(79,125,117,0.32)"
                      : "none",
                }}>
                {/* Day number */}
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 2, minWidth: 0 }}>
                  <span style={{
                    width: 22, height: 22, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center",
                    fontSize: 12, fontWeight: isToday ? 800 : 600,
                    color: !cell.inMonth ? "#d6d3d1" : isToday ? "#fff" : "#44403c",
                    background: isToday ? "#436b65" : "transparent",
                  }}>
                    {cell.day}
                  </span>
                  {dayEvents.length > 0 && (
                    <span style={{ fontSize: 9, fontWeight: 700, color: hasOverdue ? "#d65f59" : "#a8a29e", minWidth: 14, textAlign: "right" }}>
                      {dayEvents.length}
                    </span>
                  )}
                </div>

                {/* Event dots/pills */}
                <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0, overflow: "hidden" }}>
                  {dayEvents.slice(0, 3).map((ev) => {
                    const rawTime = formatEventTime(ev);
                    const time = ev.all_day ? rawTime : rawTime?.replace(" ", "");
                    const canOpen = ev.source === "booking" || ev.source === "external" || Boolean(onEventClick);
                    return (
                      <div key={ev.id} onClick={(e) => { e.stopPropagation(); if (canOpen) handleEventClick(ev, cell.date); }}
                        style={{
                          fontSize: 9, fontWeight: 500, color: "#fff", lineHeight: "14px",
                          background: pillColor(ev), borderRadius: 4, padding: "1px 5px",
                          minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", cursor: canOpen ? "pointer" : "default",
                        }}>
                        {time && <span style={{ fontWeight: 700, marginRight: 3 }}>{time}</span>}
                        {ev.title}
                      </div>
                    );
                  })}
                  {dayEvents.length > 3 && (
                    <span style={{ fontSize: 9, fontWeight: 600, color: "#78716c", paddingLeft: 4 }}>+{dayEvents.length - 3}</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── Sidebar: selected day detail ── */}
      <div style={{
        width: 300, flexShrink: 0,
        background: "rgba(255,255,255,0.85)", backdropFilter: "blur(16px)",
        borderRadius: 20, border: "1px solid rgba(28,25,23,0.06)",
        boxShadow: "0 4px 16px rgba(0,0,0,0.03)", padding: "16px 18px",
        position: "sticky", top: 20,
      }}>
        <h3 style={{ fontSize: 14, fontWeight: 700, color: "#1c1917", margin: "0 0 12px" }}>
          {selectedDate ? formatDayLabel(selectedDate) : t("component.calendar.select_a_day")}
        </h3>

        {!selectedDate && (
          <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>{t("component.calendar.click_on_day")}</p>
        )}

        {selectedDate && selEvents.length === 0 && (
          <div style={{ textAlign: "center", padding: "24px 0" }}>
            <p style={{ fontSize: 12, color: "#a8a29e", margin: "0 0 8px" }}>{t("component.calendar.no_tasks_due")}</p>
            <button onClick={() => onDateClick?.(selectedDate)}
              style={{ fontSize: 12, fontWeight: 600, color: "#436b65", background: "none", border: "none", cursor: "pointer", textDecoration: "underline" }}>
              + {t("component.calendar.create_task")}
            </button>
          </div>
        )}

        {selEvents.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {selEvents.map((ev) => {
              const isBooking = ev.source === "booking";
              const isExternal = ev.source === "external";
              const isActiveEvent = (isBooking || isExternal) && selectedEventDetail?.id === ev.id;
              const pColor = PRIORITY_COLORS[ev.priority ?? 3] || "#a8a29e";
              const sColor = STATUS_COLORS[ev.status ?? "pending"] || "#a8a29e";
              const sLabel = statusLabel(ev.status);
              const isMaster = ev.agent_type === "manor_agent" || ev.agent_id === "manor-master";
              const avatar = ev.agent_name ? ev.agent_avatar : ev.assignee_avatar;
              const name = ev.agent_name || ev.assignee_name;
              const startStr = formatEventTime(ev);
              const endStr = formatEventEndTime(ev);
              const canOpen = isBooking || isExternal || Boolean(onEventClick);
              const accentColor = isBooking ? "#4f7d75" : isExternal ? "#4869ac" : pColor;

              return (
                <div key={ev.id} onClick={() => { if (canOpen) handleEventClick(ev); }}
                  style={{
                    padding: "10px 12px", borderRadius: 12, cursor: canOpen ? "pointer" : "default",
                    border: isActiveEvent ? `1px solid ${isExternal ? "rgba(72,105,172,0.28)" : "rgba(79,125,117,0.28)"}` : "1px solid rgba(28,25,23,0.06)",
                    background: isActiveEvent ? (isExternal ? "rgba(247,249,253,0.98)" : "rgba(247,250,249,0.98)") : "#fff",
                    transition: "all 0.15s", borderLeft: `3px solid ${accentColor}`,
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.boxShadow = "0 4px 12px rgba(0,0,0,0.06)"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.boxShadow = "none"; }}
                >
                  {/* Time + Title */}
                  {startStr && (
                    <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 4 }}>
                      <IconClock size={10} style={{ color: "#78716c" }} />
                      <span style={{ fontSize: 11, fontWeight: 600, color: "#57534e" }}>
                        {startStr}{endStr ? ` – ${endStr}` : ""}{ev.duration_minutes ? ` (${ev.duration_minutes}min)` : ""}
                      </span>
                    </div>
                  )}
                  <p style={{ fontSize: 13, fontWeight: 600, color: "#1c1917", margin: "0 0 6px", lineHeight: 1.3 }}>{ev.title}</p>

                  {/* Tags */}
                  <div style={{ display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap", marginBottom: 6 }}>
                    {isBooking ? (
                      <>
                        <span style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 9, fontWeight: 700, color: "#4f7d75", padding: "2px 6px", borderRadius: 20, background: "rgba(79,125,117,0.1)" }}>
                          Booking
                        </span>
                        {ev.guest_email && (
                          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 10, color: "#78716c", fontWeight: 600 }}>
                            {ev.guest_email}
                          </span>
                        )}
                      </>
                    ) : isExternal ? (
                      <>
                        <span style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 9, fontWeight: 700, color: "#4869ac", padding: "2px 6px", borderRadius: 20, background: "rgba(72,105,172,0.1)" }}>
                          Calendar
                        </span>
                        {(ev.calendar_name || ev.location_label) && (
                          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 10, color: "#78716c", fontWeight: 600 }}>
                            {ev.calendar_name || ev.location_label}
                          </span>
                        )}
                      </>
                    ) : (
                      <>
                        <span style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 9, fontWeight: 700, color: pColor, padding: "2px 6px", borderRadius: 20, background: `${pColor}0d` }}>
                          <IconFlag size={7} /> {priorityLabel(ev.priority ?? 3)}
                        </span>
                        <span style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 9, fontWeight: 600, color: sColor, padding: "2px 6px", borderRadius: 20, background: `${sColor}0d` }}>
                          <span style={{ width: 5, height: 5, borderRadius: "50%", background: sColor }} />
                          {sLabel}
                        </span>
                      </>
                    )}
                  </div>

                  {/* Assignee */}
                  {!isBooking && !isExternal && (name || isMaster) && (
                    <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
                      {isMaster ? (
                        <span style={{ width: 16, height: 16, borderRadius: "50%", background: "linear-gradient(135deg, #436b65, #5f928a)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                          <IconManorLogo size={8} style={{ color: "#fff" }} />
                        </span>
                      ) : ev.agent_name ? (
                        <AgentAvatar
                          name={ev.agent_name}
                          avatarUrl={ev.agent_avatar}
                          seed={ev.agent_id || ev.id}
                          size={16}
                        />
                      ) : avatar ? (
                        <img src={avatar} alt="" style={{ width: 16, height: 16, borderRadius: "50%", objectFit: "cover" }} />
                      ) : (
                        <span style={{ width: 16, height: 16, borderRadius: "50%", background: ev.agent_name ? "linear-gradient(135deg,#dceae3,#c4dfd2)" : "linear-gradient(135deg,#e8eff4,#ddd6fe)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                          <IconUser size={8} style={{ color: "#57534e" }} />
                        </span>
                      )}
                      <span style={{ fontSize: 10, color: "#78716c", fontWeight: 500 }}>{isMaster ? "Manor AI" : name}</span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {selectedEventDetail && (() => {
          const isExternal = selectedEventDetail.source === "external";
          const isBooking = selectedEventDetail.source === "booking";
          const detailAccent = isExternal ? "#4869ac" : "#4f7d75";
          const detailBorder = isExternal ? "rgba(72,105,172,0.18)" : "rgba(79,125,117,0.18)";
          const detailBg = isExternal ? "rgba(247,249,253,0.96)" : "rgba(247,250,249,0.96)";
          const description = selectedEventDetail.description?.replace(/<[^>]+>/g, "").trim();
          return (
            <div
              style={{
                marginTop: 12,
                padding: "12px",
                borderRadius: 14,
                border: `1px solid ${detailBorder}`,
                background: detailBg,
                boxShadow: `0 8px 20px ${isExternal ? "rgba(72,105,172,0.08)" : "rgba(79,125,117,0.08)"}`,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "flex-start", marginBottom: 10 }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 10, fontWeight: 800, color: detailAccent, textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 4 }}>
                    {isExternal ? "Calendar event" : "Booking details"}
                  </div>
                  <div style={{ fontSize: 13, fontWeight: 800, color: "#1c1917", lineHeight: 1.25, overflowWrap: "anywhere" }}>
                    {selectedEventDetail.title}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setSelectedEventDetail(null)}
                  style={{ border: "none", background: "transparent", color: "#a8a29e", cursor: "pointer", fontSize: 16, lineHeight: 1, padding: 0 }}
                  aria-label="Close event details"
                >
                  x
                </button>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                  <IconClock size={13} style={{ color: detailAccent, marginTop: 1 }} />
                  <span style={{ fontSize: 12, fontWeight: 700, color: "#57534e", lineHeight: 1.35 }}>
                    {formatEventDateTime(selectedEventDetail)}
                  </span>
                </div>
                {isBooking && selectedEventDetail.guest_email && (
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-start", minWidth: 0 }}>
                    <IconMail size={13} style={{ color: detailAccent, marginTop: 1, flexShrink: 0 }} />
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#57534e", lineHeight: 1.35, overflowWrap: "anywhere" }}>
                      {selectedEventDetail.guest_name ? `${selectedEventDetail.guest_name} · ` : ""}{selectedEventDetail.guest_email}
                    </span>
                  </div>
                )}
                {isExternal && (selectedEventDetail.calendar_name || selectedEventDetail.external_provider) && (
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-start", minWidth: 0 }}>
                    <IconLink size={13} style={{ color: detailAccent, marginTop: 1, flexShrink: 0 }} />
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#57534e", lineHeight: 1.35, overflowWrap: "anywhere" }}>
                      {selectedEventDetail.calendar_name || externalProviderLabel(selectedEventDetail.external_provider)}
                    </span>
                  </div>
                )}
                {isExternal && selectedEventDetail.organizer_email && (
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-start", minWidth: 0 }}>
                    <IconMail size={13} style={{ color: detailAccent, marginTop: 1, flexShrink: 0 }} />
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#57534e", lineHeight: 1.35, overflowWrap: "anywhere" }}>
                      {selectedEventDetail.organizer_email}
                    </span>
                  </div>
                )}
                {selectedEventDetail.location_label && !selectedEventDetail.meeting_url && (
                  <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                    <IconLink size={13} style={{ color: detailAccent, marginTop: 1 }} />
                    <span style={{ fontSize: 12, fontWeight: 700, color: "#57534e", lineHeight: 1.35, overflowWrap: "anywhere" }}>
                      {selectedEventDetail.location_label}
                    </span>
                  </div>
                )}
                {isBooking && selectedEventDetail.note && (
                  <div style={{ fontSize: 12, color: "#57534e", lineHeight: 1.45, padding: "8px 9px", borderRadius: 10, background: "#fff", border: "1px solid rgba(231,229,228,0.9)" }}>
                    {selectedEventDetail.note}
                  </div>
                )}
                {isExternal && description && (
                  <div style={{ fontSize: 12, color: "#57534e", lineHeight: 1.45, padding: "8px 9px", borderRadius: 10, background: "#fff", border: "1px solid rgba(231,229,228,0.9)", maxHeight: 96, overflow: "auto" }}>
                    {description}
                  </div>
                )}
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: 7, marginTop: 12 }}>
                {selectedEventDetail.meeting_url ? (
                  <a
                    href={selectedEventDetail.meeting_url}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 7,
                      height: 32,
                      borderRadius: 10,
                      background: detailAccent,
                      color: "#fff",
                      fontSize: 12,
                      fontWeight: 800,
                      textDecoration: "none",
                    }}
                  >
                    <IconExternalLink size={13} /> Open meeting
                  </a>
                ) : (
                  <div style={{ display: "flex", alignItems: "center", gap: 7, padding: "8px 9px", borderRadius: 10, background: "#fff", border: "1px solid rgba(231,229,228,0.9)", color: "#78716c", fontSize: 12, fontWeight: 700 }}>
                    <IconLink size={13} /> No meeting link saved
                  </div>
                )}
                {selectedEventDetail.calendar_event_url && (
                  <a
                    href={selectedEventDetail.calendar_event_url}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 7,
                      height: 32,
                      borderRadius: 10,
                      border: `1px solid ${isExternal ? "rgba(72,105,172,0.22)" : "rgba(79,125,117,0.22)"}`,
                      color: isExternal ? "#4869ac" : "#436b65",
                      background: "#fff",
                      fontSize: 12,
                      fontWeight: 800,
                      textDecoration: "none",
                    }}
                  >
                    <IconExternalLink size={13} /> Calendar event
                  </a>
                )}
              </div>
            </div>
          );
        })()}
      </div>
    </div>
  );
}
