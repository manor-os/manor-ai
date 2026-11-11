/**
 * The form controls a schema picks beyond text, numbers, lists and pickers:
 * date range, tags, colour, rating, Markdown text, address, signature and
 * line items. WorkflowSchemaFields draws them for every form (run inputs,
 * input waits, chat questions); their value rules live in workflowSchema.ts.
 */
import { lazy, Suspense, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { t } from "../../lib/i18n";
import DateTimePicker from "../ui/DateTimePicker";
import TabSwitcher from "../ui/TabSwitcher";
import { IconClose, IconExternalLink, IconPlus, IconStar, IconTrash } from "../icons";
import { uploadWorkflowFile } from "./WorkflowWaitDocumentField";
import type { WorkflowDocumentRef } from "../../lib/workflowRunInputs";
import {
  addTags,
  isValidHexColor,
  lineItemColumns,
  lineItemTotal,
  mapsSearchUrl,
  normalizeHexColor,
  ratingScale,
  removeTag,
  splitTagText,
  tagList,
} from "./workflowSchema";
import type { WorkflowInputSchema } from "./workflowSchema";
import "./WorkflowFormControlFields.css";

const ChatMarkdown = lazy(() => import("../ChatMarkdown"));

interface ControlProps {
  id: string;
  label: string;
  disabled?: boolean;
  required?: boolean;
  invalid?: boolean;
  describedBy?: string;
}

/** Two dates (or moments) read as one answer, ``{start, end}``. */
export function DateRangeField({ id, label, value, mode, disabled, required, invalid, describedBy, onChange }: ControlProps & {
  value: unknown;
  mode: "date" | "datetime";
  onChange: (value: { start: string; end: string }) => void;
}) {
  const range = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const start = String(range.start ?? "");
  const end = String(range.end ?? "");
  return (
    <div id={id} className="workflow-form-range" role="group" aria-label={label} aria-describedby={describedBy}>
      <label>
        <span>{t("component.workflow_schema.range_start")}</span>
        <DateTimePicker
          id={`${id}-start`}
          mode={mode}
          value={start}
          max={end && mode === "date" ? end : undefined}
          onChange={(next) => onChange({ start: next, end })}
          placeholder={t("component.chat_action_card.select")}
          disabled={disabled}
          ariaLabel={`${label} · ${t("component.workflow_schema.range_start")}`}
          ariaRequired={required}
          ariaInvalid={invalid}
        />
      </label>
      <span aria-hidden="true" className="workflow-form-range-dash">–</span>
      <label>
        <span>{t("component.workflow_schema.range_end")}</span>
        <DateTimePicker
          id={`${id}-end`}
          mode={mode}
          value={end}
          min={start && mode === "date" ? start : undefined}
          onChange={(next) => onChange({ start, end: next })}
          placeholder={t("component.chat_action_card.select")}
          disabled={disabled}
          ariaLabel={`${label} · ${t("component.workflow_schema.range_end")}`}
          ariaRequired={required}
          ariaInvalid={invalid}
        />
      </label>
    </div>
  );
}

/** Words typed into the box become chips at Enter or a comma; the value is their line list. */
export function TagsField({ id, label, value, disabled, invalid, describedBy, placeholder, onChange }: ControlProps & {
  value: unknown;
  placeholder?: string;
  onChange: (draft: string) => void;
}) {
  const [text, setText] = useState("");
  const tags = tagList(value);
  const commit = (raw: string, all: boolean) => {
    const { tags: added, rest } = splitTagText(raw, all);
    if (added.length) onChange(addTags(value, added));
    setText(rest);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      commit(text, true);
    } else if (event.key === "Backspace" && !text && tags.length) {
      onChange(removeTag(value, tags[tags.length - 1]));
    }
  };
  return (
    <div className="workflow-form-tags" data-disabled={disabled || undefined}>
      {tags.map((tag) => (
        <span key={tag} className="workflow-form-tag">
          {tag}
          <button type="button" disabled={disabled} aria-label={t("component.workflow_schema.tag_remove", { tag })}
            onClick={() => onChange(removeTag(value, tag))}>
            <IconClose size={11} />
          </button>
        </span>
      ))}
      <input
        id={id}
        type="text"
        value={text}
        disabled={disabled}
        placeholder={tags.length ? "" : placeholder || t("component.workflow_schema.tag_placeholder")}
        aria-label={label}
        aria-invalid={invalid}
        aria-describedby={describedBy}
        onChange={(event) => commit(event.target.value, false)}
        onKeyDown={onKeyDown}
        onBlur={() => commit(text, true)}
      />
    </div>
  );
}

/** A colour picked from the system picker or typed as ``#RRGGBB``. */
export function ColorField({ id, label, value, disabled, required, invalid, describedBy, onChange }: ControlProps & {
  value: unknown;
  onChange: (value: string) => void;
}) {
  const text = String(value ?? "");
  const swatch = isValidHexColor(normalizeHexColor(text)) ? normalizeHexColor(text) : "#000000";
  return (
    <span className="workflow-form-color">
      <input
        type="color"
        value={swatch}
        disabled={disabled}
        aria-label={label}
        onChange={(event) => onChange(event.target.value)}
      />
      <input
        id={id}
        type="text"
        className="mono"
        value={text}
        maxLength={7}
        placeholder="#1A2B3C"
        disabled={disabled}
        spellCheck={false}
        aria-label={`${label} · ${t("component.workflow_schema.color_hex")}`}
        aria-required={required}
        aria-invalid={invalid}
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.value)}
        onBlur={() => { if (text) onChange(normalizeHexColor(text)); }}
      />
    </span>
  );
}

/** Stars from the schema's minimum to maximum (1–5 unless it says otherwise). */
export function RatingField({ id, label, schema, value, disabled, required, invalid, describedBy, onChange }: ControlProps & {
  schema: WorkflowInputSchema;
  value: unknown;
  onChange: (value: string) => void;
}) {
  const scale = ratingScale(schema);
  const current = Number(String(value ?? ""));
  const max = scale[scale.length - 1] ?? 5;
  return (
    <span className="workflow-form-rating">
      <span id={id} role="radiogroup" aria-label={label} aria-required={required} aria-invalid={invalid} aria-describedby={describedBy}>
        {scale.map((step) => (
          <button
            key={step}
            type="button"
            role="radio"
            aria-checked={current === step}
            aria-label={t("component.workflow_schema.rating_value", { value: step, max })}
            data-filled={Number.isFinite(current) && step <= current ? "true" : undefined}
            disabled={disabled}
            onClick={() => onChange(String(step))}
          >
            <IconStar size={20} />
          </button>
        ))}
      </span>
      {!required && Number.isFinite(current) && String(value ?? "") !== "" && (
        <button type="button" className="workflow-form-link" disabled={disabled} onClick={() => onChange("")}>
          {t("component.workflow_schema.rating_clear")}
        </button>
      )}
    </span>
  );
}

const RICH_TEXT_MARKS = {
  bold: ["**", "**"],
  italic: ["_", "_"],
  list: ["\n- ", ""],
  link: ["[", "](https://)"],
} as const;

/** Markdown with a few formatting buttons and a preview (no rich editor ships with Manor's web app). */
export function RichTextField({ id, label, value, rows, placeholder, disabled, required, invalid, describedBy, onChange }: ControlProps & {
  value: unknown;
  rows?: number;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  const [view, setView] = useState("write");
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const text = String(value ?? "");
  const mark = (kind: keyof typeof RICH_TEXT_MARKS) => {
    const area = areaRef.current;
    const [before, after] = RICH_TEXT_MARKS[kind];
    const from = area?.selectionStart ?? text.length;
    const to = area?.selectionEnd ?? text.length;
    const next = `${text.slice(0, from)}${before}${text.slice(from, to)}${after}${text.slice(to)}`;
    onChange(next);
    requestAnimationFrame(() => {
      area?.focus();
      area?.setSelectionRange(from + before.length, to + before.length);
    });
  };
  return (
    <div className="workflow-form-rich">
      <div className="workflow-form-rich-bar">
        <TabSwitcher
          tabs={[{ key: "write", label: t("component.workflow_schema.rich_write") }, { key: "preview", label: t("component.workflow_schema.rich_preview") }]}
          value={view}
          onChange={setView}
        />
        {view === "write" && (
          <span className="workflow-form-rich-marks">
            <button type="button" disabled={disabled} aria-label={t("component.workflow_schema.rich_bold")} onClick={() => mark("bold")}><strong>B</strong></button>
            <button type="button" disabled={disabled} aria-label={t("component.workflow_schema.rich_italic")} onClick={() => mark("italic")}><em>I</em></button>
            <button type="button" disabled={disabled} aria-label={t("component.workflow_schema.rich_list")} onClick={() => mark("list")}>•</button>
            <button type="button" disabled={disabled} aria-label={t("component.workflow_schema.rich_link")} onClick={() => mark("link")}>↗</button>
          </span>
        )}
      </div>
      {view === "write" ? (
        <textarea
          ref={areaRef}
          id={id}
          value={text}
          rows={rows || 5}
          placeholder={placeholder}
          disabled={disabled}
          aria-label={label}
          aria-required={required}
          aria-invalid={invalid}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <div className="workflow-form-rich-preview" id={id}>
          {text.trim()
            ? <Suspense fallback={<p>{text}</p>}><ChatMarkdown content={text} enableFileCards={false} /></Suspense>
            : <p className="workflow-form-muted">{t("component.workflow_schema.rich_empty")}</p>}
        </div>
      )}
    </div>
  );
}

/** A street address typed in full, with a link that opens it on a map. */
export function AddressField({ id, label, value, placeholder, disabled, required, invalid, describedBy, onChange }: ControlProps & {
  value: unknown;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  const text = String(value ?? "");
  const link = mapsSearchUrl(text);
  return (
    <span className="workflow-form-address">
      <textarea
        id={id}
        rows={2}
        value={text}
        placeholder={placeholder}
        autoComplete="street-address"
        disabled={disabled}
        aria-label={label}
        aria-required={required}
        aria-invalid={invalid}
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.value)}
      />
      {link && (
        <a className="workflow-form-link" href={link} target="_blank" rel="noopener noreferrer">
          <IconExternalLink size={12} /> {t("component.workflow_schema.open_map")}
        </a>
      )}
    </span>
  );
}

/** A signature drawn on a pad, saved as a PNG Document (the upload shape, one file). */
export function SignatureField({ id, label, value, disabled, workspaceId, workflowRunInput, describedBy, onChange, onBusyChange }: ControlProps & {
  value: unknown;
  workspaceId?: string;
  workflowRunInput?: boolean;
  onChange: (value: WorkflowDocumentRef[]) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const drawing = useRef<number | null>(null);
  const [inked, setInked] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const [preview, setPreview] = useState("");
  const signed = Array.isArray(value) ? value.find((item): item is WorkflowDocumentRef => Boolean(item && typeof item === "object" && "document_id" in item)) : undefined;

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    canvas.width = 720;
    canvas.height = 220;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.lineWidth = 4;
    // Ink follows the theme's text colour; the saved PNG keeps a transparent background.
    context.strokeStyle = getComputedStyle(canvas).color || "#1d1d1f";
  }, [signed?.document_id]);

  const point = (event: PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    return [
      (event.clientX - rect.left) * (event.currentTarget.width / rect.width),
      (event.clientY - rect.top) * (event.currentTarget.height / rect.height),
    ] as const;
  };
  const start = (event: PointerEvent<HTMLCanvasElement>) => {
    if (disabled || saving || drawing.current !== null || !event.isPrimary) return;
    drawing.current = event.pointerId;
    event.currentTarget.setPointerCapture(event.pointerId);
    const context = event.currentTarget.getContext("2d");
    const [x, y] = point(event);
    context?.beginPath();
    context?.moveTo(x, y);
    context?.lineTo(x + 0.1, y + 0.1);
    context?.stroke();
    setInked(true);
  };
  const move = (event: PointerEvent<HTMLCanvasElement>) => {
    if (drawing.current !== event.pointerId) return;
    const context = event.currentTarget.getContext("2d");
    const [x, y] = point(event);
    context?.lineTo(x, y);
    context?.stroke();
  };
  const end = (event: PointerEvent<HTMLCanvasElement>) => {
    if (drawing.current !== event.pointerId) return;
    drawing.current = null;
  };
  const clear = () => {
    const canvas = canvasRef.current;
    canvas?.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
    setInked(false);
    setFailed(false);
  };
  const save = async () => {
    const canvas = canvasRef.current;
    if (!canvas || !inked || saving) return;
    setSaving(true);
    setFailed(false);
    onBusyChange?.(true);
    try {
      const dataUrl = canvas.toDataURL("image/png");
      const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("empty signature");
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      const document = await uploadWorkflowFile(new File([blob], `signature-${stamp}.png`, { type: "image/png" }), { workspaceId, workflowRunInput });
      setPreview(dataUrl);
      onChange([document]);
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
      onBusyChange?.(false);
    }
  };

  if (signed) {
    return (
      <div id={id} className="workflow-form-signature is-signed" aria-describedby={describedBy}>
        {preview ? <img src={preview} alt={label} /> : <span className="workflow-form-muted">{signed.name || signed.document_id}</span>}
        <span className="workflow-form-signature-row">
          <span>{t("component.workflow_schema.signature_signed")}</span>
          <button type="button" className="workflow-form-link" disabled={disabled} onClick={() => { setPreview(""); setInked(false); onChange([]); }}>
            {t("component.workflow_schema.signature_redo")}
          </button>
        </span>
      </div>
    );
  }
  return (
    <div className="workflow-form-signature">
      <canvas
        id={id}
        ref={canvasRef}
        role="img"
        aria-label={`${label} · ${t("component.workflow_schema.signature_pad")}`}
        aria-describedby={describedBy}
        onPointerDown={start}
        onPointerMove={move}
        onPointerUp={end}
        onPointerCancel={end}
      />
      {!inked && <span className="workflow-form-signature-hint" aria-hidden="true">{t("component.workflow_schema.signature_hint")}</span>}
      <span className="workflow-form-signature-row">
        <button type="button" className="workflow-form-link" disabled={disabled || saving || !inked} onClick={clear}>
          {t("component.workflow_schema.signature_clear")}
        </button>
        <button type="button" className="chat-hitl-btn-secondary" disabled={disabled || saving || !inked} onClick={() => void save()}>
          {saving ? t("component.workflow_schema.signature_saving") : t("component.workflow_schema.signature_use")}
        </button>
      </span>
      {failed && <small role="alert">{t("component.workflow_schema.signature_failed")}</small>}
    </div>
  );
}

/** Rows of an order or quote: one input per column, rows added and removed, number columns totalled. */
export function LineItemsField({ id, label, schema, value, disabled, invalid, describedBy, errors, errorPath, onChange }: ControlProps & {
  schema: WorkflowInputSchema;
  value: unknown;
  /** The form's errors; a cell's sits at ``<errorPath>.<row>.<column>``. */
  errors: Record<string, string>;
  errorPath: string;
  onChange: (rows: Record<string, unknown>[]) => void;
}) {
  const columns = lineItemColumns(schema.items);
  const rows = Array.isArray(value) ? value.map((row) => (row && typeof row === "object" ? row as Record<string, unknown> : {})) : [];
  const update = (index: number, key: string, cell: string) => onChange(rows.map((row, at) => (at === index ? { ...row, [key]: cell } : row)));
  const add = () => onChange([...rows, Object.fromEntries(columns.map((column) => [column.key, ""]))]);
  const maxRows = typeof schema.maxItems === "number" ? schema.maxItems : 200;
  return (
    <div className="workflow-form-lines" id={id} role="group" aria-label={label} aria-describedby={describedBy} aria-invalid={invalid}>
      {rows.length === 0 ? <p className="workflow-form-muted">{t("component.workflow_schema.line_empty")}</p> : (
        <div className="workflow-form-lines-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((column) => <th key={column.key} scope="col" data-numeric={column.kind !== "text" && column.kind !== "select" || undefined}>{column.label}</th>)}
                <th aria-hidden="true" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  {columns.map((column) => {
                    const cellError = errors[`${errorPath}.${index}.${column.key}`];
                    const cellLabel = `${column.label} · ${index + 1}`;
                    return (
                      <td key={column.key}>
                        {column.kind === "select" ? (
                          <select value={String(row[column.key] ?? "")} disabled={disabled} aria-label={cellLabel} aria-invalid={Boolean(cellError)}
                            onChange={(event) => update(index, column.key, event.target.value)}>
                            <option value="">{t("component.chat_action_card.select")}</option>
                            {(column.options || []).map((option) => <option key={option} value={option}>{option}</option>)}
                          </select>
                        ) : (
                          <input
                            type={column.kind === "text" ? "text" : "number"}
                            inputMode={column.kind === "integer" ? "numeric" : column.kind === "number" ? "decimal" : undefined}
                            step={column.kind === "integer" ? 1 : column.kind === "number" ? "any" : undefined}
                            className={column.kind === "text" ? undefined : "mono"}
                            value={String(row[column.key] ?? "")}
                            disabled={disabled}
                            aria-label={cellLabel}
                            aria-invalid={Boolean(cellError)}
                            title={cellError || undefined}
                            onChange={(event) => update(index, column.key, event.target.value)}
                          />
                        )}
                      </td>
                    );
                  })}
                  <td>
                    <button type="button" className="workflow-form-icon-button" disabled={disabled}
                      aria-label={t("component.workflow_schema.line_remove", { index: index + 1 })}
                      onClick={() => onChange(rows.filter((_, at) => at !== index))}>
                      <IconTrash size={14} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
            {columns.some((column) => column.kind === "number" || column.kind === "integer") && (
              <tfoot>
                <tr>
                  {columns.map((column, index) => {
                    const total = column.kind === "number" || column.kind === "integer" ? lineItemTotal(rows, column.key) : null;
                    return <td key={column.key} className={total === null ? undefined : "mono"} data-numeric={total !== null || undefined}>
                      {total !== null ? total : index === 0 ? t("component.workflow_schema.line_total") : ""}
                    </td>;
                  })}
                  <td />
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}
      {Object.entries(errors).some(([path, message]) => message && path.startsWith(`${errorPath}.`)) && (
        <small role="alert">{Object.entries(errors).find(([path, message]) => message && path.startsWith(`${errorPath}.`))?.[1]}</small>
      )}
      <button type="button" className="workflow-form-link" disabled={disabled || rows.length >= maxRows} onClick={add}>
        <IconPlus size={12} /> {t("component.workflow_schema.line_add")}
      </button>
    </div>
  );
}
