import type { ExternalCalendarEvent } from './types';

export type RecordingCalendarRequest = { identity: { user_id: string; entity_id: string }; at: string; mode?: 'automatic' };
export function calendarPickerOriginAllowed(origin: string, currentOrigin: string) {
  if (origin === 'https://minutes.manorai.xyz') return true;
  const local = (value: string) => { try { const u = new URL(value); return u.origin === value && u.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(u.hostname); } catch { return false; } };
  return local(origin) && local(currentOrigin);
}
export function parseRecordingCalendarRequest(value: unknown): RecordingCalendarRequest {
  const v = value as RecordingCalendarRequest;
  if (!v || typeof v.at !== 'string' || v.at.length > 80 || !Number.isFinite(Date.parse(v.at))
    || !v.identity || [v.identity.user_id, v.identity.entity_id].some(id => typeof id !== 'string' || !id || id.length > 128)) throw new Error('Invalid calendar request.');
  return { identity: { user_id: v.identity.user_id, entity_id: v.identity.entity_id }, at: v.at,
    ...(v.mode === 'automatic' ? { mode: 'automatic' as const } : {}) };
}
export function calendarIdentityMatches(user: { id: string; entity_id: string } | null, request: RecordingCalendarRequest) {
  return !!user && user.id === request.identity.user_id && user.entity_id === request.identity.entity_id;
}
export function recordingCalendarRange(at: string) {
  const time = Date.parse(at), day = 86400000;
  // Covers the recording's day across calendar/device timezone boundaries.
  return { start: new Date(time - day).toISOString().slice(0, 10), end: new Date(time + 2 * day).toISOString().slice(0, 10) };
}
export function rankRecordingMeetings(events: ExternalCalendarEvent[], at: string) {
  const now = Date.parse(at), nearby = 90 * 60000;
  if (!Number.isFinite(now)) return [];
  const seen = new Set<string>();
  return events.flatMap(event => {
    const start = Date.parse(event.starts_at), end = event.ends_at ? Date.parse(event.ends_at) : start + 30 * 60000;
    if (event.all_day || ['cancelled', 'canceled', 'declined'].includes(event.status || '')
      || !event.title.trim() || !Number.isFinite(start) || !Number.isFinite(end) || end <= start || seen.has(event.id)) return [];
    seen.add(event.id);
    const ongoing = start <= now && end > now;
    if (!ongoing && Math.abs(start - now) > nearby) return [];
    return [{ event, ongoing, distance: Math.abs(start - now) }];
  }).sort((a, b) => Number(b.ongoing) - Number(a.ongoing) || a.distance - b.distance || a.event.id.localeCompare(b.event.id)).slice(0, 8);
}
export function selectedRecordingMeeting(event: ExternalCalendarEvent) {
  // Only one selected or unambiguously matched meeting crosses into Minutes. No description,
  // attendees, account tokens, join links or other calendar entries are sent.
  return { provider: event.provider, event_id: event.external_event_id, calendar_id: event.calendar_id,
    title: event.title.slice(0, 255), starts_at: event.starts_at, ends_at: event.ends_at || null, location: (event.location || '').slice(0, 500) };
}

export function automaticRecordingMeeting(events: ExternalCalendarEvent[], at: string) {
  const candidates = rankRecordingMeetings(events, at);
  const ongoing = candidates.filter(candidate => candidate.ongoing);
  // A unique ongoing meeting is stronger evidence than a nearby start time.
  // Otherwise only auto-fill a unique meeting starting in the next 15 minutes.
  const likely = ongoing.length ? ongoing : candidates.filter(candidate =>
    Date.parse(candidate.event.starts_at) >= Date.parse(at) && candidate.distance <= 15 * 60000);
  return likely.length === 1 ? likely[0].event : null;
}
