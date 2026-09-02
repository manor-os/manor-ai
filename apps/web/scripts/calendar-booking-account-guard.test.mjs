import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const accountSource = readFileSync(
  new URL("../src/pages/Account.tsx", import.meta.url),
  "utf8",
);
const bookingLinkSource = readFileSync(
  new URL("../src/pages/BookingLink.tsx", import.meta.url),
  "utf8",
);
const tasksSource = readFileSync(
  new URL("../src/pages/Tasks.tsx", import.meta.url),
  "utf8",
);
const workingHoursSource = readFileSync(
  new URL("../src/components/ui/WorkingHoursEditor.tsx", import.meta.url),
  "utf8",
);
const stylesSource = readFileSync(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const routerSource = readFileSync(
  new URL("../src/router.tsx", import.meta.url),
  "utf8",
);
const calendarSource = readFileSync(
  new URL("../src/components/ui/Calendar.tsx", import.meta.url),
  "utf8",
);

test("saved calendar connections are matched within their provider", () => {
  assert.match(
    accountSource,
    /const savedProvider = settings\?\.provider \|\| "";[\s\S]*?connections\.find\(\(connection\) =>[\s\S]*?connection\.provider === savedProvider[\s\S]*?connection\.provider_user_id === savedConnectionId/,
  );
});

test("booking links require a saved and connected calendar account", () => {
  assert.match(
    accountSource,
    /const calendarAccountReady = calendarAccountSelectionSaved[\s\S]*?providerConnections\.some\(\(connection\) => connection\.id === connectionId\)/,
  );
  assert.match(
    accountSource,
    /const createLink = async \(\) => \{[\s\S]*?if \(!linkName\.trim\(\) \|\| !linkDurationValid \|\| !calendarAccountReady\) return;/,
  );
  assert.match(
    accountSource,
    /disabled=\{!linkName\.trim\(\) \|\| !linkDurationValid \|\| linkBusy \|\| !calendarAccountReady\}/,
  );
  assert.match(
    accountSource,
    /Save calendar settings before creating a booking link\./,
  );
  assert.match(
    accountSource,
    /Connect a calendar account before creating a booking link\./,
  );
});

test("connected calendar providers keep an add-account entry point", () => {
  assert.match(
    accountSource,
    /providerConnections\.length > 0[\s\S]*?Connected account[\s\S]*?calendarConnectButton\("Add account", "sm"\)/,
  );
  assert.match(
    accountSource,
    /provider === "google_calendar"[\s\S]*?onClick=\{connectCalendarProvider\}[\s\S]*?<NangoConnectButton/,
  );
});

test("new booking links can be cancelled and reset", () => {
  assert.match(
    accountSource,
    /const resetLinkForm = \(\) => \{[\s\S]*?setLinkName\(""\)[\s\S]*?setLinkDuration\(String\(durationMinutes \|\| 30\)\)[\s\S]*?setLinkLocationType\("video"\)[\s\S]*?setShowLinkForm\(false\)/,
  );
  assert.match(
    accountSource,
    /onClick=\{resetLinkForm\}[\s\S]*?>[\s\S]*?Cancel[\s\S]*?<\/Button>/,
  );
});

test("booking-link working hours center Friday by weekday semantics", () => {
  assert.match(workingHoursSource, /data-day-of-week=\{row\.day_of_week\}/);
  assert.match(
    stylesSource,
    /\.working-hours-grid > \.working-hours-row\[data-day-of-week="4"\]/,
  );
  assert.match(
    stylesSource,
    /\.booking-link-editor-hours \.working-hours-row\[data-day-of-week="4"\]/,
  );
  assert.doesNotMatch(
    stylesSource,
    /\.booking-link-editor-hours \.working-hours-row:last-child:nth-child\(odd\)/,
  );
});

test("conflict availability combines calendars from every connected account", () => {
  assert.match(
    accountSource,
    /useQueries\(\{[\s\S]*?api\.calendarSettings\.calendars\(account\.provider, account\.connectionId\)/,
  );
  assert.match(accountSource, /conflict_sources: conflictSources/);
  assert.match(accountSource, /Check conflicts across accounts/);
  assert.match(
    accountSource,
    /Selected busy times are combined before booking slots are shown\./,
  );
});

test("calendar settings cannot save unresolved conflict accounts", () => {
  assert.match(
    accountSource,
    /const conflictCalendarsReady = conflictCalendarQueries\.every\(\(query\) =>[\s\S]*?query\.isSuccess[\s\S]*?query\.data\?\.calendars\.length/,
  );
  assert.match(
    accountSource,
    /const saveSettings = async \(\) => \{[\s\S]*?if \(!conflictCalendarsReady\)[\s\S]*?Calendar settings cannot be saved/,
  );
  assert.match(
    accountSource,
    /disabled=\{savingSettings \|\| isLoading \|\| !conflictCalendarsReady\}/,
  );
});

test("external calendar events refresh and failed syncs preserve visible state", () => {
  assert.match(
    accountSource,
    /invalidateQueries\(\{ queryKey: \["calendar-settings-events"\] \}\)/,
  );
  assert.match(
    tasksSource,
    /externalCalendarSettingsKey[\s\S]*?settings\.provider[\s\S]*?settings\.connection_id[\s\S]*?settings\.visible_calendar_ids/,
  );
  assert.match(
    tasksSource,
    /queryKey:\s*\[\s*"calendar-settings-events"[\s\S]*?externalCalendarSettingsKey[\s\S]*?refetchInterval: EXTERNAL_CALENDAR_REFRESH_INTERVAL_MS[\s\S]*?refetchIntervalInBackground: false/,
  );
  assert.match(
    tasksSource,
    /externalCalendarSyncFailed && \([\s\S]*?role="alert"[\s\S]*?Showing the last successfully synced external events\.[\s\S]*?refetchExternalCalendar/,
  );
});

test("external calendar sync uses the configured timezone and an exclusive range end", () => {
  assert.match(
    tasksSource,
    /const endExclusive = new Date\(year, mo \+ 1, 7 - endDow\);[\s\S]*?end: localDateKey\(endExclusive\)/,
  );
  assert.match(
    tasksSource,
    /timezone: externalCalendarData\?\.timezone \|\| event\.timezone \|\| undefined/,
  );
});

test("public booking availability is month-scoped and preserves invalidated choices", () => {
  assert.match(
    bookingLinkSource,
    /refetchInterval: confirmation \? false : AVAILABILITY_REFRESH_INTERVAL_MS/,
  );
  assert.match(bookingLinkSource, /refetchIntervalInBackground: false/);
  assert.match(bookingLinkSource, /refetchOnWindowFocus: !confirmation/);
  assert.match(
    bookingLinkSource,
    /publicBookingLink\([\s\S]*?visibleMonth\.slice\(0, 7\)[\s\S]*?viewerTimezone \|\| undefined/,
  );
  assert.match(
    bookingLinkSource,
    /monthKeysInRange\([\s\S]*?data\.availability_range_start[\s\S]*?data\.availability_range_end/,
  );
  assert.match(
    bookingLinkSource,
    /if \(selectedSlot && !slotExists\) \{[\s\S]*?setSelectedSlot\(""\)[\s\S]*?setAvailabilityChanged\(true\)/,
  );
  assert.match(
    bookingLinkSource,
    /availabilityChanged && \([\s\S]*?booking-availability-notice[\s\S]*?previous time is no longer available/,
  );
  assert.match(
    bookingLinkSource,
    /mutationError\.status !== 409[\s\S]*?setSelectedSlot\(""\)[\s\S]*?setAvailabilityChanged\(true\)[\s\S]*?await refetch\(\)/,
  );
  assert.match(bookingLinkSource, /role="alert" aria-live="polite"/);
  assert.match(bookingLinkSource, /role="status" aria-live="polite"/);
});

test("public booking routes and requests stay anonymous", () => {
  assert.match(routerSource, /path: "\/book\/u\/:ownerId\/:slug"/);
  assert.match(routerSource, /path: "\/book\/:slug"/);
  assert.match(
    apiSource,
    /publicBookingLink:[\s\S]*?return request<PublicBookingLink>\([\s\S]*?\{\},\s*null,?\s*\);/,
  );
  assert.match(
    apiSource,
    /bookPublicBookingLink:[\s\S]*?request<BookingConfirmation>\([\s\S]*?\{ method: "POST", body: JSON\.stringify\(data\) \},\s*null,?\s*\)/,
  );
  assert.match(
    apiSource,
    /res\.status === 401 &&\s*authTokenOverride === undefined &&\s*Boolean\(token\) &&\s*tokenIsCurrent\(\)/,
  );
});

test("all-day external events preserve their calendar date", () => {
  assert.match(
    calendarSource,
    /if \(ev\.all_day\) return value\.slice\(0, 10\);/,
  );
  assert.match(
    calendarSource,
    /if \(ev\.all_day\) return `\$\{formatCivilDate\(eventStartValue\(ev\)\)\}, All day`;/,
  );
  assert.match(calendarSource, /timeZone: "UTC"/);
});
