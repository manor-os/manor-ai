import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import Select from "../components/ui/Select";
import { IconCalendar, IconCheck, IconChevronLeft, IconChevronRight, IconClock, IconExternalLink, IconManorLogo } from "../components/icons";
import { api, ApiError } from "../lib/api";
import type { BookingAvailableSlot, BookingConfirmation } from "../lib/types";

const CALENDAR_WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];
const AVAILABILITY_REFRESH_INTERVAL_MS = 30_000;

function locationLabel(value: string): string {
  return value.replace("_", " ");
}

function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

function dateKeyFromInstant(value: string | Date, timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: timezone,
  }).formatToParts(new Date(value));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function dateKey(slot: BookingAvailableSlot, timezone: string): string {
  return dateKeyFromInstant(slot.starts_at, timezone);
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

function monthKeyFromInstant(value: string | Date, timezone: string): string {
  return monthKeyFromDateKey(dateKeyFromInstant(value, timezone));
}

function monthKeysInRange(startsAt: string, endsAt: string, timezone: string): string[] {
  const first = parseDateKey(monthKeyFromInstant(startsAt, timezone));
  const lastInstant = new Date(Math.max(new Date(endsAt).getTime() - 1, 0));
  const last = parseDateKey(monthKeyFromInstant(lastInstant, timezone));
  const months: string[] = [];
  const cursor = new Date(first);
  while (cursor <= last) {
    months.push(formatDateKeyFromDate(cursor).slice(0, 7) + "-01");
    cursor.setMonth(cursor.getMonth() + 1, 1);
  }
  return months;
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
  const sundayOffset = firstOfMonth.getDay();
  const start = new Date(firstOfMonth);
  start.setDate(firstOfMonth.getDate() - sundayOffset);

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

function formatSlotLabel(startsAt: string, endsAt: string, timezone: string): string {
  const formatter = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  });
  return `${formatter.format(new Date(startsAt))} - ${formatter.format(new Date(endsAt))}`;
}

function timeZoneOptions(...preferred: string[]) {
  const intlWithSupportedValues = Intl as typeof Intl & {
    supportedValuesOf?: (key: "timeZone") => string[];
  };
  const supported = intlWithSupportedValues.supportedValuesOf?.("timeZone") || [];
  const values = Array.from(new Set([...preferred.filter(Boolean), ...supported]));
  return values.map((value) => ({
    value,
    label: value.replaceAll("_", " "),
  }));
}

export default function BookingLink() {
  const { ownerId, slug = "" } = useParams<{ ownerId?: string; slug: string }>();
  const navigate = useNavigate();
  const [selectedDate, setSelectedDate] = useState("");
  const [selectedSlot, setSelectedSlot] = useState("");
  const [guestName, setGuestName] = useState("");
  const [guestEmail, setGuestEmail] = useState("");
  const [note, setNote] = useState("");
  const [visibleMonth, setVisibleMonth] = useState(() =>
    monthKeyFromInstant(new Date(), browserTimeZone() || "UTC"),
  );
  const [confirmation, setConfirmation] = useState<BookingConfirmation | null>(null);
  const [viewerTimezone, setViewerTimezone] = useState(browserTimeZone);
  const [availabilityChanged, setAvailabilityChanged] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["public-booking-link", ownerId || "", slug, visibleMonth, viewerTimezone],
    queryFn: () => api.calendarSettings.publicBookingLink(
      slug,
      ownerId,
      visibleMonth.slice(0, 7),
      viewerTimezone || undefined,
    ),
    enabled: Boolean(slug && visibleMonth),
    staleTime: 0,
    refetchInterval: confirmation ? false : AVAILABILITY_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: !confirmation,
  });
  const availabilityUnavailable = error instanceof ApiError && error.status === 503;

  const slots = data?.available_slots || [];
  const displayTimezone = viewerTimezone || data?.timezone || "UTC";
  const timezoneOptions = useMemo(
    () => timeZoneOptions(displayTimezone, data?.timezone || ""),
    [data?.timezone, displayTimezone],
  );

  useEffect(() => {
    if (!viewerTimezone && data?.timezone) {
      setViewerTimezone(data.timezone);
      setVisibleMonth(monthKeyFromInstant(new Date(), data.timezone));
    }
  }, [data?.timezone, viewerTimezone]);

  useEffect(() => {
    if (!ownerId && data?.owner_id && data.slug) {
      navigate(`/book/u/${data.owner_id}/${data.slug}`, { replace: true });
    }
  }, [data?.owner_id, data?.slug, navigate, ownerId]);

  const groupedSlots = useMemo(() => {
    const groups = new Map<string, BookingAvailableSlot[]>();
    slots.forEach((slot) => {
      const key = dateKey(slot, displayTimezone);
      groups.set(key, [...(groups.get(key) || []), slot]);
    });
    return Array.from(groups.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, items]) => ({
        key,
        items: [...items].sort((a, b) => a.starts_at.localeCompare(b.starts_at)),
      }));
  }, [displayTimezone, slots]);

  const slotsByDate = useMemo(() => {
    const map = new Map<string, BookingAvailableSlot[]>();
    groupedSlots.forEach((group) => map.set(group.key, group.items));
    return map;
  }, [groupedSlots]);

  const availableMonthKeys = useMemo(() => {
    if (!data) return [];
    if (data.availability_range_start && data.availability_range_end) {
      return monthKeysInRange(
        data.availability_range_start,
        data.availability_range_end,
        displayTimezone,
      );
    }
    return Array.from(new Set(
      groupedSlots.map((group) => monthKeyFromDateKey(group.key)),
    ));
  }, [data, displayTimezone, groupedSlots]);

  useEffect(() => {
    if (!groupedSlots.length) {
      if (selectedSlot) {
        setSelectedSlot("");
        setSelectedDate("");
        setAvailabilityChanged(true);
      }
      return;
    }
    const slotExists = slots.some((slot) => slot.starts_at === selectedSlot);
    if (selectedSlot && !slotExists) {
      setSelectedSlot("");
      setSelectedDate("");
      setAvailabilityChanged(true);
      return;
    }
    if (!selectedSlot && !availabilityChanged) {
      const firstGroup = groupedSlots[0];
      setSelectedDate(firstGroup.key);
      setSelectedSlot(firstGroup.items[0]?.starts_at || "");
      return;
    }
  }, [availabilityChanged, groupedSlots, selectedSlot, slots]);

  const activeSlots = slotsByDate.get(selectedDate) || [];
  const chosenSlot = slots.find((slot) => slot.starts_at === selectedSlot) || null;
  const currentMonth = visibleMonth;
  const currentMonthIndex = availableMonthKeys.indexOf(currentMonth);
  const calendarDays = currentMonth ? calendarCells(currentMonth) : [];

  const bookMutation = useMutation({
    mutationFn: () => {
      if (!chosenSlot) throw new Error("Choose a time");
      return api.calendarSettings.bookPublicBookingLink(slug, {
        starts_at: chosenSlot.starts_at,
        guest_name: guestName.trim(),
        guest_email: guestEmail.trim(),
        note: note.trim() || null,
        timezone: displayTimezone,
      }, ownerId);
    },
  });

  const selectSlot = (startsAt: string) => {
    setSelectedSlot(startsAt);
    setAvailabilityChanged(false);
    bookMutation.reset();
  };

  const selectDate = (key: string) => {
    const items = slotsByDate.get(key);
    if (!items?.length) return;
    setSelectedDate(key);
    selectSlot(items[0].starts_at);
  };

  const moveMonth = (direction: -1 | 1) => {
    if (currentMonthIndex < 0) return;
    const nextMonth = availableMonthKeys[currentMonthIndex + direction];
    if (!nextMonth) return;
    setVisibleMonth(nextMonth);
    setSelectedDate("");
    setSelectedSlot("");
    setAvailabilityChanged(false);
    bookMutation.reset();
  };

  const submitBooking = async () => {
    try {
      const result = await bookMutation.mutateAsync();
      setConfirmation(result);
    } catch (mutationError) {
      if (!(mutationError instanceof ApiError) || mutationError.status !== 409) return;
      setSelectedSlot("");
      setAvailabilityChanged(true);
      await refetch();
    }
  };

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

        {!isLoading && !confirmation && (error || !data) && (
          <section className="booking-shell booking-shell--single">
            <div className="booking-empty-state booking-empty-state--stacked">
              <h1>{availabilityUnavailable ? "Availability temporarily unavailable" : "Booking link unavailable"}</h1>
              <p>
                {availabilityUnavailable
                  ? "The connected calendars could not be checked. Please try again shortly."
                  : "This link is disabled or no longer exists."}
              </p>
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
                {formatWhen(confirmation.starts_at, confirmation.ends_at, displayTimezone)}
              </p>
              <p className="booking-confirmed-copy">
                {confirmation.calendar_event_created
                  ? confirmation.host_email
                    ? `Calendar invitation from ${confirmation.host_email} sent to ${confirmation.guest_email}.`
                    : `Calendar invitation sent to ${confirmation.guest_email}.`
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

        {!isLoading && !error && data && !confirmation && (
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
                  {displayTimezone}
                </span>
              </div>

              {data.description && (
                <p className="booking-description">{data.description}</p>
              )}

              <div>
                <h2 className="booking-section-title">Time zone</h2>
                <Select
                  value={displayTimezone}
                  onChange={(nextTimezone) => {
                    setViewerTimezone(nextTimezone);
                    setSelectedDate("");
                    setSelectedSlot("");
                    setAvailabilityChanged(false);
                    setVisibleMonth(monthKeyFromInstant(new Date(), nextTimezone));
                    bookMutation.reset();
                  }}
                  options={timezoneOptions}
                  dropdownMinWidth={260}
                  buttonStyle={{ width: "100%", minHeight: 40, fontSize: 12 }}
                  dropdownStyle={{ maxHeight: 280 }}
                />
                <p className="booking-muted-line" style={{ marginTop: 8 }}>
                  Times are shown in your local time zone. You can change it here.
                </p>
              </div>
            </aside>

            <div className="booking-content">
              <div className="booking-time-section">
                <h2 className="booking-content-title">Select a time</h2>
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
                  {activeSlots.length > 0 ? (
                    <div className="booking-slot-grid">
                      {activeSlots.map((slot) => {
                        const active = slot.starts_at === selectedSlot;
                        return (
                          <button
                            key={slot.starts_at}
                            type="button"
                            onClick={() => selectSlot(slot.starts_at)}
                            aria-pressed={active}
                            className={`booking-slot-button${active ? " is-active" : ""}`}
                          >
                            {formatSlotLabel(slot.starts_at, slot.ends_at, displayTimezone)}
                          </button>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="booking-no-slots">
                      No times available in this month. Try another month.
                    </div>
                  )}
                </div>
              </div>

              <div className="booking-form">
                <div className="booking-form-header">
                  <h2 className="booking-content-title">Your details</h2>
                  {chosenSlot && (
                    <div className="booking-selected-time">
                      <span>Selected</span>
                      <strong>{formatWhen(chosenSlot.starts_at, chosenSlot.ends_at, displayTimezone)}</strong>
                    </div>
                  )}
                  {availabilityChanged && (
                    <div className="booking-availability-notice" role="status" aria-live="polite">
                      Your previous time is no longer available. Please choose another slot.
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
                  <div className="booking-error-text" role="alert" aria-live="polite">
                    {bookMutation.error instanceof ApiError && bookMutation.error.status === 409
                      ? "That time is no longer available. Choose another slot."
                      : bookMutation.error instanceof ApiError
                        ? bookMutation.error.message
                        : "Could not book this time"}
                  </div>
                )}
                <button className="btn-manor booking-submit" type="button" disabled={!canSubmit} onClick={() => { void submitBooking(); }}>
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
