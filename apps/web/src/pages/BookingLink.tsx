import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import { IconCalendar, IconCheck, IconChevronLeft, IconChevronRight, IconClock, IconExternalLink, IconManorLogo } from "../components/icons";
import { api, ApiError } from "../lib/api";
import type { BookingAvailableSlot, BookingConfirmation } from "../lib/types";

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const CALENDAR_WEEKDAYS = ["M", "T", "W", "T", "F", "S", "S"];

function locationLabel(value: string): string {
  return value.replace("_", " ");
}

function dateKey(slot: BookingAvailableSlot): string {
  return slot.starts_at.slice(0, 10);
}

function parseDateKey(key: string): Date {
  const [year, month, day] = key.split("-").map((part) => Number(part));
  return new Date(year, month - 1, day, 12, 0, 0, 0);
}

function formatDateKeyFromDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function monthKeyFromDateKey(key: string): string {
  return `${key.slice(0, 7)}-01`;
}

function formatMonthLabel(monthKey: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "long",
    year: "numeric",
  }).format(parseDateKey(monthKey));
}

function calendarCells(monthKey: string) {
  const monthDate = parseDateKey(monthKey);
  const firstOfMonth = new Date(monthDate.getFullYear(), monthDate.getMonth(), 1, 12);
  const mondayOffset = (firstOfMonth.getDay() + 6) % 7;
  const start = new Date(firstOfMonth);
  start.setDate(firstOfMonth.getDate() - mondayOffset);

  return Array.from({ length: 42 }, (_, index) => {
    const cellDate = new Date(start);
    cellDate.setDate(start.getDate() + index);
    return {
      key: formatDateKeyFromDate(cellDate),
      day: cellDate.getDate(),
      isCurrentMonth: cellDate.getMonth() === monthDate.getMonth(),
    };
  });
}

function formatWhen(startsAt: string, endsAt: string, timezone: string): string {
  const date = new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
    timeZone: timezone,
  }).format(new Date(startsAt));
  const start = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(startsAt));
  const end = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(endsAt));
  return `${date}, ${start} - ${end}`;
}

export default function BookingLink() {
  const { ownerId, slug = "" } = useParams<{ ownerId?: string; slug: string }>();
  const navigate = useNavigate();
  const [selectedDate, setSelectedDate] = useState("");
  const [selectedSlot, setSelectedSlot] = useState("");
  const [guestName, setGuestName] = useState("");
  const [guestEmail, setGuestEmail] = useState("");
  const [note, setNote] = useState("");
  const [visibleMonth, setVisibleMonth] = useState("");
  const [confirmation, setConfirmation] = useState<BookingConfirmation | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["public-booking-link", ownerId || "", slug],
    queryFn: () => api.calendarSettings.publicBookingLink(slug, ownerId),
    enabled: Boolean(slug),
  });

  const slots = data?.available_slots || [];
  const enabledHours = (data?.working_hours || []).filter((row) => row.enabled);

  useEffect(() => {
    if (!ownerId && data?.owner_id && data.slug) {
      navigate(`/book/u/${data.owner_id}/${data.slug}`, { replace: true });
    }
  }, [data?.owner_id, data?.slug, navigate, ownerId]);

  const groupedSlots = useMemo(() => {
    const groups = new Map<string, BookingAvailableSlot[]>();
    slots.forEach((slot) => {
      const key = dateKey(slot);
      groups.set(key, [...(groups.get(key) || []), slot]);
    });
    return Array.from(groups.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, items]) => ({
        key,
        items: [...items].sort((a, b) => a.starts_at.localeCompare(b.starts_at)),
      }));
  }, [slots]);

  const slotsByDate = useMemo(() => {
    const map = new Map<string, BookingAvailableSlot[]>();
    groupedSlots.forEach((group) => map.set(group.key, group.items));
    return map;
  }, [groupedSlots]);

  const availableMonthKeys = useMemo(
    () => Array.from(new Set(groupedSlots.map((group) => monthKeyFromDateKey(group.key)))),
    [groupedSlots],
  );

  useEffect(() => {
    if (!groupedSlots.length) return;
    const slotExists = slots.some((slot) => slot.starts_at === selectedSlot);
    if (!selectedSlot || !slotExists) {
      const firstGroup = groupedSlots[0];
      setSelectedDate(firstGroup.key);
      setSelectedSlot(firstGroup.items[0]?.starts_at || "");
      setVisibleMonth(monthKeyFromDateKey(firstGroup.key));
      return;
    }
    if (!visibleMonth) {
      setVisibleMonth(monthKeyFromDateKey(selectedDate || groupedSlots[0].key));
    }
  }, [groupedSlots, selectedDate, selectedSlot, slots, visibleMonth]);

  const activeSlots = slotsByDate.get(selectedDate) || [];
  const chosenSlot = slots.find((slot) => slot.starts_at === selectedSlot) || null;
  const currentMonth = visibleMonth || availableMonthKeys[0] || "";
  const currentMonthIndex = availableMonthKeys.indexOf(currentMonth);
  const calendarDays = currentMonth ? calendarCells(currentMonth) : [];

  const selectDate = (key: string) => {
    const items = slotsByDate.get(key);
    if (!items?.length) return;
    setSelectedDate(key);
    setSelectedSlot(items[0].starts_at);
  };

  const moveMonth = (direction: -1 | 1) => {
    if (currentMonthIndex < 0) return;
    const nextMonth = availableMonthKeys[currentMonthIndex + direction];
    if (!nextMonth) return;
    setVisibleMonth(nextMonth);
    const firstGroupInMonth = groupedSlots.find((group) => monthKeyFromDateKey(group.key) === nextMonth);
    if (firstGroupInMonth) {
      setSelectedDate(firstGroupInMonth.key);
      setSelectedSlot(firstGroupInMonth.items[0]?.starts_at || "");
    }
  };

  const bookMutation = useMutation({
    mutationFn: () => {
      if (!chosenSlot) throw new Error("Choose a time");
      return api.calendarSettings.bookPublicBookingLink(slug, {
        starts_at: chosenSlot.starts_at,
        guest_name: guestName.trim(),
        guest_email: guestEmail.trim(),
        note: note.trim() || null,
      }, ownerId);
    },
    onSuccess: (result) => setConfirmation(result),
  });

  const canSubmit = Boolean(chosenSlot && guestName.trim() && guestEmail.trim() && !bookMutation.isPending);

  return (
    <main className="booking-page">
      <header className="booking-brand-bar">
        <a className="booking-brand" href="/" aria-label="Manor AI">
          <span className="booking-brand-mark">
            <IconManorLogo size={16} />
          </span>
          <span>Manor AI</span>
        </a>
      </header>

      <div className="booking-page-body">
        {isLoading && (
          <section className="booking-shell booking-shell--single">
            <div className="booking-empty-state">
              <LoadingSpinner size={18} /> Loading
            </div>
          </section>
        )}

        {!isLoading && (error || !data) && (
          <section className="booking-shell booking-shell--single">
            <div className="booking-empty-state booking-empty-state--stacked">
              <h1>Booking link unavailable</h1>
              <p>This link is disabled or no longer exists.</p>
            </div>
          </section>
        )}

        {!isLoading && data && confirmation && (
          <section className="booking-shell booking-shell--single">
            <div className="booking-confirmation">
              <div className="booking-kicker">
                <IconCheck size={14} /> Confirmed
              </div>
              <h1 className="booking-title">{data.name}</h1>
              <p className="booking-confirmed-time">
                {formatWhen(confirmation.starts_at, confirmation.ends_at, confirmation.timezone)}
              </p>
              <p className="booking-confirmed-copy">
                {confirmation.calendar_event_created
                  ? `Calendar invitation sent to ${confirmation.guest_email}.`
                  : confirmation.email_sent
                    ? `Confirmation email sent to ${confirmation.guest_email}.`
                    : "Your booking is confirmed."}
              </p>
              <div className="booking-confirmed-actions">
                {confirmation.meeting_url && (
                  <a className="btn-manor" href={confirmation.meeting_url} target="_blank" rel="noreferrer">
                    Join meeting
                  </a>
                )}
                {confirmation.calendar_event_url && (
                  <a className="btn-manor-ghost" href={confirmation.calendar_event_url} target="_blank" rel="noreferrer">
                    Open calendar event
                  </a>
                )}
              </div>
            </div>
          </section>
        )}

        {!isLoading && data && !confirmation && (
          <section className="booking-shell">
            <aside className="booking-side">
              <div className="booking-side-main">
                <div className="booking-kicker">
                  <IconCalendar size={14} /> Booking
                </div>
                <h1 className="booking-title">{data.name}</h1>
                <p className="booking-host">
                  {data.owner_name ? `with ${data.owner_name}` : "Manor booking link"}
                </p>
              </div>

              <div className="booking-meta">
                <span className="booking-pill">
                  <IconClock size={14} /> {data.duration_minutes} min
                </span>
                <span className="booking-pill">
                  <IconExternalLink size={14} /> {locationLabel(data.location_type)}
                </span>
                <span className="booking-pill booking-pill--timezone">
                  {data.timezone}
                </span>
              </div>

              {data.description && (
                <p className="booking-description">{data.description}</p>
              )}

              <div>
                <h2 className="booking-section-title">Availability</h2>
                <div className="booking-availability-grid">
                  {enabledHours.map((row) => (
                    <div key={row.day_of_week} className="booking-availability-card">
                      <span>{DAY_LABELS[row.day_of_week]}</span>
                      <strong>{row.start} - {row.end}</strong>
                    </div>
                  ))}
                  {enabledHours.length === 0 && (
                    <div className="booking-muted-line">No availability configured.</div>
                  )}
                </div>
              </div>
            </aside>

            <div className="booking-content">
              <div className="booking-time-section">
                <h2 className="booking-content-title">Select a time</h2>
                {groupedSlots.length === 0 ? (
                  <div className="booking-no-slots">
                    No times available right now.
                  </div>
                ) : (
                  <div className="booking-slot-picker">
                    <div className="booking-calendar-panel">
                      <div className="booking-calendar-header">
                        <button
                          type="button"
                          className="booking-calendar-nav"
                          aria-label="Previous month"
                          disabled={currentMonthIndex <= 0}
                          onClick={() => moveMonth(-1)}
                        >
                          <IconChevronLeft size={14} />
                        </button>
                        <strong>{currentMonth ? formatMonthLabel(currentMonth) : "Available dates"}</strong>
                        <button
                          type="button"
                          className="booking-calendar-nav"
                          aria-label="Next month"
                          disabled={currentMonthIndex < 0 || currentMonthIndex >= availableMonthKeys.length - 1}
                          onClick={() => moveMonth(1)}
                        >
                          <IconChevronRight size={14} />
                        </button>
                      </div>
                      <div className="booking-calendar-weekdays">
                        {CALENDAR_WEEKDAYS.map((label, index) => (
                          <span key={`${label}-${index}`}>{label}</span>
                        ))}
                      </div>
                      <div className="booking-calendar-grid">
                        {calendarDays.map((cell) => {
                          const daySlots = slotsByDate.get(cell.key) || [];
                          const active = cell.key === selectedDate;
                          const available = daySlots.length > 0;
                          return (
                            <button
                              key={cell.key}
                              type="button"
                              onClick={() => selectDate(cell.key)}
                              aria-pressed={active}
                              disabled={!available}
                              className={`booking-calendar-day${active ? " is-active" : ""}${cell.isCurrentMonth ? "" : " is-outside"}${available ? " is-available" : ""}`}
                              aria-label={available ? `${cell.key}, ${daySlots.length} available times` : `${cell.key}, no available times`}
                            >
                              <span>{cell.day}</span>
                              {available && <small>{daySlots.length}</small>}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                    <div className="booking-slot-grid">
                      {activeSlots.map((slot) => {
                        const active = slot.starts_at === selectedSlot;
                        return (
                          <button
                            key={slot.starts_at}
                            type="button"
                            onClick={() => setSelectedSlot(slot.starts_at)}
                            aria-pressed={active}
                            className={`booking-slot-button${active ? " is-active" : ""}`}
                          >
                            {slot.label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              <div className="booking-form">
                <div className="booking-form-header">
                  <h2 className="booking-content-title">Your details</h2>
                  {chosenSlot && (
                    <div className="booking-selected-time">
                      <span>Selected</span>
                      <strong>{formatWhen(chosenSlot.starts_at, chosenSlot.ends_at, data.timezone)}</strong>
                    </div>
                  )}
                </div>
                <div>
                  <label className="manor-label">Name</label>
                  <input className="manor-input" value={guestName} onChange={(event) => setGuestName(event.target.value)} placeholder="Jane Doe" />
                </div>
                <div>
                  <label className="manor-label">Email</label>
                  <input className="manor-input" value={guestEmail} onChange={(event) => setGuestEmail(event.target.value)} placeholder="jane@example.com" />
                </div>
                <div>
                  <label className="manor-label">Note</label>
                  <textarea className="manor-input" value={note} onChange={(event) => setNote(event.target.value)} rows={3} placeholder="Optional" style={{ resize: "vertical", paddingTop: 10 }} />
                </div>
                {bookMutation.error && (
                  <div className="booking-error-text">
                    {bookMutation.error instanceof ApiError ? bookMutation.error.message : "Could not book this time"}
                  </div>
                )}
                <button className="btn-manor booking-submit" type="button" disabled={!canSubmit} onClick={() => bookMutation.mutate()}>
                  {bookMutation.isPending ? "Booking..." : "Book meeting"}
                </button>
              </div>
            </div>
          </section>
        )}
      </div>

      <footer className="booking-footer">
        <span>2026 Manor AI LLC</span>
      </footer>
    </main>
  );
}
