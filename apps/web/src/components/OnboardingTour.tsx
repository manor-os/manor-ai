import { useState, useEffect, useCallback, type CSSProperties } from "react";
import { useLocation } from "react-router-dom";
import { t } from "../lib/i18n";


/**
 * Lightweight spotlight tour for new users.
 * Highlights UI sections one at a time with a brief explanation.
 * User can navigate forward/back or skip entirely.
 */

interface TourStep {
  target: string; // CSS selector or data-tour attribute
  title: string;
  description: string;
  position?: "top" | "bottom" | "left" | "right";
}

type TourPlacement = NonNullable<TourStep["position"]>;

const TOUR_STEPS: TourStep[] = [
  {
    target: "[data-tour='mode-switcher']",
    title: t("component.onboarding_tour.chat_and_workspace"),
    description: t("component.onboarding_tour.switch_between_chatting_with_ai_agents_and_managing_yo"),
    position: "right",
  },
  {
    target: "[data-tour='chat-input']",
    title: t("component.onboarding_tour.ai_chat"),
    description: t("component.onboarding_tour.ask_anything_your_ai_can_search_the_web_write_files_cr"),
    position: "top",
  },
  {
    target: "[data-tour='nav-workspaces']",
    title: t("nav.workspaces"),
    description: t("component.onboarding_tour.organize_your_operations_into_workspaces_each_one_has"),
    position: "right",
  },
  {
    target: "[data-tour='nav-knowledge']",
    title: t("page.knowledge.knowledge_base"),
    description: t("component.onboarding_tour.upload_documents_here_your_ai_agents_can_search_and_re"),
    position: "right",
  },
  {
    target: "[data-tour='nav-team']",
    title: t("nav.team"),
    description: t("component.onboarding_tour.invite_teammates_manage_roles_and_decide_who_can_view"),
    position: "right",
  },
  {
    target: "[data-tour='configure-menu']",
    title: t("page.apps.configure"),
    description: t("component.onboarding_tour.set_up_advanced_capabilities_here_agents_integrations"),
    position: "right",
  },
];

const STORAGE_KEY = "manor_tour_completed";
const TARGET_PADDING = 8;
const VIEWPORT_MARGIN = 16;
const TOOLTIP_WIDTH = 320;
const TOOLTIP_ESTIMATED_HEIGHT = 170;
const TOOLTIP_GAP = 16;

function clamp(value: number, min: number, max: number) {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

function viewportSize() {
  if (typeof window === "undefined") {
    return { width: 1024, height: 768 };
  }
  return {
    width: window.innerWidth,
    height: window.innerHeight,
  };
}

export function isTourSuppressedPath(pathname: string) {
  return (
    pathname.startsWith("/editor/") ||
    pathname.startsWith("/viewer/") ||
    pathname.startsWith("/diagram-canvas")
  );
}

export default function OnboardingTour() {
  const location = useLocation();
  const [currentStep, setCurrentStep] = useState(0);
  const [visible, setVisible] = useState(false);
  const [targetRect, setTargetRect] = useState<DOMRect | null>(null);
  const [viewport, setViewport] = useState(() => viewportSize());
  const tourSuppressedPath = isTourSuppressedPath(location.pathname);

  // Check if tour should show (auto for new accounts)
  useEffect(() => {
    if (tourSuppressedPath) {
      setVisible(false);
      return;
    }
    const completed = localStorage.getItem(STORAGE_KEY);
    if (!completed) {
      const timer = setTimeout(() => setVisible(true), 800);
      return () => clearTimeout(timer);
    }
  }, [tourSuppressedPath]);

  // Allow manual re-trigger via custom event
  useEffect(() => {
    const handleStart = () => {
      if (tourSuppressedPath) return;
      setCurrentStep(0);
      setVisible(true);
    };
    window.addEventListener("manor:start-tour", handleStart);
    return () => window.removeEventListener("manor:start-tour", handleStart);
  }, [tourSuppressedPath]);

  // Find the target, bring it into view, then measure against the viewport.
  useEffect(() => {
    if (!visible) return;
    const step = TOUR_STEPS[currentStep];
    if (!step) return;

    let settleTimer: number | undefined;

    const measureTarget = () => {
      setViewport(viewportSize());
      const el = document.querySelector(step.target);
      if (el) {
        const rect = el.getBoundingClientRect();
        const outsideViewport =
          rect.top < 96 ||
          rect.bottom > window.innerHeight - 96 ||
          rect.left < VIEWPORT_MARGIN ||
          rect.right > window.innerWidth - VIEWPORT_MARGIN;

        if (outsideViewport && "scrollIntoView" in el) {
          el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
        }

        window.requestAnimationFrame(() => {
          setViewport(viewportSize());
          setTargetRect(el.getBoundingClientRect());
        });
      } else {
        setTargetRect(null);
      }
    };

    measureTarget();
    settleTimer = window.setTimeout(measureTarget, 280);
    window.addEventListener("resize", measureTarget);
    window.addEventListener("scroll", measureTarget, true);
    return () => {
      if (settleTimer) window.clearTimeout(settleTimer);
      window.removeEventListener("resize", measureTarget);
      window.removeEventListener("scroll", measureTarget, true);
    };
  }, [visible, currentStep, location.pathname]);

  const finish = useCallback(() => {
    localStorage.setItem(STORAGE_KEY, "true");
    setVisible(false);
  }, []);

  const next = useCallback(() => {
    if (currentStep < TOUR_STEPS.length - 1) {
      setCurrentStep((s) => s + 1);
    } else {
      finish();
    }
  }, [currentStep, finish]);

  const back = useCallback(() => {
    if (currentStep > 0) setCurrentStep((s) => s - 1);
  }, [currentStep]);

  // Keyboard support
  useEffect(() => {
    if (!visible) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") finish();
      if (e.key === "ArrowRight" || e.key === "Enter") next();
      if (e.key === "ArrowLeft") back();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [visible, next, back, finish]);

  if (!visible || tourSuppressedPath) return null;

  const step = TOUR_STEPS[currentStep];
  const padding = TARGET_PADDING;
  const tooltipWidth = Math.min(TOOLTIP_WIDTH, viewport.width - VIEWPORT_MARGIN * 2);
  const compactTooltip = viewport.width < 640;

  // Tooltip position
  let tooltipStyle: CSSProperties = {
    position: "fixed",
    zIndex: 10001,
    width: compactTooltip ? `calc(100vw - ${VIEWPORT_MARGIN * 2}px)` : tooltipWidth,
    maxHeight: `calc(100vh - ${VIEWPORT_MARGIN * 2}px)`,
    overflowY: "auto",
    boxSizing: "border-box",
    padding: "20px 24px",
    background: "color-mix(in srgb, var(--surface-panel) 97%, transparent)",
    backdropFilter: "blur(12px)",
    borderRadius: 16,
    border: "1px solid var(--border-default)",
    boxShadow: "var(--shadow-lg)",
  };

  if (compactTooltip) {
    tooltipStyle.left = VIEWPORT_MARGIN;
    tooltipStyle.right = VIEWPORT_MARGIN;
    tooltipStyle.bottom = VIEWPORT_MARGIN;
  } else if (targetRect) {
    const preferred: TourPlacement = step.position || "right";
    const available = {
      right: viewport.width - targetRect.right - VIEWPORT_MARGIN,
      left: targetRect.left - VIEWPORT_MARGIN,
      bottom: viewport.height - targetRect.bottom - VIEWPORT_MARGIN,
      top: targetRect.top - VIEWPORT_MARGIN,
    };
    const candidates: TourPlacement[] = Array.from(new Set<TourPlacement>([preferred, "bottom", "top", "right", "left"]));
    const pos =
      candidates.find((candidate) => {
        if (candidate === "right" || candidate === "left") return available[candidate] >= tooltipWidth + TOOLTIP_GAP;
        return available[candidate] >= TOOLTIP_ESTIMATED_HEIGHT + TOOLTIP_GAP;
      }) || preferred;

    const maxLeft = viewport.width - tooltipWidth - VIEWPORT_MARGIN;
    const centeredLeft = targetRect.left + targetRect.width / 2 - tooltipWidth / 2;
    const centeredTop = targetRect.top + targetRect.height / 2 - TOOLTIP_ESTIMATED_HEIGHT / 2;

    if (pos === "right") {
      tooltipStyle.left = clamp(targetRect.right + TOOLTIP_GAP, VIEWPORT_MARGIN, maxLeft);
      tooltipStyle.top = clamp(centeredTop, VIEWPORT_MARGIN, viewport.height - TOOLTIP_ESTIMATED_HEIGHT - VIEWPORT_MARGIN);
    } else if (pos === "left") {
      tooltipStyle.left = clamp(targetRect.left - tooltipWidth - TOOLTIP_GAP, VIEWPORT_MARGIN, maxLeft);
      tooltipStyle.top = clamp(centeredTop, VIEWPORT_MARGIN, viewport.height - TOOLTIP_ESTIMATED_HEIGHT - VIEWPORT_MARGIN);
    } else if (pos === "bottom") {
      tooltipStyle.left = clamp(centeredLeft, VIEWPORT_MARGIN, maxLeft);
      tooltipStyle.top = clamp(targetRect.bottom + TOOLTIP_GAP, VIEWPORT_MARGIN, viewport.height - TOOLTIP_ESTIMATED_HEIGHT - VIEWPORT_MARGIN);
    } else if (pos === "top") {
      tooltipStyle.left = clamp(centeredLeft, VIEWPORT_MARGIN, maxLeft);
      tooltipStyle.top = clamp(targetRect.top - TOOLTIP_ESTIMATED_HEIGHT - TOOLTIP_GAP, VIEWPORT_MARGIN, viewport.height - TOOLTIP_ESTIMATED_HEIGHT - VIEWPORT_MARGIN);
    }
  } else {
    // Fallback: center
    tooltipStyle.left = "50%";
    tooltipStyle.top = "50%";
    tooltipStyle.transform = "translate(-50%, -50%)";
  }

  return (
    <>
      <style>{`
        @keyframes tour-glow {
          0%, 100% { box-shadow: 0 0 0 6px rgba(67,107,101,0.2), 0 0 20px rgba(67,107,101,0.3); }
          50% { box-shadow: 0 0 0 10px rgba(67,107,101,0.15), 0 0 30px rgba(67,107,101,0.4); }
        }
      `}</style>
      {/* Overlay with spotlight hole */}
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 10000,
          pointerEvents: "auto",
        }}
        onClick={next}
      >
        <svg width="100%" height="100%" style={{ position: "absolute", inset: 0 }}>
          <defs>
            <mask id="tour-mask">
              <rect width="100%" height="100%" fill="white" />
              {targetRect && (
                <rect
                  x={targetRect.left - padding}
                  y={targetRect.top - padding}
                  width={targetRect.width + padding * 2}
                  height={targetRect.height + padding * 2}
                  rx={12}
                  fill="black"
                />
              )}
            </mask>
          </defs>
          <rect
            width="100%"
            height="100%"
            fill="rgba(28,25,23,0.5)"
            mask="url(#tour-mask)"
          />
        </svg>

        {/* Spotlight ring */}
        {targetRect && (
          <div
            style={{
              position: "fixed",
              left: targetRect.left - padding,
              top: targetRect.top - padding,
              width: targetRect.width + padding * 2,
              height: targetRect.height + padding * 2,
              borderRadius: step.target.includes("chat-input") ? "50%" : 12,
              border: "2px solid rgba(67,107,101,0.6)",
              boxShadow: step.target.includes("chat-input")
                ? "0 0 0 6px rgba(67,107,101,0.2), 0 0 20px rgba(67,107,101,0.3)"
                : "0 0 0 4px rgba(67,107,101,0.15)",
              pointerEvents: "none",
              transition: "all 0.3s ease",
              animation: step.target.includes("chat-input") ? "tour-glow 1.5s ease-in-out infinite" : undefined,
            }}
          />
        )}
      </div>

      {/* Tooltip */}
      <div style={tooltipStyle} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ fontSize: 16, fontWeight: 700, color: "var(--text-strong)", margin: "0 0 8px" }}>
          {step.title}
        </h3>
        <p style={{ fontSize: 13, color: "var(--text-muted)", margin: "0 0 20px", lineHeight: 1.5 }}>
          {step.description}
        </p>

        {/* Footer */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          {/* Progress dots */}
          <div style={{ display: "flex", gap: 5 }}>
            {TOUR_STEPS.map((_, i) => (
              <div
                key={i}
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  background: i === currentStep ? "var(--accent)" : "var(--border-default)",
                  transition: "background 0.2s",
                }}
              />
            ))}
          </div>

          {/* Buttons */}
          <div style={{ display: "flex", gap: 8 }}>
            <button
              onClick={finish}
              style={{
                fontSize: 12,
                fontWeight: 500,
                color: "var(--text-faint)",
                background: "none",
                border: "none",
                cursor: "pointer",
                padding: "6px 10px",
              }}
            >
              {t("component.onboarding_tour.skip")}</button>
            {currentStep > 0 && (
              <button
                onClick={back}
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: "var(--text-default)",
                  background: "var(--surface-muted)",
                  border: "none",
                  borderRadius: 8,
                  cursor: "pointer",
                  padding: "6px 14px",
                }}
              >
                {t("page.onboarding.back")}</button>
            )}
            <button
              onClick={next}
              style={{
                fontSize: 12,
                fontWeight: 600,
                color: "#fff",
                background: "#436b65",
                border: "none",
                borderRadius: 8,
                cursor: "pointer",
                padding: "6px 14px",
              }}
            >
              {currentStep === TOUR_STEPS.length - 1 ? t("page.team_people.done") : t("page.onboarding.next")}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
