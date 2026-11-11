import { type ReactNode } from "react";
import { t } from "../../lib/i18n";
import { IconManorLogo } from "../icons";

interface AuthShellProps {
  children: ReactNode;
}

export function AuthBrand() {
  return (
    <div className="flex items-center gap-2">
      <div
        className="flex items-center justify-center"
        style={{ width: 32, height: 32, borderRadius: 8, background: "#000000" }}
      >
        <span aria-hidden="true" style={{ display: "flex", color: "#ffffff" }}>
          <IconManorLogo size={24} />
        </span>
      </div>
      <span style={{ fontSize: 16, fontWeight: 600, color: "var(--text-strong)" }}>
        {t("page.chat_history.manor_ai")}
      </span>
    </div>
  );
}

/** Sign-in pages: one centered column on the app canvas, nothing else. */
export default function AuthShell({ children }: AuthShellProps) {
  return (
    <div className="auth-shell">
      <div className="login-shell-panel auth-panel">
        {children}
      </div>
    </div>
  );
}
