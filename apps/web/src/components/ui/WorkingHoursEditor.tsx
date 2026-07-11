import { IconClock } from "../icons";
import Checkbox from "./Checkbox";
import Select from "./Select";

export interface WorkingHoursWindow {
  day_of_week: number;
  enabled: boolean;
  start: string;
  end: string;
}

interface WorkingHoursEditorProps {
  value: WorkingHoursWindow[];
  onChange: (value: WorkingHoursWindow[]) => void;
  days?: string[];
  title?: string;
}

const DEFAULT_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function minutesFromTime(value: string): number {
  const [hour, minute] = value.split(":").map((part) => Number(part));
  if (!Number.isFinite(hour) || !Number.isFinite(minute)) return 0;
  return hour * 60 + minute;
}

function formatTimeLabel(value: string): string {
  const minutes = minutesFromTime(value);
  const hour24 = Math.floor(minutes / 60);
  const minute = minutes % 60;
  const suffix = hour24 >= 12 ? "PM" : "AM";
  const hour12 = hour24 === 0 ? 12 : hour24 > 12 ? hour24 - 12 : hour24;
  return `${pad(hour12)}:${pad(minute)} ${suffix}`;
}

const TIME_OPTIONS = Array.from({ length: 24 * 4 }, (_, index) => {
  const total = index * 15;
  const value = `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
  return { value, label: formatTimeLabel(value) };
});

function timeOptionsWithValue(value: string) {
  if (!value || TIME_OPTIONS.some((option) => option.value === value)) return TIME_OPTIONS;
  return [...TIME_OPTIONS, { value, label: formatTimeLabel(value) }]
    .sort((a, b) => minutesFromTime(a.value) - minutesFromTime(b.value));
}

export default function WorkingHoursEditor({
  value,
  onChange,
  days = DEFAULT_DAYS,
  title = "Working hours",
}: WorkingHoursEditorProps) {
  const rows = value.length ? value : [];

  const updateRow = (day: number, patch: Partial<WorkingHoursWindow>) => {
    onChange(rows.map((row) => (row.day_of_week === day ? { ...row, ...patch } : row)));
  };

  return (
    <div className="working-hours-editor">
      <div className="working-hours-editor-heading" style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <IconClock size={14} style={{ color: "#78716c" }} />
        <span style={{ fontSize: 13, fontWeight: 800, color: "#292524" }}>{title}</span>
      </div>
      <div
        className="working-hours-grid"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 360px), 1fr))",
          gap: 8,
          maxWidth: 980,
        }}
      >
        {rows.map((row) => (
          <div
            key={row.day_of_week}
            className={`working-hours-row${row.enabled ? "" : " is-disabled"}`}
            style={{
              display: "grid",
              gridTemplateColumns: "64px minmax(120px, 1fr) minmax(120px, 1fr)",
              alignItems: "center",
              gap: 8,
              padding: "10px 11px",
              borderRadius: 12,
              border: "1px solid rgba(231,229,228,0.82)",
              background: row.enabled ? "rgba(255,255,255,0.76)" : "rgba(245,245,244,0.62)",
            }}
          >
            <Checkbox
              checked={row.enabled}
              onChange={(checked) => updateRow(row.day_of_week, { enabled: checked })}
              label={days[row.day_of_week]}
              size="sm"
              style={{ fontSize: 12, fontWeight: 800 }}
            />
            <Select
              value={row.start}
              disabled={!row.enabled}
              onChange={(start) => updateRow(row.day_of_week, { start })}
              options={timeOptionsWithValue(row.start)}
              dropdownMinWidth={150}
              buttonStyle={{ height: 34, fontSize: 12, padding: "0 10px" }}
              dropdownStyle={{ maxHeight: 220, borderRadius: 12 }}
              optionStyle={{ height: 32, fontSize: 12, padding: "0 10px" }}
            />
            <Select
              value={row.end}
              disabled={!row.enabled}
              onChange={(end) => updateRow(row.day_of_week, { end })}
              options={timeOptionsWithValue(row.end)}
              dropdownMinWidth={150}
              buttonStyle={{ height: 34, fontSize: 12, padding: "0 10px" }}
              dropdownStyle={{ maxHeight: 220, borderRadius: 12 }}
              optionStyle={{ height: 32, fontSize: 12, padding: "0 10px" }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
