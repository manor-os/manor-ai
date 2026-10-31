import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useAuthStore } from '../stores/auth';
import { api } from '../lib/api';
import { getAuthToken, isSameAuthIdentity } from '../lib/authToken';
import { getPreferredTimeZone } from '../lib/format';
import { applyThemePreference, getStoredThemePreference } from '../lib/theme';
import { automaticRecordingMeeting, calendarIdentityMatches, calendarPickerOriginAllowed, parseRecordingCalendarRequest, rankRecordingMeetings, recordingCalendarRange, selectedRecordingMeeting } from '../lib/recordingCalendar';
import type { RecordingCalendarRequest } from '../lib/recordingCalendar';
import type { CalendarConnectionOption, ExternalCalendarEvent } from '../lib/types';
import './TaskImportEmbed.css';
import './CalendarPickerEmbed.css';

export default function CalendarPickerEmbed() {
  const { user, isLoading, checkAuth } = useAuthStore();
  const [request, setRequest] = useState<RecordingCalendarRequest | null>(null);
  const [connections, setConnections] = useState<CalendarConnectionOption[]>([]), [connection, setConnection] = useState('');
  const [events, setEvents] = useState<ExternalCalendarEvent[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [configured, setConfigured] = useState(false), [reload, setReload] = useState(0), [sent, setSent] = useState(false);
  const selecting = useRef(false), generation = useRef(0);
  const channel = useMemo(() => {
    const params = new URLSearchParams(window.location.hash.slice(1)), nonce = params.get('nonce') || '', origin = params.get('origin') || '';
    return window.parent !== window && /^[a-f0-9-]{36}$/.test(nonce) && calendarPickerOriginAllowed(origin, window.location.origin)
      ? { nonce, origin, theme: params.get('theme') } : null;
  }, []);
  const send = useCallback((data: Record<string, unknown>) => { if (channel) window.parent.postMessage({ ...data, nonce: channel.nonce }, channel.origin); }, [channel]);
  const theme = useCallback((value: unknown) => {
    if (!channel || (value !== 'dark' && value !== 'light')) return;
    document.documentElement.dataset.companionTaskTheme = value;
    applyThemePreference(value === 'dark' ? 'dark' : 'white');
  }, [channel]);
  useLayoutEffect(() => { theme(channel?.theme); return () => { delete document.documentElement.dataset.companionTaskTheme; applyThemePreference(getStoredThemePreference()); }; }, [channel, theme]);
  useEffect(() => {
    if (!channel) return;
    let received = false;
    const receive = (event: MessageEvent) => {
      if (event.source !== window.parent || event.origin !== channel.origin || event.data?.nonce !== channel.nonce) return;
      if (event.data?.type === 'manor:calendar-theme:v1') { theme(event.data.theme); return; }
      if (received || event.data?.type !== 'manor:calendar:request') return;
      try { const incoming = parseRecordingCalendarRequest(event.data); received = true; theme(event.data.theme); setRequest(incoming); send({ type: 'manor:calendar:received' }); void checkAuth(); }
      catch { setError('Could not load this calendar request. Close and try again.'); }
    };
    const ready = () => { if (!received) send({ type: 'manor:calendar:ready' }); };
    window.addEventListener('message', receive); ready();
    const interval = window.setInterval(ready, 500);
    const timeout = window.setTimeout(() => { window.clearInterval(interval); if (!received) setError('The connection timed out. Close and try again.'); }, 30000);
    return () => { generation.current++; window.removeEventListener('message', receive); window.clearInterval(interval); window.clearTimeout(timeout); };
  }, [channel, checkAuth, send, theme]);
  useEffect(() => {
    const ended = () => { generation.current++; setEvents([]); setConnections([]); setConfigured(false); useAuthStore.setState({ user: null, token: null, isLoading: false }); };
    window.addEventListener('manor:companion-session-ended', ended);
    return () => window.removeEventListener('manor:companion-session-ended', ended);
  }, []);
  const matches = !!request && calendarIdentityMatches(user, request);
  const automatic = request?.mode === 'automatic';
  useEffect(() => {
    if (!automatic || !request || isLoading) return;
    if (!matches) { send({ type: 'manor:calendar:complete', status: 'unavailable' }); return; }
    let active = true;
    const token = getAuthToken(), id = generation.current;
    const valid = () => active && id === generation.current && isSameAuthIdentity(token, getAuthToken());
    void (async () => {
      try {
        const settings = await api.calendarSettings.get();
        if (!valid()) return;
        const range = recordingCalendarRange(request.at);
        // Check all connected accounts before deciding a match is unique. A
        // partially failed fetch must never silently pick the wrong calendar.
        const results = await Promise.all(settings.connections.map(connection =>
          api.calendarSettings.events(range.start, range.end, { provider: connection.provider, connectionId: connection.id })));
        if (!valid()) return;
        const current = await api.auth.me();
        if (!valid() || !calendarIdentityMatches(current, request)) return;
        const events = results.flatMap(result => result.events);
        const event = automaticRecordingMeeting(events, request.at);
        if (event) send({ type: 'manor:calendar:selected', event: selectedRecordingMeeting(event) });
        else send({ type: 'manor:calendar:complete', status: rankRecordingMeetings(events, request.at).length ? 'ambiguous' : 'empty' });
      } catch { if (valid()) send({ type: 'manor:calendar:complete', status: 'unavailable' }); }
    })();
    return () => { active = false; };
  }, [automatic, request, matches, isLoading, user?.id, user?.entity_id, send]);
  useEffect(() => {
    if (automatic) return;
    const id = ++generation.current;
    setEvents([]); setConnections([]); setConnection(''); setConfigured(false);
    if (!matches || isLoading) return;
    let active = true; setBusy(true); setError('');
    api.calendarSettings.get().then(data => {
      if (!active || id !== generation.current) return;
      setConnections(data.connections);
      const selected = data.connections.find(c => c.id === data.settings.connection_id && c.provider === data.settings.provider)
        || data.connections.find(c => c.is_default) || data.connections[0];
      setConnection(selected?.id || ''); setConfigured(true);
    }).catch(() => { if (active) setError('Could not load your calendars. Try again.'); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [automatic, matches, isLoading, user?.id, user?.entity_id, reload]);
  useEffect(() => {
    setEvents([]);
    if (!matches || !request || !configured || !connection) return;
    const selected = connections.find(c => c.id === connection);
    if (!selected) return;
    let active = true; setBusy(true); setError('');
    const range = recordingCalendarRange(request.at);
    api.calendarSettings.events(range.start, range.end, { provider: selected.provider, connectionId: selected.id })
      .then(data => { if (active) setEvents(data.events); })
      .catch(() => { if (active) setError('Calendar could not load. Reconnect it in Manor or try again.'); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [matches, request, configured, connection, connections]);
  const choose = async (event: ExternalCalendarEvent) => {
    if (!request || !matches || sent || selecting.current) return;
    selecting.current = true; setBusy(true); setError('');
    const token = getAuthToken(), id = generation.current;
    try {
      // Account may have changed in another Manor tab while this picker was open.
      const current = await api.auth.me();
      if (id !== generation.current) return;
      if (!isSameAuthIdentity(token, getAuthToken()) || !calendarIdentityMatches(current, request)) {
        setEvents([]); setError('Your Manor account changed. Close this picker and open it again.'); return;
      }
      setSent(true); send({ type: 'manor:calendar:selected', event: selectedRecordingMeeting(event) });
    } catch { setError('Could not confirm your Manor account. Please sign in again.'); }
    finally { selecting.current = false; setBusy(false); }
  };
  if (automatic) return <main aria-label="Calendar matching" />;
  if (!channel || !request || isLoading || !user || !matches || sent) return <main className="companion-task-message">
    <p role={error || (user && !matches) ? 'alert' : 'status'}>{!channel ? 'Open this picker from Minutes.' : error || (!request || isLoading ? 'Loading Manor…' : sent ? 'Meeting selected. You can close this window.' : !user ? 'Sign in to the same Manor account you use in Minutes.' : 'This Manor account differs from the account signed in to Minutes.')}</p>
    {request && !isLoading && (!user || !matches) && <><a href="/account" target="_blank" rel="noopener noreferrer">Open Manor account</a><button onClick={() => void checkAuth()}>I’ve signed in — continue</button></>}
  </main>;
  const candidates = rankRecordingMeetings(events, request.at);
  return <main className="calendar-picker">
    <div className="calendar-picker-account"><span>{user.email}</span><button onClick={() => { void checkAuth(); setReload(v => v + 1); }} disabled={busy}>Refresh</button></div>
    <p className="calendar-picker-note">Choose a possible match for this recording. Only the meeting you select is added to Minutes.</p>
    {connections.length > 1 && <label>Calendar account<select aria-label="Calendar account" disabled={busy} value={connection} onChange={e => setConnection(e.target.value)}>{connections.map(c => <option key={c.id} value={c.id}>{c.display_name || c.provider_user_id || c.provider}</option>)}</select></label>}
    {error && <p role="alert">{error}</p>}
    {busy && <p role="status">Loading…</p>}
    {!busy && configured && !connections.length && <p>No calendar connected. <a href="/integrations" target="_blank" rel="noopener noreferrer">Connect a calendar in Manor</a>.</p>}
    {!busy && configured && !!connection && !error && !candidates.length && <p>No meetings near this time. Close this picker to enter a title and location manually.</p>}
    {!error && candidates.map(({ event, ongoing }) => <article key={event.id}>
      <div><strong>{event.title}</strong><p>{ongoing ? 'In progress · ' : ''}{new Date(event.starts_at).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: getPreferredTimeZone() })}{event.ends_at ? ` – ${new Date(event.ends_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZone: getPreferredTimeZone() })}` : ''}</p>{event.location && <p>{event.location}</p>}</div>
      <button onClick={() => void choose(event)} disabled={busy}>Use meeting</button>
    </article>)}
  </main>;
}
