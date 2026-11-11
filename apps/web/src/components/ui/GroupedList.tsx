/**
 * The one settings layout: a titled group of rows on a quiet fill.
 *
 * Usage:
 *   <ConfigPage title="General">
 *     <ConfigGroup title="Basics">
 *       <ConfigRow label="Name" control={<input className="config-input" />} />
 *       <ConfigRow label="Usage" note="Today, this week" onClick={open} chevron />
 *     </ConfigGroup>
 *   </ConfigPage>
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { IconChevronRight } from "../icons";

export function ConfigPage({
  title,
  action,
  children,
  wide = false,
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={`config-page${wide ? " config-page--wide" : ""}`}>
      {(title || action) && (
        <div className="config-page-head">
          <h2 className="config-page-title">{title}</h2>
          {action}
        </div>
      )}
      {children}
    </div>
  );
}

export function ConfigGroup({
  title,
  action,
  note,
  children,
  id,
}: {
  title?: ReactNode;
  action?: ReactNode;
  note?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section className="config-group" id={id}>
      {(title || action) && (
        <div className="config-group-head">
          <h3 className="config-group-title">{title}</h3>
          {action}
        </div>
      )}
      <div className="config-group-body">{children}</div>
      {note && <p className="config-group-note">{note}</p>}
    </section>
  );
}

export function ConfigRow({
  label,
  note,
  leading,
  control,
  meta,
  chevron = false,
  danger = false,
  disabled = false,
  expanded,
  onClick,
  to,
  htmlFor,
}: {
  label: ReactNode;
  note?: ReactNode;
  leading?: ReactNode;
  control?: ReactNode;
  meta?: ReactNode;
  chevron?: boolean;
  danger?: boolean;
  disabled?: boolean;
  expanded?: boolean;
  onClick?: () => void;
  to?: string;
  htmlFor?: string;
}) {
  const showChevron = chevron || expanded !== undefined;
  const body = (
    <>
      {leading && <span className="config-row-leading">{leading}</span>}
      <span className="config-row-copy">
        {htmlFor
          ? <label className="config-row-label" htmlFor={htmlFor}>{label}</label>
          : <span className="config-row-label">{label}</span>}
        {note && <span className="config-row-note">{note}</span>}
      </span>
      {meta && <span className="config-row-meta">{meta}</span>}
      {control && <span className="config-row-control">{control}</span>}
      {showChevron && (
        <IconChevronRight
          size={14}
          className={`config-row-chevron${expanded ? " is-open" : ""}`}
        />
      )}
    </>
  );
  const className = `config-row${danger ? " config-row--danger" : ""}`;
  if (to) return <Link className={`${className} config-row--action`} to={to}>{body}</Link>;
  if (onClick) {
    return (
      <button
        type="button"
        className={`${className} config-row--action`}
        onClick={onClick}
        disabled={disabled}
        aria-expanded={expanded}
      >
        {body}
      </button>
    );
  }
  return <div className={className}>{body}</div>;
}

/** Stacks a full-width editor (a textarea, a list) inside a group. */
export function ConfigBlock({ children }: { children: ReactNode }) {
  return <div className="config-block">{children}</div>;
}

export function ConfigAddRow({
  label,
  onClick,
  disabled,
}: {
  label: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button type="button" className="config-row config-row--action config-row--add" onClick={onClick} disabled={disabled}>
      <span className="config-row-add-icon" aria-hidden="true">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M8 3.5v9M3.5 8h9" /></svg>
      </span>
      <span className="config-row-label">{label}</span>
    </button>
  );
}
