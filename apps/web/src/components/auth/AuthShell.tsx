import { type MouseEvent, type ReactNode, useCallback, useRef } from "react";
import { t } from "../../lib/i18n";
import { IconCheckCircle } from "../icons";

interface AuthShellProps {
  children: ReactNode;
}

export function AuthBrand() {
  return (
    <div className="flex items-center gap-2">
      <div
        className="flex items-center justify-center"
        style={{ width: 32, height: 32, borderRadius: 10, background: "#292524" }}
      >
        <svg viewBox="0 0 1024 1024" width="16" height="16" fill="white" aria-hidden="true">
          <path d="M295.152941 0l224.376471 224.376471L743.905882 0H1024v63.247059L519.529412 567.717647 0 49.694118V0h295.152941zM0 256l243.952941 243.952941V1024H0V256z m1024 15.058824v752.941176H780.047059V515.011765L1024 271.058824z" />
        </svg>
      </div>
      <span style={{ fontSize: 18, fontWeight: 800, color: "#292524" }}>
        {t("page.chat_history.manor_ai")}
      </span>
    </div>
  );
}

export default function AuthShell({ children }: AuthShellProps) {
  const spotlightRef = useRef<HTMLDivElement>(null);
  const perspectiveRef = useRef<HTMLDivElement>(null);

  const handleMouseMove = useCallback((event: MouseEvent<HTMLDivElement>) => {
    if (spotlightRef.current) {
      spotlightRef.current.style.background = `radial-gradient(800px circle at ${event.clientX}px ${event.clientY}px, rgba(79,125,117,0.06), transparent 60%)`;
    }

    if (perspectiveRef.current) {
      const rect = perspectiveRef.current.getBoundingClientRect();
      const centerX = rect.left + rect.width / 2;
      const centerY = rect.top + rect.height / 2;
      const rotateY = ((event.clientX - centerX) / rect.width) * 6;
      const rotateX = -((event.clientY - centerY) / rect.height) * 6;
      perspectiveRef.current.style.transform = `perspective(1200px) rotateX(${rotateX}deg) rotateY(${rotateY}deg)`;
    }
  }, []);

  const handleMouseLeave = useCallback(() => {
    if (perspectiveRef.current) {
      perspectiveRef.current.style.transform = "perspective(1200px) rotateX(0deg) rotateY(0deg)";
    }
    if (spotlightRef.current) {
      spotlightRef.current.style.background = "transparent";
    }
  }, []);

  return (
    <div
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      className="min-h-screen w-full flex items-center justify-center relative overflow-hidden"
    >
      <div className="tech-bg" />
      <div ref={spotlightRef} className="spotlight" />
      <div className="blob blob-1" />
      <div className="blob blob-2" />

      <div
        className="login-shell-panel relative z-10 w-full flex overflow-hidden"
        style={{ maxWidth: 1152, height: "85vh", borderRadius: 40 }}
      >
        <div className="w-full lg:w-5/12 flex flex-col overflow-y-auto">
          {children}
        </div>

        <div
          className="hidden lg:flex lg:w-7/12 relative items-center justify-center overflow-hidden"
          style={{ borderRadius: "0 40px 40px 0" }}
          aria-hidden="true"
        >
          <div className="login-showcase-wash" style={{ position: "absolute", inset: 0, zIndex: 1 }} />

          <div
            ref={perspectiveRef}
            style={{
              position: "relative",
              zIndex: 2,
              width: "100%",
              height: "100%",
              transition: "transform 0.15s ease-out",
              transformStyle: "preserve-3d",
            }}
          >
            <div
              className="login-float-card float"
              style={{
                position: "absolute",
                top: "14%",
                left: "8%",
                transform: "translateZ(40px)",
                animationDelay: "0s",
              }}
            >
              <div style={{ width: 40, height: 40, borderRadius: 12, background: "#5f84bd", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 10 }}>
                <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18L9 11.25l4.306 4.307a11.95 11.95 0 015.814-5.519l2.74-1.22m0 0l-5.94-2.28m5.94 2.28l-2.28 5.941" />
                </svg>
              </div>
              <p style={{ color: "#1c1917", fontWeight: 700, fontSize: 15, marginBottom: 2 }}>{t("page.login.efficiency_up")}</p>
              <p style={{ color: "#78716c", fontSize: 13 }}>{t("page.login.plus_24_percent_this_week")}</p>
            </div>

            <div
              className="login-float-card float"
              style={{
                position: "absolute",
                bottom: "16%",
                right: "8%",
                transform: "translateZ(40px)",
                animationDelay: "-2s",
              }}
            >
              <div style={{ width: 40, height: 40, borderRadius: 12, background: "#54a176", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 10 }}>
                <IconCheckCircle size={20} className="text-white" />
              </div>
              <p style={{ color: "#1c1917", fontWeight: 700, fontSize: 15, marginBottom: 2 }}>{t("page.login.tasks_completed")}</p>
              <p style={{ color: "#78716c", fontSize: 13 }}>{t("page.login.12_today")}</p>
            </div>

            <div
              className="login-float-card float"
              style={{
                position: "absolute",
                top: "40%",
                left: "50%",
                marginLeft: -80,
                transform: "translateZ(40px)",
                animationDelay: "-4s",
              }}
            >
              <div style={{ width: 40, height: 40, borderRadius: 12, background: "#9079c2", display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 10 }}>
                <svg className="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
                </svg>
              </div>
              <p style={{ color: "#1c1917", fontWeight: 700, fontSize: 15, marginBottom: 2 }}>{t("page.login.ai_agents_active")}</p>
              <p style={{ color: "#78716c", fontSize: 13 }}>{t("page.login.8_running_now")}</p>
            </div>

            <div style={{ position: "absolute", bottom: "10%", left: "8%", right: "8%", zIndex: 3 }}>
              <h2 style={{ fontSize: "3rem", fontWeight: 900, color: "#1c1917", lineHeight: 1.1, marginBottom: 12 }}>
                {t("page.onboarding.step_welcome")}{" "}
                <span
                  style={{
                    background: "linear-gradient(135deg, #5d7f77, #82ada4)",
                    WebkitBackgroundClip: "text",
                    WebkitTextFillColor: "transparent",
                  }}
                >
                  {t("page.onboarding.back")}
                </span>
              </h2>
              <p style={{ color: "#57534e", fontSize: 14, lineHeight: 1.6, maxWidth: 360 }}>
                {t("page.login.your_ai_powered_business_management_platform_str")}
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
