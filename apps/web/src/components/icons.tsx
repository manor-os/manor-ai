/**
 * Centralized icon system for Manor OS.
 *
 * Every icon uses Heroicons 24-outline conventions:
 *   viewBox="0 0 24 24", fill="none", stroke="currentColor", strokeWidth={1.5}
 *
 * Usage:
 *   import { IconPlus, IconTrash } from "../components/icons";
 *   <IconPlus />
 *   <IconPlus size={16} className="text-red-500" />
 */

import type { MouseEvent } from "react";

/* ── Shared prop interface ────────────────────────────────────── */

export interface IconProps {
  size?: number;
  className?: string;
  style?: React.CSSProperties;
  onClick?: (e: MouseEvent) => void;
}

/* ── Helper: wraps every icon in a consistent <svg> shell ───── */

function Svg({
  size = 20,
  className,
  style,
  onClick,
  children,
}: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      onClick={onClick}
      style={{ ...(onClick ? { cursor: "pointer" } : undefined), ...style }}
    >
      {children}
    </svg>
  );
}

/* ================================================================
   GENERAL / ACTIONS
   ================================================================ */

/** Plus sign — add / create */
export function IconPlus(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 4.5v15m7.5-7.5h-15" />
    </Svg>
  );
}

/** Trash can — delete */
export function IconTrash(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
    </Svg>
  );
}

/** Pencil square — edit */
export function IconEdit(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L6.832 19.82a4.5 4.5 0 0 1-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 0 1 1.13-1.897L16.863 4.487Zm0 0L19.5 7.125" />
    </Svg>
  );
}

/** Text cursor / add text */
export function IconText(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4.5 6.75V5.25h15v1.5M12 5.25v13.5m-3.75 0h7.5" />
    </Svg>
  );
}

/** Signature squiggle */
export function IconSignature(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.75 18.75h16.5" />
      <path d="M4.5 15.5c2.25-6.5 4.5-8.5 6-7.25 1.25 1.04-.25 4.5-1.75 6.75 2-1.75 4.25-3 5.5-2 1 .8.3 2.35 1.6 2.35.98 0 1.9-.8 3.65-2.1" />
    </Svg>
  );
}

/** Curved arrow left — undo */
export function IconUndo(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9 7.5 4.5 12 9 16.5" />
      <path d="M5.25 12H15a4.5 4.5 0 0 1 0 9h-1.5" />
    </Svg>
  );
}

/** Curved arrow right — redo */
export function IconRedo(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15 7.5 19.5 12 15 16.5" />
      <path d="M18.75 12H9a4.5 4.5 0 0 0 0 9h1.5" />
    </Svg>
  );
}

/** Highlighter marker */
export function IconHighlighter(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m14.25 4.5 5.25 5.25-8.75 8.75H5.5v-5.25L14.25 4.5Z" />
      <path d="m12.75 6 5.25 5.25" />
      <path d="M4.5 20.25h15" />
    </Svg>
  );
}

/** Freehand pen line */
export function IconPenLine(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m16.5 4.5 3 3L8.25 18.75l-4.5 1.5 1.5-4.5L16.5 4.5Z" />
      <path d="m14.75 6.25 3 3" />
      <path d="M12.5 20.25h7" />
    </Svg>
  );
}

/** Eraser / whiteout */
export function IconEraser(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m7.5 20.25-4.25-4.25a2.12 2.12 0 0 1 0-3L12 4.25a2.12 2.12 0 0 1 3 0L20.75 10a2.12 2.12 0 0 1 0 3l-7.25 7.25h-6Z" />
      <path d="m9 7.25 7.75 7.75" />
      <path d="M14 20.25h6.25" />
    </Svg>
  );
}

/** Magnifying glass — search */
export function IconSearch(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
    </Svg>
  );
}

/** X mark — close / dismiss */
export function IconClose(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6 18L18 6M6 6l12 12" />
    </Svg>
  );
}

/** Clipboard / copy */
export function IconCopy(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9.75a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184" />
    </Svg>
  );
}

/** Refresh / arrows-path */
export function IconRefresh(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.992 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" />
    </Svg>
  );
}

/** Link */
export function IconLink(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m9.86-2.54a4.5 4.5 0 00-1.242-7.244l-4.5-4.5a4.5 4.5 0 00-6.364 6.364l1.757 1.757" />
    </Svg>
  );
}

/** External link / arrow-top-right-on-square */
export function IconExternalLink(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
    </Svg>
  );
}

/** Send / paper-airplane */
export function IconSend(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
    </Svg>
  );
}

/** Envelope — email */
export function IconMail(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
    </Svg>
  );
}

/** Phone handset */
export function IconPhone(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2.25 6.75c0 8.284 6.716 15 15 15h2.25a2.25 2.25 0 002.25-2.25v-1.372c0-.516-.351-.966-.852-1.091l-4.423-1.106a1.125 1.125 0 00-1.173.417l-.97.97a1.125 1.125 0 01-1.21.244 12.035 12.035 0 01-6.31-6.31 1.125 1.125 0 01.244-1.21l.97-.97c.362-.362.527-.884.417-1.386L7.337 3.102A1.125 1.125 0 006.245 2.25H4.5A2.25 2.25 0 002.25 4.5v2.25z" />
    </Svg>
  );
}

/* ================================================================
   DOCUMENTS & FILES
   ================================================================ */

/** Document / page */
export function IconDocument(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
    </Svg>
  );
}

/** Folder */
export function IconFolder(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z" />
    </Svg>
  );
}

/** Paperclip — standard "attach a file" icon used by chat + task composers. */
export function IconPaperclip(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
    </Svg>
  );
}

/** Image / photo */
export function IconImage(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4.5 5.25h15A2.25 2.25 0 0121.75 7.5v9A2.25 2.25 0 0119.5 18.75h-15A2.25 2.25 0 012.25 16.5v-9A2.25 2.25 0 014.5 5.25z" />
      <path d="m3.75 16.25 4.1-4.1a1.5 1.5 0 012.12 0l2.03 2.03 1.53-1.53a1.5 1.5 0 012.12 0l4.6 4.6" />
      <path d="M16.5 9.25h.01" />
    </Svg>
  );
}

/** Aspect ratio / crop proportions */
export function IconAspectRatio(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4.5 7.5V6a1.5 1.5 0 011.5-1.5h1.5" />
      <path d="M16.5 4.5H18A1.5 1.5 0 0119.5 6v1.5" />
      <path d="M19.5 16.5V18a1.5 1.5 0 01-1.5 1.5h-1.5" />
      <path d="M7.5 19.5H6A1.5 1.5 0 014.5 18v-1.5" />
      <path d="M8.25 9h7.5v6h-7.5z" />
    </Svg>
  );
}

/** Resolution / output quality */
export function IconResolution(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4.5 5.25h15A2.25 2.25 0 0121.75 7.5v9a2.25 2.25 0 01-2.25 2.25h-15A2.25 2.25 0 012.25 16.5v-9A2.25 2.25 0 014.5 5.25z" />
      <path d="M7.5 9.75h.01M10.5 9.75h.01M13.5 9.75h.01M16.5 9.75h.01" />
      <path d="M7.5 12.75h.01M10.5 12.75h.01M13.5 12.75h.01M16.5 12.75h.01" />
      <path d="M7.5 15.75h.01M10.5 15.75h.01M13.5 15.75h.01M16.5 15.75h.01" />
    </Svg>
  );
}

/** Upload / arrow-up-tray */
export function IconUpload(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
    </Svg>
  );
}

/** Download / arrow-down-tray */
export function IconDownload(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
    </Svg>
  );
}

/* ================================================================
   STATUS / FEEDBACK
   ================================================================ */

/** Check mark */
export function IconCheck(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4.5 12.75l6 6 9-13.5" />
    </Svg>
  );
}

/** Check inside circle */
export function IconCheckCircle(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </Svg>
  );
}

/** Clock */
export function IconClock(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
    </Svg>
  );
}

/** Star (outline) */
export function IconStar(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" />
    </Svg>
  );
}

/** Warning triangle / exclamation-triangle */
export function IconWarning(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
    </Svg>
  );
}

/** Info circle / information-circle */
export function IconInfo(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
    </Svg>
  );
}

/** Error / x-circle */
export function IconError(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9.75 9.75l4.5 4.5m0-4.5l-4.5 4.5M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </Svg>
  );
}

/* ================================================================
   CHEVRONS
   ================================================================ */

export function IconChevronDown(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
    </Svg>
  );
}

export function IconChevronRight(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M8.25 4.5l7.5 7.5-7.5 7.5" />
    </Svg>
  );
}

export function IconChevronLeft(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15.75 19.5L8.25 12l7.5-7.5" />
    </Svg>
  );
}

/* ================================================================
   ARROWS
   ================================================================ */

export function IconArrowUp(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4.5 10.5L12 3m0 0l7.5 7.5M12 3v18" />
    </Svg>
  );
}

export function IconArrowDown(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M19.5 13.5L12 21m0 0l-7.5-7.5M12 21V3" />
    </Svg>
  );
}

export function IconArrowLeft(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" />
    </Svg>
  );
}

export function IconArrowRight(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
    </Svg>
  );
}

/* ================================================================
   NAVIGATION / CHROME
   ================================================================ */

/** Gear / cog — settings */
export function IconSettings(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z" />
      <path d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </Svg>
  );
}

/** Bell — notifications */
export function IconBell(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
    </Svg>
  );
}

/** Single user */
export function IconUser(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" />
    </Svg>
  );
}

/** Multiple users / user-group */
export function IconUsers(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0112.75 0v.109zm0-9.879a3 3 0 11-6 0 3 3 0 016 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" />
    </Svg>
  );
}

/** Home */
export function IconHome(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2.25 12l8.954-8.955c.44-.439 1.152-.439 1.591 0L21.75 12M4.5 9.75v10.125c0 .621.504 1.125 1.125 1.125H9.75v-4.875c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21h4.125c.621 0 1.125-.504 1.125-1.125V9.75M8.25 21h8.25" />
    </Svg>
  );
}

/* ================================================================
   VISIBILITY
   ================================================================ */

/** Eye — visible */
export function IconEye(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z" />
      <path d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </Svg>
  );
}

/** Eye with slash — hidden */
export function IconEyeOff(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88" />
    </Svg>
  );
}

/* ================================================================
   COMMUNICATION
   ================================================================ */

/** Chat bubble */
export function IconChat(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z" />
    </Svg>
  );
}

/* ================================================================
   LAYOUT / VIEWS
   ================================================================ */

/** Dashboard — squares-2x2 */
export function IconDashboard(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
    </Svg>
  );
}

/** Grid — view-columns (3 columns) */
export function IconGrid(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9 4.5v15m6-15v15m-10.875 0h15.75c.621 0 1.125-.504 1.125-1.125V5.625c0-.621-.504-1.125-1.125-1.125H4.125C3.504 4.5 3 5.004 3 5.625v12.75c0 .621.504 1.125 1.125 1.125z" />
    </Svg>
  );
}

/** List — bars-3 */
export function IconList(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
    </Svg>
  );
}

/* ================================================================
   DOMAIN-SPECIFIC (Manor OS)
   ================================================================ */

/** Agent — robot / CPU chip */
export function IconAgent(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M8.25 3v1.5M4.5 8.25H3m18 0h-1.5M4.5 12H3m18 0h-1.5m-13.5 3.75H3m18 0h-1.5M8.25 19.5V21M12 3v1.5m0 15V21m3.75-18v1.5m0 15V21m-9-1.5h10.5a2.25 2.25 0 002.25-2.25V6.75a2.25 2.25 0 00-2.25-2.25H6.75A2.25 2.25 0 004.5 6.75v10.5a2.25 2.25 0 002.25 2.25zm.75-12h9v9h-9v-9z" />
    </Svg>
  );
}

/** Flow — arrow-path / route */
export function IconFlow(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M7.5 21L3 16.5m0 0L7.5 12M3 16.5h13.5m0-13.5L21 7.5m0 0L16.5 12M21 7.5H7.5" />
    </Svg>
  );
}

/** Skill — bolt / lightning */
export function IconSkill(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
    </Svg>
  );
}

/** App — squares / puzzle piece */
export function IconApp(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M14.25 6.087c0-.355.186-.676.401-.959.221-.29.349-.634.349-1.003 0-1.036-1.007-1.875-2.25-1.875s-2.25.84-2.25 1.875c0 .369.128.713.349 1.003.215.283.401.604.401.959v0a.64.64 0 01-.657.643 48.491 48.491 0 01-4.163-.3c-1.1-.128-1.907-1.077-1.907-2.185V2.95A48.078 48.078 0 0112 2.25c2.291 0 4.545.16 6.75.463" />
      <path d="M12 2.25c2.291 0 4.545.16 6.75.463v1.282c0 1.108-.806 2.057-1.907 2.185a48.507 48.507 0 01-4.163.3v0a.64.64 0 01-.657-.643v0c0-.355.186-.676.401-.959.221-.29.349-.634.349-1.003 0-1.036-1.007-1.875-2.25-1.875s-2.25.84-2.25 1.875c0 .369.128.713.349 1.003.215.283.401.604.401.959v0a.64.64 0 01-.657.643 48.491 48.491 0 01-4.163-.3c-1.1-.128-1.907-1.077-1.907-2.185V2.95A48.078 48.078 0 0112 2.25z" />
    </Svg>
  );
}

/** Workspace — building-office */
export function IconWorkspace(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.75 21h16.5M4.5 3h15M5.25 3v18m13.5-18v18M9 6.75h1.5m-1.5 3h1.5m-1.5 3h1.5m3-6H15m-1.5 3H15m-1.5 3H15M9 21v-3.375c0-.621.504-1.125 1.125-1.125h3.75c.621 0 1.125.504 1.125 1.125V21" />
    </Svg>
  );
}

/** Goal — target / crosshairs */
export function IconGoal(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="5" />
      <circle cx="12" cy="12" r="1" />
    </Svg>
  );
}

/** Key */
export function IconKey(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1121.75 8.25z" />
    </Svg>
  );
}

/** Webhook — signal / rss */
export function IconWebhook(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9.348 14.651a3.75 3.75 0 010-5.303m5.304 0a3.75 3.75 0 010 5.303m-7.425 2.122a6.75 6.75 0 010-9.546m9.546 0a6.75 6.75 0 010 9.546M5.106 18.894c-3.808-3.808-3.808-9.98 0-13.789m13.788 0c3.808 3.808 3.808 9.981 0 13.79M12 12h.008v.007H12V12zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z" />
    </Svg>
  );
}

/* ================================================================
   METADATA / MISC
   ================================================================ */

/** Calendar */
export function IconCalendar(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5" />
    </Svg>
  );
}

/** Tag */
export function IconTag(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9.568 3H5.25A2.25 2.25 0 003 5.25v4.318c0 .597.237 1.17.659 1.591l9.581 9.581c.699.699 1.78.872 2.607.33a18.095 18.095 0 005.223-5.223c.542-.827.369-1.908-.33-2.607L11.16 3.66A2.25 2.25 0 009.568 3z" />
      <path d="M6 6h.008v.008H6V6z" />
    </Svg>
  );
}

/** Briefcase — title / role metadata */
export function IconBriefcase(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M20.25 14.15v4.1A2.25 2.25 0 0118 20.5H6a2.25 2.25 0 01-2.25-2.25v-4.1m16.5 0a2.25 2.25 0 00.75-1.681V8.75A2.25 2.25 0 0018.75 6.5H5.25A2.25 2.25 0 003 8.75v3.719c0 .65.276 1.237.75 1.681m16.5 0a13.45 13.45 0 01-6.75 1.831m-9.75-1.831A13.45 13.45 0 0010.5 15.981m3 0v-1.106A1.125 1.125 0 0012.375 13.75h-.75a1.125 1.125 0 00-1.125 1.125v1.106m3 0a13.696 13.696 0 01-3 0M9 6.5V5.75A2.25 2.25 0 0111.25 3.5h1.5A2.25 2.25 0 0115 5.75v.75" />
    </Svg>
  );
}

/** Filter / funnel */
export function IconFilter(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 01-.659 1.591l-5.432 5.432a2.25 2.25 0 00-.659 1.591v2.927a2.25 2.25 0 01-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 00-.659-1.591L3.659 7.409A2.25 2.25 0 013 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0112 3z" />
    </Svg>
  );
}

/** Sort — bars-arrow-down */
export function IconSort(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 4.5h14.25M3 9h9.75M3 13.5h5.25m5.25-.75L17.25 9m0 0L21 12.75M17.25 9v12" />
    </Svg>
  );
}

/* ================================================================
   PLAYBACK / MEDIA
   ================================================================ */

/** Play */
export function IconPlay(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z" />
    </Svg>
  );
}

/** Pause */
export function IconPause(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15.75 5.25v13.5m-7.5-13.5v13.5" />
    </Svg>
  );
}

/** Stop */
export function IconStop(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M5.25 7.5A2.25 2.25 0 017.5 5.25h9a2.25 2.25 0 012.25 2.25v9a2.25 2.25 0 01-2.25 2.25h-9a2.25 2.25 0 01-2.25-2.25v-9z" />
    </Svg>
  );
}

/** Heart */
export function IconHeart(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" />
    </Svg>
  );
}

/** Thumb up */
export function IconThumbUp(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M6.633 10.5c.806 0 1.533-.446 2.031-1.08a9.041 9.041 0 0 1 2.861-2.4c.723-.384 1.35-.956 1.653-1.715.213-.533.322-1.104.322-1.672V3a.75.75 0 0 1 .75-.75A2.25 2.25 0 0 1 16.5 4.5c0 1.152-.26 2.243-.723 3.218-.266.558.107 1.282.725 1.282h3.126c1.026 0 1.945.694 2.054 1.715.045.422.068.85.068 1.285 0 2.836-.981 5.445-2.649 7.521-.388.482-.987.729-1.605.729H13.48c-.483 0-.963-.077-1.423-.23l-3.114-1.04a4.501 4.501 0 0 0-1.423-.23H5.904" />
      <path d="m6.633 10.5.884 8.25m-.884-8.25H4.875c-.621 0-1.125.504-1.125 1.125v6c0 .621.504 1.125 1.125 1.125h2.642" />
    </Svg>
  );
}

/** Thumb down */
export function IconThumbDown(props: IconProps) {
  return (
    <Svg {...props}>
      <g transform="rotate(180 12 12)">
        <path d="M6.633 10.5c.806 0 1.533-.446 2.031-1.08a9.041 9.041 0 0 1 2.861-2.4c.723-.384 1.35-.956 1.653-1.715.213-.533.322-1.104.322-1.672V3a.75.75 0 0 1 .75-.75A2.25 2.25 0 0 1 16.5 4.5c0 1.152-.26 2.243-.723 3.218-.266.558.107 1.282.725 1.282h3.126c1.026 0 1.945.694 2.054 1.715.045.422.068.85.068 1.285 0 2.836-.981 5.445-2.649 7.521-.388.482-.987.729-1.605.729H13.48c-.483 0-.963-.077-1.423-.23l-3.114-1.04a4.501 4.501 0 0 0-1.423-.23H5.904" />
        <path d="m6.633 10.5.884 8.25m-.884-8.25H4.875c-.621 0-1.125.504-1.125 1.125v6c0 .621.504 1.125 1.125 1.125h2.642" />
      </g>
    </Svg>
  );
}


/* ═══════════════════════════════════════════════════════════════
   Navigation & Layout icons
   ═══════════════════════════════════════════════════════════════ */

export function IconGrid4(props: IconProps) {
  return (<Svg {...props}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></Svg>);
}
export function IconChecklist(props: IconProps) {
  return (<Svg {...props}><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" /></Svg>);
}
export function IconChatBubble(props: IconProps) {
  return (<Svg {...props}><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></Svg>);
}
export function IconBuilding(props: IconProps) {
  return (<Svg {...props}><path d="M3 21h18M5 21V7l8-4v18M19 21V11l-6-4" /><path d="M9 9v.01M9 12v.01M9 15v.01M9 18v.01" /></Svg>);
}
export function IconBrain(props: IconProps) {
  return (<Svg {...props}><path d="M12 2a7 7 0 017 7c0 2.38-1.19 4.47-3 5.74V17a2 2 0 01-2 2h-4a2 2 0 01-2-2v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 017-7z" /><path d="M9 21h6M10 17v4M14 17v4" /></Svg>);
}
export function IconConnection(props: IconProps) {
  return (<Svg {...props}><path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71" /><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71" /></Svg>);
}
export function IconPeople(props: IconProps) {
  return (<Svg {...props}><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" /></Svg>);
}
export function IconStore(props: IconProps) {
  return (<Svg {...props}><path d="M3 9l1-4h16l1 4" /><path d="M3 9v10a2 2 0 002 2h14a2 2 0 002-2V9" /><path d="M9 21V9M3 9c0 1.66 1.34 3 3 3s3-1.34 3-3M9 9c0 1.66 1.34 3 3 3s3-1.34 3-3M15 9c0 1.66 1.34 3 3 3s3-1.34 3-3" /></Svg>);
}
export function IconLayers(props: IconProps) {
  return (<Svg {...props}><polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></Svg>);
}
export function IconGear(props: IconProps) {
  return (<Svg {...props}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" /></Svg>);
}
export function IconFields(props: IconProps) {
  return (<Svg {...props}><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M9 21V9" /></Svg>);
}
export function IconMemoryChip(props: IconProps) {
  return (<Svg {...props}><path d="M4 4h16v16H4z" /><path d="M9 9h6v6H9z" /><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" /></Svg>);
}
export function IconReport(props: IconProps) {
  return (<Svg {...props}><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /><polyline points="10 9 9 9 8 9" /></Svg>);
}
export function IconHelp(props: IconProps) {
  return (<Svg {...props}><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></Svg>);
}


/* ═══════════════════════════════════════════════════════════════
   Business & category icons (used in tasks, integrations, etc.)
   ═══════════════════════════════════════════════════════════════ */

export function IconWrench(props: IconProps) {
  return (<Svg {...props}><path d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085" /></Svg>);
}
export function IconTool(props: IconProps) {
  return (<Svg {...props}><path d="M21.75 6.75a4.5 4.5 0 01-4.884 4.484c-1.076-.091-2.264.071-2.95.904l-7.152 8.684a2.548 2.548 0 11-3.586-3.586l8.684-7.152c.833-.686.995-1.874.904-2.95a4.5 4.5 0 014.484-4.884l-3.276 3.276a3 3 0 004.243 4.243l3.276-3.276" /></Svg>);
}
export function IconSparkles(props: IconProps) {
  return (<Svg {...props}><path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" /></Svg>);
}
export function IconClipboard(props: IconProps) {
  return (<Svg {...props}><path d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25z" /></Svg>);
}
export function IconLock(props: IconProps) {
  return (<Svg {...props}><path d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" /></Svg>);
}
export function IconHeadphones(props: IconProps) {
  return (<Svg {...props}><path d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9 5.25h.008v.008H12v-.008z" /></Svg>);
}
export function IconAudioWave(props: IconProps) {
  return (<Svg {...props}><path d="M4.5 10.5v3" /><path d="M8.25 7.5v9" /><path d="M12 4.5v15" /><path d="M15.75 7.5v9" /><path d="M19.5 10.5v3" /></Svg>);
}
export function IconMicrophone(props: IconProps) {
  return (<Svg {...props}><path d="M12 3.75a3 3 0 00-3 3v5.25a3 3 0 006 0V6.75a3 3 0 00-3-3z" /><path d="M5.25 10.5v1.125a6.75 6.75 0 0013.5 0V10.5" /><path d="M12 18.375v2.875" /><path d="M8.25 21.25h7.5" /></Svg>);
}
export function IconMusicNote(props: IconProps) {
  return (<Svg {...props}><path d="M9 18.25a2.5 2.5 0 11-2.5-2.5A2.5 2.5 0 019 18.25z" /><path d="M19.5 15.25a2.5 2.5 0 11-2.5-2.5 2.5 2.5 0 012.5 2.5z" /><path d="M9 18.25V6.5l10.5-2v10.75" /><path d="M9 9.25l10.5-2" /></Svg>);
}
export function IconTrendingUp(props: IconProps) {
  return (<Svg {...props}><path d="M2.25 18L9 11.25l4.306 4.307a11.95 11.95 0 015.814-5.519l2.74-1.22m0 0l-5.94-2.28m5.94 2.28l-2.28 5.941" /></Svg>);
}
export function IconDollar(props: IconProps) {
  return (<Svg {...props}><path d="M12 6v12m-3-2.818l.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></Svg>);
}
export function IconShoppingCart(props: IconProps) {
  return (<Svg {...props}><path d="M2.25 3h1.386c.51 0 .955.343 1.087.835l.383 1.437M7.5 14.25a3 3 0 00-3 3h15.75m-12.75-3h11.218c1.121-2.3 2.1-4.684 2.924-7.138a60.114 60.114 0 00-16.536-1.84M7.5 14.25L5.106 5.272M6 20.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm12.75 0a.75.75 0 11-1.5 0 .75.75 0 011.5 0z" /></Svg>);
}
export function IconCreditCard(props: IconProps) {
  return (<Svg {...props}><path d="M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 002.25-2.25V6.75A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25v10.5A2.25 2.25 0 004.5 19.5z" /></Svg>);
}
export function IconUserPlus(props: IconProps) {
  return (<Svg {...props}><path d="M19 7.5v3m0 0v3m0-3h3m-3 0h-3m-2.25-4.125a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zM4 19.235v-.11a6.375 6.375 0 0112.75 0v.109A12.318 12.318 0 0110.374 21c-2.331 0-4.512-.645-6.374-1.766z" /></Svg>);
}
export function IconAcademicCap(props: IconProps) {
  return (<Svg {...props}><path d="M4.26 10.147a60.436 60.436 0 00-.491 6.347A48.627 48.627 0 0112 20.904a48.627 48.627 0 018.232-4.41 60.46 60.46 0 00-.491-6.347m-15.482 0a50.57 50.57 0 00-2.658-.813A59.905 59.905 0 0112 3.493a59.902 59.902 0 0110.399 5.84c-.896.248-1.783.52-2.658.814m-15.482 0A50.697 50.697 0 0112 13.489a50.702 50.702 0 017.74-3.342M6.75 15a.75.75 0 100-1.5.75.75 0 000 1.5zm0 0v-3.675A55.378 55.378 0 0112 8.443m-7.007 11.55A5.981 5.981 0 006.75 15.75v-1.5" /></Svg>);
}
export function IconCode(props: IconProps) {
  return (<Svg {...props}><path d="M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" /></Svg>);
}
export function IconServer(props: IconProps) {
  return (<Svg {...props}><path d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z" /></Svg>);
}
export function IconTerminal(props: IconProps) {
  return (<Svg {...props}><path d="M6.75 7.5l3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0021 18V6a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 6v12a2.25 2.25 0 002.25 2.25z" /></Svg>);
}
export function IconBug(props: IconProps) {
  return (<Svg {...props}><path d="M12 12.75c1.148 0 2.278.08 3.383.237 1.037.146 1.866.966 1.866 2.013 0 3.728-2.35 6.75-5.25 6.75S6.75 18.728 6.75 15c0-1.046.83-1.867 1.866-2.013A24.204 24.204 0 0112 12.75zm0 0c2.883 0 5.647.508 8.207 1.44a23.91 23.91 0 01-1.152 6.06M12 12.75c-2.883 0-5.647.508-8.208 1.44.125 2.104.52 4.136 1.153 6.06M12 12.75a2.25 2.25 0 002.248-2.354M12 12.75a2.25 2.25 0 01-2.248-2.354M12 8.25c.995 0 1.971-.08 2.922-.236.403-.066.74-.358.795-.762a3.778 3.778 0 00-.399-2.25M12 8.25c-.995 0-1.97-.08-2.922-.236-.402-.066-.74-.358-.795-.762a3.734 3.734 0 01.4-2.253M12 8.25a2.25 2.25 0 00-2.248 2.146M12 8.25a2.25 2.25 0 012.248 2.146M8.683 5a6.032 6.032 0 01-1.155-1.002c.07-.63.27-1.222.574-1.747m.581 2.749A3.75 3.75 0 0115.318 5m0 0c.427-.283.815-.62 1.155-.999a4.471 4.471 0 00-.575-1.752M4.921 6a24.048 24.048 0 00-.392 3.314c1.668.546 3.416.914 5.223 1.082M19.08 6c.205 1.08.337 2.187.392 3.314a23.882 23.882 0 01-5.223 1.082" /></Svg>);
}
export function IconPalette(props: IconProps) {
  return (<Svg {...props}><path d="M9.53 16.122a3 3 0 00-5.78 1.128 2.25 2.25 0 01-2.4 2.245 4.5 4.5 0 008.4-2.245c0-.399-.078-.78-.22-1.128zm0 0a15.998 15.998 0 003.388-1.62m-5.043-.025a15.994 15.994 0 011.622-3.395m3.42 3.42a15.995 15.995 0 004.764-4.648l3.876-5.814a1.151 1.151 0 00-1.597-1.597L14.146 6.32a15.996 15.996 0 00-4.649 4.763m3.42 3.42a6.776 6.776 0 00-3.42-3.42" /></Svg>);
}
export function IconGlobe(props: IconProps) {
  return (<Svg {...props}><path d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418" /></Svg>);
}
export function IconTruck(props: IconProps) {
  return (<Svg {...props}><path d="M8.25 18.75a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m3 0h6m-9 0H3.375a1.125 1.125 0 01-1.125-1.125V14.25m17.25 4.5a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m3 0h1.125c.621 0 1.129-.504 1.09-1.124a17.902 17.902 0 00-3.213-9.193 2.056 2.056 0 00-1.58-.86H14.25M16.5 18.75h-2.25m0-11.177v-.958c0-.568-.422-1.048-.987-1.106a48.554 48.554 0 00-10.026 0 1.106 1.106 0 00-.987 1.106v7.635m12-6.677v6.677m0 4.5v-4.5m0 0h-12" /></Svg>);
}
export function IconBox(props: IconProps) {
  return (<Svg {...props}><path d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" /></Svg>);
}
export function IconShield(props: IconProps) {
  return (<Svg {...props}><path d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" /></Svg>);
}
export function IconScale(props: IconProps) {
  return (<Svg {...props}><path d="M12 3v17.25m0 0c-1.472 0-2.882.265-4.185.75M12 20.25c1.472 0 2.882.265 4.185.75M18.75 4.97A48.416 48.416 0 0012 4.5c-2.291 0-4.545.16-6.75.47m13.5 0c1.01.143 2.01.317 3 .52m-3-.52l2.62 10.726c.122.499-.106 1.028-.589 1.202a5.988 5.988 0 01-2.031.352 5.988 5.988 0 01-2.031-.352c-.483-.174-.711-.703-.59-1.202L18.75 4.971zm-16.5.52c.99-.203 1.99-.377 3-.52m0 0l2.62 10.726c.122.499-.106 1.028-.589 1.202a5.989 5.989 0 01-2.031.352 5.989 5.989 0 01-2.031-.352c-.483-.174-.711-.703-.59-1.202L5.25 4.971z" /></Svg>);
}
export function IconRocket(props: IconProps) {
  return (<Svg {...props}><path d="M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.631 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.84m2.699 2.7c-.103.021-.207.041-.311.06a15.09 15.09 0 01-2.448-2.448 14.9 14.9 0 01.06-.312m-2.24 2.39a4.493 4.493 0 00-1.757 4.306 4.493 4.493 0 004.306-1.758M16.5 9a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0z" /></Svg>);
}
export function IconBeaker(props: IconProps) {
  return (<Svg {...props}><path d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23-.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" /></Svg>);
}
export function IconBolt(props: IconProps) {
  return (<Svg {...props}><path d="M13 10V3L4 14h7v7l9-11h-7z" /></Svg>);
}
export function IconMegaphone(props: IconProps) {
  return (<Svg {...props}><path d="M10.34 15.84c-.688-.06-1.386-.09-2.09-.09H7.5a4.5 4.5 0 110-9h.75c.704 0 1.402-.03 2.09-.09m0 9.18c.253.962.584 1.892.985 2.783.247.55.06 1.21-.463 1.511l-.657.38c-.551.318-1.26.117-1.527-.461a20.845 20.845 0 01-1.44-4.282m3.102.069a18.03 18.03 0 01-.59-4.59c0-1.586.205-3.124.59-4.59m0 9.18a23.848 23.848 0 018.835 2.535M10.34 6.66a23.847 23.847 0 008.835-2.535m0 0A23.74 23.74 0 0018.795 3m.38 1.125a23.91 23.91 0 011.014 5.395m-1.014 8.855c-.118.38-.245.754-.38 1.125m.38-1.125a23.91 23.91 0 001.014-5.395m0-3.46c.495.413.811 1.035.811 1.73 0 .695-.316 1.317-.811 1.73m0-3.46a24.347 24.347 0 010 3.46" /></Svg>);
}
export function IconFacilities(props: IconProps) {
  return (<Svg {...props}><path d="M2.25 21h19.5m-18-18v18m10.5-18v18m6-13.5V21M6.75 6.75h.75m-.75 3h.75m-.75 3h.75m3-6h.75m-.75 3h.75m-.75 3h.75M6.75 21v-3.375c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125V21M3 3h12m-.75 4.5H21m-3.75 3H21m-3.75 3H21" /></Svg>);
}
export function IconPin(props: IconProps) {
  return (<Svg {...props}><path d="M15 10.5a3 3 0 11-6 0 3 3 0 016 0z" /><path d="M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1115 0z" /></Svg>);
}
export function IconArchive(props: IconProps) {
  return (<Svg {...props}><path d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5m8.25 3v6.75m0 0l-3-3m3 3l3-3M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" /></Svg>);
}
export function IconShare(props: IconProps) {
  return (<Svg {...props}><path d="M7.217 10.907a2.25 2.25 0 100 2.186m0-2.186c.18.324.283.696.283 1.093s-.103.77-.283 1.093m0-2.186l9.566-5.314m-9.566 7.5l9.566 5.314m0 0a2.25 2.25 0 103.935 2.186 2.25 2.25 0 00-3.935-2.186zm0-12.814a2.25 2.25 0 103.933-2.185 2.25 2.25 0 00-3.933 2.185z" /></Svg>);
}
export function IconBookmark(props: IconProps) {
  return (<Svg {...props}><path d="M17.593 3.322c1.1.128 1.907 1.077 1.907 2.185V21L12 17.25 4.5 21V5.507c0-1.108.806-2.057 1.907-2.185a48.507 48.507 0 0111.186 0z" /></Svg>);
}
export function IconDragHandle(props: IconProps) {
  return (<Svg {...props}><path d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" /></Svg>);
}
export function IconManorLogo({ size = 20, className, style }: IconProps) {
  return (
    <svg viewBox="0 0 1024 1024" width={size} height={size} fill="currentColor" className={className} style={style}>
      <path d="M295.152941 0l224.376471 224.376471L743.905882 0H1024v63.247059L519.529412 567.717647 0 49.694118V0h295.152941zM0 256l243.952941 243.952941V1024H0V256z m1024 15.058824v752.941176H780.047059V515.011765L1024 271.058824z" />
    </svg>
  );
}
export function IconFlag(props: IconProps) {
  return (<Svg {...props}><path d="M3 3v1.5M3 21v-6m0 0l2.77-.693a9 9 0 016.208.682l.108.054a9 9 0 006.086.71l3.114-.732a48.524 48.524 0 01-.005-10.499l-3.11.732a9 9 0 01-6.085-.711l-.108-.054a9 9 0 00-6.208-.682L3 4.5M3 15V4.5" /></Svg>);
}
export function IconCircleDot(props: IconProps) {
  return (<Svg {...props}><circle cx="12" cy="12" r="3" /><path d="M12 2a10 10 0 100 20 10 10 0 000-20z" /></Svg>);
}
export function IconReopen(props: IconProps) {
  return (<Svg {...props}><path d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" /></Svg>);
}
export function IconCancel(props: IconProps) {
  return (<Svg {...props}><path d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" /></Svg>);
}
export function IconCategory(props: IconProps) {
  return (<Svg {...props}><path d="M9.568 3H5.25A2.25 2.25 0 003 5.25v4.318c0 .597.237 1.17.659 1.591l9.581 9.581c.699.699 1.78.872 2.607.33a18.095 18.095 0 005.223-5.223c.542-.827.369-1.908-.33-2.607L11.16 3.66A2.25 2.25 0 009.568 3z" /><path d="M6 6h.008v.008H6V6z" /></Svg>);
}
export function IconTimeline(props: IconProps) {
  return (<Svg {...props}><path d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></Svg>);
}
export function IconComment(props: IconProps) {
  return (<Svg {...props}><path d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" /></Svg>);
}


/* ═══════════════════════════════════════════════════════════════
   Integration / brand icons (filled, not stroke-based)
   Uses fill="currentColor" — apply color via className or parent color
   ═══════════════════════════════════════════════════════════════ */

function BrandSvg({ size = 20, className, onClick, style, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24"
      fill="currentColor" className={className} onClick={onClick}
      style={onClick ? { ...style, cursor: "pointer" } : style}
    >{children}</svg>
  );
}

export function IconTelegram(props: IconProps) {
  return (<BrandSvg {...props}><path d="M11.944 0A12 12 0 000 12a12 12 0 0012 12 12 12 0 0012-12A12 12 0 0012 0a12 12 0 00-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 01.171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z" /></BrandSvg>);
}
export function IconWhatsApp(props: IconProps) {
  return (<BrandSvg {...props}><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z" /></BrandSvg>);
}
export function IconWeChat(props: IconProps) {
  return (<BrandSvg {...props}><path d="M8.691 2.188C3.891 2.188 0 5.476 0 9.53c0 2.212 1.17 4.203 3.002 5.55a.59.59 0 01.213.665l-.39 1.48c-.019.07-.048.141-.048.213 0 .163.13.295.29.295a.326.326 0 00.167-.054l1.903-1.114a.864.864 0 01.717-.098 10.16 10.16 0 002.837.403c.276 0 .543-.027.811-.05-.857-2.578.157-4.972 1.932-6.446 1.703-1.415 3.882-1.98 5.853-1.838-.576-3.583-4.196-6.348-8.596-6.348z" /></BrandSvg>);
}
export function IconSlack(props: IconProps) {
  return (<BrandSvg {...props}><path d="M5.042 15.165a2.528 2.528 0 01-2.52 2.523A2.528 2.528 0 010 15.165a2.527 2.527 0 012.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 012.521-2.52 2.527 2.527 0 012.521 2.52v6.313A2.528 2.528 0 018.834 24a2.528 2.528 0 01-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 01-2.521-2.52A2.528 2.528 0 018.834 0a2.528 2.528 0 012.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 012.521 2.521 2.528 2.528 0 01-2.521 2.521H2.522A2.528 2.528 0 010 8.834a2.528 2.528 0 012.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 012.522-2.521A2.528 2.528 0 0124 8.834a2.528 2.528 0 01-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 01-2.523 2.521 2.527 2.527 0 01-2.52-2.521V2.522A2.527 2.527 0 0115.165 0a2.528 2.528 0 012.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 012.523 2.522A2.528 2.528 0 0115.165 24a2.527 2.527 0 01-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 01-2.52-2.523 2.526 2.526 0 012.52-2.52h6.313A2.527 2.527 0 0124 15.165a2.528 2.528 0 01-2.522 2.523h-6.313z" /></BrandSvg>);
}
export function IconGitHub(props: IconProps) {
  return (<BrandSvg {...props}><path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" /></BrandSvg>);
}
export function IconGoogle(props: IconProps) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width={props.size || 20} height={props.size || 20} viewBox="0 0 24 24" className={props.className}>
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" />
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
      <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
    </svg>
  );
}
export function IconStripe(props: IconProps) {
  return (<BrandSvg {...props}><path d="M13.976 9.15c-2.172-.806-3.356-1.426-3.356-2.409 0-.831.683-1.305 1.901-1.305 2.227 0 4.515.858 6.09 1.631l.89-5.494C18.252.975 15.697 0 12.165 0 9.667 0 7.589.654 6.104 1.872 4.56 3.147 3.757 4.992 3.757 7.218c0 4.039 2.467 5.76 6.476 7.219 2.585.92 3.445 1.574 3.445 2.583 0 .98-.84 1.545-2.354 1.545-1.875 0-4.965-.921-6.99-2.109l-.9 5.555C5.175 22.99 8.385 24 11.714 24c2.641 0 4.843-.624 6.328-1.813 1.664-1.305 2.525-3.236 2.525-5.732 0-4.128-2.524-5.851-6.591-7.305z" /></BrandSvg>);
}
export function IconPayPal(props: IconProps) {
  return (<BrandSvg {...props}><path d="M7.076 21.337H2.47a.641.641 0 0 1-.633-.74L4.944.901C5.026.382 5.474 0 5.998 0h7.46c2.57 0 4.578.543 5.69 1.81 1.01 1.15 1.304 2.42 1.012 4.287-.023.143-.047.288-.077.437-.983 5.05-4.349 6.797-8.647 6.797h-2.19c-.524 0-.968.382-1.05.9l-1.12 7.106zm14.146-14.42a3.35 3.35 0 0 0-.607-.541c-.013.076-.026.175-.041.254-.93 4.778-4.005 7.201-9.138 7.201h-2.19a.563.563 0 0 0-.556.479l-1.187 7.527h-.506l-.24 1.516a.56.56 0 0 0 .554.647h3.882c.46 0 .85-.334.922-.788.06-.26.76-4.852.816-5.09a.932.932 0 0 1 .923-.788h.58c3.76 0 6.705-1.528 7.565-5.946.36-1.847.174-3.388-.777-4.471z" /></BrandSvg>);
}
export function IconTwilio(props: IconProps) {
  return (<BrandSvg {...props}><path d="M12 0C5.381 0 0 5.381 0 12s5.381 12 12 12 12-5.381 12-12S18.619 0 12 0zm0 20.25c-4.556 0-8.25-3.694-8.25-8.25S7.444 3.75 12 3.75s8.25 3.694 8.25 8.25-3.694 8.25-8.25 8.25zm3.075-11.325a1.725 1.725 0 110 3.45 1.725 1.725 0 010-3.45zm0 4.65a1.725 1.725 0 110 3.45 1.725 1.725 0 010-3.45zm-6.15-4.65a1.725 1.725 0 110 3.45 1.725 1.725 0 010-3.45zm0 4.65a1.725 1.725 0 110 3.45 1.725 1.725 0 010-3.45z" /></BrandSvg>);
}
export function IconEmail(props: IconProps) {
  return (<Svg {...props}><path d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" /></Svg>);
}
export function IconSMS(props: IconProps) {
  return (<Svg {...props}><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z" /></Svg>);
}
export function IconLinkedIn(props: IconProps) {
  return (<BrandSvg {...props}><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" /></BrandSvg>);
}
export function IconTwitter(props: IconProps) {
  return (<BrandSvg {...props}><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" /></BrandSvg>);
}
export function IconFacebook(props: IconProps) {
  return (<BrandSvg {...props}><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z" /></BrandSvg>);
}
export function IconYouTube(props: IconProps) {
  return (<BrandSvg {...props}><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z" /></BrandSvg>);
}
export function IconTikTok(props: IconProps) {
  return (<BrandSvg {...props}><path d="M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.08-.14 1.62.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z" /></BrandSvg>);
}
export function IconXiaohongshu(props: IconProps) {
  // Real Xiaohongshu (小红书 / RedNote) wordmark: rounded red square
  // with the 小红书 text in white, mirroring the platform's app icon.
  // BrandSvg's currentColor carries the red — the wrapper sets it via
  // ``style={{ color: '#FF2741' }}`` in MCP_LOGO_COLOR. Text is white
  // and uses a Chinese-capable font stack so the chars render on
  // every platform Manor runs on.
  return (
    <BrandSvg {...props}>
      <path d="M5 0h14a5 5 0 015 5v14a5 5 0 01-5 5H5a5 5 0 01-5-5V5a5 5 0 015-5z" />
      <text
        x="12"
        y="16"
        textAnchor="middle"
        fontSize="9.5"
        fontWeight="700"
        fontFamily='"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Source Han Sans SC", "Noto Sans SC", sans-serif'
        fill="white"
        // Tighten letter-spacing so 3 glyphs fit comfortably; the
        // app icon's wordmark is very compact.
        letterSpacing="-0.5"
      >
        小红书
      </text>
    </BrandSvg>
  );
}
export function IconDatabase(props: IconProps) {
  return (<Svg {...props}><path d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" /></Svg>);
}
export function IconCloud(props: IconProps) {
  return (<Svg {...props}><path d="M2.25 15a4.5 4.5 0 004.5 4.5H18a3.75 3.75 0 001.332-7.257 3 3 0 00-3.758-3.848 5.25 5.25 0 00-10.233 2.33A4.502 4.502 0 002.25 15z" /></Svg>);
}
export function IconQRCode(props: IconProps) {
  return (<Svg {...props}><path d="M3.75 4.875c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5A1.125 1.125 0 013.75 9.375v-4.5zM3.75 14.625c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5a1.125 1.125 0 01-1.125-1.125v-4.5zM13.5 4.875c0-.621.504-1.125 1.125-1.125h4.5c.621 0 1.125.504 1.125 1.125v4.5c0 .621-.504 1.125-1.125 1.125h-4.5A1.125 1.125 0 0113.5 9.375v-4.5z" /><path d="M6.75 6.75h.75v.75h-.75zM6.75 16.5h.75v.75h-.75zM16.5 6.75h.75v.75h-.75zM13.5 13.5h.75v.75h-.75zM13.5 19.5h.75v.75h-.75zM19.5 13.5h.75v.75h-.75zM19.5 19.5h.75v.75h-.75zM16.5 16.5h.75v.75h-.75z" /></Svg>);
}

// ── Microsoft 365 brand icons ──────────────────────────────────────────────
// The Outlook / OneDrive / MS Calendar / MS Teams cards reuse generic icons
// (IconEmail / IconCloud / IconCalendar / IconChat) — color comes from the
// MCP_LOGO_COLOR registry. Excel gets its own dedicated grid mark since the
// closest generic (IconDatabase) renders as cylinders, which doesn't read as
// "spreadsheet" at 16-20px.

export function IconExcelGrid(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.75 6h16.5M3.75 10.5h16.5M3.75 15h16.5M3.75 19.5h16.5M9 3.75v16.5M14.25 3.75v16.5" />
    </Svg>
  );
}
