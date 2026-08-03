import {
  createContext,
  useContext,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { IconPlus } from "../icons";

const PAGE_HEADER_TITLE_CLASS =
  "page-header-title m-0 flex h-10 min-w-0 items-center overflow-hidden text-ellipsis whitespace-nowrap text-2xl font-bold leading-[1.2] tracking-[-0.014em] text-[color:var(--text-strong)] md:text-[28px]";
const PAGE_HEADER_SUBTITLE_CLASS =
  "page-header-subtitle mt-1 h-5 max-w-3xl overflow-hidden text-ellipsis whitespace-nowrap text-[13px] font-normal leading-5 text-[color:var(--text-muted)]";
const PAGE_HEADER_META_CLASS =
  "page-header-meta mt-1 flex h-6 max-w-3xl items-center overflow-x-auto overflow-y-hidden whitespace-nowrap text-[13px] font-normal leading-5 text-[color:var(--text-muted)]";

interface PageHeaderPortalContextValue {
  target: HTMLDivElement | null;
}

const PageHeaderPortalContext = createContext<PageHeaderPortalContextValue | null>(null);

export interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  /** Counts, scope, or other compact context shown in the dedicated metadata row. */
  meta?: ReactNode;
  /** Primary page-level actions. */
  actions?: ReactNode;
  /** Section navigation or in-page segmented controls. */
  tabs?: ReactNode;
  /** Search, filters, and contextual tools. */
  toolbar?: ReactNode;
  /** Legacy slot: prefer actions/tabs/toolbar for new code. */
  children?: ReactNode;
  /** Keep a section-level header in place instead of using the app-level header slot. */
  inline?: boolean;
}

interface PageHeaderAddButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  caret?: boolean;
}

export function PageHeaderTitle({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <h1 className={`${PAGE_HEADER_TITLE_CLASS} ${className}`}>{children}</h1>;
}

export function PageHeaderSubtitle({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`${PAGE_HEADER_SUBTITLE_CLASS} ${className}`}>{children}</div>;
}

export function PageHeaderBoundary({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<HTMLDivElement | null>(null);

  return (
    <PageHeaderPortalContext.Provider value={{ target }}>
      <div ref={setTarget} className="page-header-host shrink-0" data-page-header-host />
      {children}
    </PageHeaderPortalContext.Provider>
  );
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
      className={`inline-flex h-9 min-w-[152px] max-w-full shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-[10px] border-0 bg-manor-700 px-3 text-[13px] font-semibold text-white transition-colors hover:bg-manor-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-manor-700/25 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-55 ${className}`}
      disabled={disabled}
      {...props}
    >
      {children ?? (
        <>
          <IconPlus size={14} className="shrink-0" />
          <span>{label}</span>
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
  title,
  subtitle,
  meta,
  actions,
  tabs,
  toolbar,
  children,
  inline = false,
}: PageHeaderProps) {
  const portalContext = useContext(PageHeaderPortalContext);
  const hasControls = tabs || toolbar || children || actions;
  const frameClass =
    "flex w-full flex-col gap-3 2xl:flex-row 2xl:items-start 2xl:justify-between 2xl:gap-6";
  const titleClass = "min-w-0 text-left 2xl:w-[420px] 2xl:flex-none";
  const controlsClass =
    "flex w-full min-w-0 flex-row flex-wrap items-center justify-start gap-2.5 2xl:mt-1 2xl:flex-1 2xl:flex-nowrap 2xl:justify-end";
  const groupClass =
    "flex min-w-0 flex-none flex-wrap items-center gap-2.5 md:flex-nowrap md:justify-end";
  const actionsClass =
    "flex min-w-0 flex-wrap items-center gap-2.5 md:flex-none md:justify-end";

  const header = (
    <header
      className={portalContext && !inline
        ? "page-header shrink-0 px-3 pb-0 pt-3 sm:px-4 sm:pt-4 md:px-6 md:pt-6"
        : "page-header mb-4 shrink-0 px-2 pt-1"}
      data-page-header-layout={portalContext && !inline ? "app" : "inline"}
    >
      <div className={frameClass}>
        <div className={titleClass}>
          <PageHeaderTitle>{title}</PageHeaderTitle>
          <PageHeaderSubtitle className={!subtitle ? "invisible" : ""}>
            {subtitle || <span aria-hidden="true">&nbsp;</span>}
          </PageHeaderSubtitle>
          {meta && (
            <div className={PAGE_HEADER_META_CLASS}>
              <div className="flex h-full min-w-0 flex-nowrap items-center gap-2">{meta}</div>
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
    </header>
  );

  if (portalContext && !inline) {
    return portalContext.target ? createPortal(header, portalContext.target) : null;
  }

  return header;
}
