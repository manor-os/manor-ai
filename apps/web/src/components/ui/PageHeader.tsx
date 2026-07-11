import type { ButtonHTMLAttributes, ReactNode } from "react";
import { IconPlus } from "../icons";

interface PageHeaderProps {
  title: string;
  subtitle?: ReactNode;
  /** Primary page-level actions. */
  actions?: ReactNode;
  /** Section navigation or in-page segmented controls. */
  tabs?: ReactNode;
  /** Search, filters, and contextual tools. */
  toolbar?: ReactNode;
  /** Legacy slot: prefer actions/tabs/toolbar for new code. */
  children?: ReactNode;
  /** Disable for dense toolbars that should wait for wide screens. */
  compactControls?: boolean;
}

interface PageHeaderAddButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  caret?: boolean;
}

export function PageHeaderAddButton({
  label,
  caret = false,
  className = "",
  disabled,
  children,
  ...props
}: PageHeaderAddButtonProps) {
  return (
    <button
      type="button"
      className={`inline-flex h-9 w-[152px] max-w-full shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-[10px] border-0 bg-manor-700 px-3 text-[13px] font-semibold text-white transition-colors hover:bg-manor-800 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-55 ${className}`}
      disabled={disabled}
      {...props}
    >
      {children ?? (
        <>
          <IconPlus size={14} className="shrink-0" />
          <span className="min-w-0 truncate">{label}</span>
        </>
      )}
      {caret && (
        <svg
          width={14}
          height={14}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      )}
    </button>
  );
}

export default function PageHeader({
  title, subtitle, actions, tabs, toolbar, children, compactControls = true,
}: PageHeaderProps) {
  const hasControls = tabs || toolbar || children || actions;
  const frameClass = compactControls
    ? "flex w-full flex-col gap-4 md:flex-row md:items-start md:justify-between md:gap-6"
    : "flex w-full flex-col gap-4 2xl:flex-row 2xl:items-start 2xl:justify-between 2xl:gap-6";
  const titleClass = compactControls
    ? "min-w-0 text-left md:min-w-[160px] md:flex-1 xl:min-w-[220px]"
    : "min-w-0 text-left 2xl:min-w-[220px] 2xl:flex-1";
  const controlsClass = compactControls
    ? "flex w-full min-w-0 flex-row flex-wrap items-center justify-start gap-2.5 md:mt-1 md:flex-1 md:justify-end 2xl:w-auto 2xl:flex-none 2xl:flex-nowrap"
    : "flex w-full min-w-0 flex-row flex-wrap items-center justify-start gap-2.5 2xl:mt-1 2xl:w-auto 2xl:flex-none 2xl:flex-nowrap 2xl:justify-end";
  const groupClass = compactControls
    ? "flex min-w-0 flex-none flex-wrap items-center gap-2.5 md:justify-end"
    : "flex min-w-0 flex-none flex-wrap items-center gap-2.5 2xl:justify-end";
  const actionsClass = compactControls
    ? "flex min-w-0 flex-wrap items-center gap-2.5 md:flex-none md:justify-end"
    : "flex min-w-0 flex-wrap items-center gap-2.5 2xl:flex-none 2xl:justify-end";

  return (
    <div className="shrink-0 px-2 pt-1 mb-4">
      <div className={frameClass}>
        <div className={titleClass}>
          <h1 className="m-0 break-words text-2xl font-bold leading-tight tracking-tight text-stone-800 lg:text-3xl">
            {title}
          </h1>
          {subtitle && (
            <div className="mt-1.5 break-words text-[13px] font-medium leading-5 text-stone-500 lg:max-w-3xl">
              {subtitle}
            </div>
          )}
        </div>

        {/* Unified controls row: tabs -> filters/search -> page actions. */}
        {hasControls && (
          <div className={controlsClass}>
            {tabs && (
              <div className="min-w-0 flex-none overflow-x-auto overflow-y-hidden">
                {tabs}
              </div>
            )}
            {(toolbar || children) && (
              <div className={groupClass}>
                {toolbar}
                {children}
              </div>
            )}
            {actions && (
              <div className={actionsClass}>
                {actions}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
