import { useState, useEffect, useCallback, useRef, type CSSProperties } from "react";
import { t } from "../lib/i18n";

/**
 * Generic spotlight tour engine.
 *
 * Highlights UI regions one at a time with a brief explanation. Used by the
 * sidebar onboarding tour (OnboardingTour).
 * Auto-shows once per `storageKey`, can be re-triggered via `startEvent`.
 */

export interface TourStep {
  /** CSS selector for the highlight target. Omit for a centered intro card. */
  target?: string;
  title: string;
  description: string;
  position?: "top" | "bottom" | "left" | "right";
  /** Sidebar mode the target lives in; the tour switches the app there before measuring. */
  mode?: "chat" | "workspace";
  /** Target lives inside the collapsible Configure menu; ask the sidebar to open it. */
  openConfigure?: boolean;
}

type TourPlacement = NonNullable<TourStep["position"]>;

const TARGET_PADDING = 8;
const VIEWPORT_MARGIN = 16;
const TOOLTIP_WIDTH = 320;
const TOOLTIP_ESTIMATED_HEIGHT = 170;
const TOOLTIP_GAP = 16;
const TARGET_RETRY_MS = 120;
const MAX_TARGET_ATTEMPTS = 15;

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

/** First match that actually renders — a selector can hit hidden duplicates. */
function findTourTarget(selector: string): Element | null {
  const candidates = Array.from(document.querySelectorAll(selector));
  return candidates.find((el) => el.getClientRects().length > 0) ?? null;
}

interface SpotlightTourProps {
  steps: TourStep[];
  /** localStorage key that marks this tour as completed. */
  storageKey: string;
  /** Optional window event name that restarts the tour from step one. */
  startEvent?: string;
  /** Delay before the tour auto-shows for accounts that haven't seen it. */
  autoStartDelay?: number;
  /** Gate: when false the tour never auto-shows (it can still be event-started). */
  enabled?: boolean;
}

export default function SpotlightTour({
  steps,
  storageKey,
  startEvent,
  autoStartDelay = 800,
  enabled = true,
}: SpotlightTourProps) {
  const [currentStep, setCurrentStep] = useState(0);
  const [visible, setVisible] = useState(false);
  const [targetRect, setTargetRect] = useState<DOMRect | null>(null);
  const [viewport, setViewport] = useState(() => viewportSize());
  const directionRef = useRef<"forward" | "backward">("forward");

  // Auto-show once per storageKey
  useEffect(() => {
    if (!enabled) {
      setVisible(false);
      return;
    }
    const completed = localStorage.getItem(storageKey);
    if (!completed) {
      const timer = setTimeout(() => setVisible(true), autoStartDelay);
      return () => clearTimeout(timer);
    }
  }, [enabled, storageKey, autoStartDelay]);

  // Allow manual re-trigger via custom event. An explicit start bypasses the
  // `enabled` gate — that gate only governs auto-showing.
  useEffect(() => {
    if (!startEvent) return;
    const handleStart = () => {
      directionRef.current = "forward";
      setCurrentStep(0);
      setVisible(true);
    };
    window.addEventListener(startEvent, handleStart);
    return () => window.removeEventListener(startEvent, handleStart);
  }, [startEvent]);

  const finish = useCallback(() => {
    localStorage.setItem(storageKey, "true");
    setVisible(false);
  }, [storageKey]);

  const next = useCallback(() => {
    directionRef.current = "forward";
    if (currentStep < steps.length - 1) {
      setCurrentStep((s) => s + 1);
    } else {
      finish();
    }
  }, [currentStep, steps.length, finish]);

  const back = useCallback(() => {
    directionRef.current = "backward";
    if (currentStep > 0) setCurrentStep((s) => s - 1);
  }, [currentStep]);

  // A step whose target never appears is skipped in the direction of travel
  // instead of showing a tooltip that points at nothing.
  const skipMissingStep = useCallback(() => {
    if (directionRef.current === "backward") {
      if (currentStep > 0) {
        setCurrentStep((s) => s - 1);
        return;
      }
      directionRef.current = "forward";
    }
    if (currentStep < steps.length - 1) {
      setCurrentStep((s) => s + 1);
    } else {
      finish();
    }
  }, [currentStep, steps.length, finish]);

  // Find the target, bring it into view, then measure against the viewport.
  useEffect(() => {
    if (!visible) return;
    const step = steps[currentStep];
    if (!step) return;

    // Targets like the workspace nav only exist in one sidebar mode; ask the
    // app shell to switch there before measuring (AppLayout listens for this).
    if (step.mode) {
      window.dispatchEvent(new CustomEvent("manor:tour-mode", { detail: { mode: step.mode } }));
    }
    if (step.openConfigure) {
      window.dispatchEvent(new Event("manor:tour-configure"));
    }

    // The component can mount while the window is still settling (early page
    // load, browser pane resizes), so keep the viewport in sync for every
    // step — a stale small width would wrongly switch to the mobile layout.
    const syncViewport = () => setViewport(viewportSize());
    syncViewport();
    window.addEventListener("resize", syncViewport);

    if (!step.target) {
      // Intro step — centered card over a dimmed page, nothing to measure.
      setTargetRect(null);
      return () => window.removeEventListener("resize", syncViewport);
    }

    // Layout keeps shifting after a step mounts (banners appearing, sidebar
    // animations, fonts) and none of that fires scroll/resize events, so a
    // one-shot measurement strands the spotlight on a stale rect. Track the
    // target every frame while the tour is up and follow it when it moves.
    let cancelled = false;
    let attempts = 0;
    let el: Element | null = null;
    let lastRect: DOMRect | null = null;
    let scrolledIntoView = false;
    let raf = 0;

    const track = () => {
      if (cancelled) return;
      if (!el || !el.isConnected || el.getClientRects().length === 0) {
        el = findTourTarget(step.target!);
        if (!el) {
          setTargetRect(null);
          lastRect = null;
          // Mode switches and route changes need a few frames to mount the target.
          if (attempts >= MAX_TARGET_ATTEMPTS) {
            skipMissingStep();
            return;
          }
          attempts += 1;
          raf = window.requestAnimationFrame(() => {
            window.setTimeout(track, TARGET_RETRY_MS);
          });
          return;
        }
        attempts = 0;
        scrolledIntoView = false;
      }
      const rect = el.getBoundingClientRect();
      if (!scrolledIntoView) {
        scrolledIntoView = true;
        const outsideViewport =
          rect.top < 96 ||
          rect.bottom > window.innerHeight - 96 ||
          rect.left < VIEWPORT_MARGIN ||
          rect.right > window.innerWidth - VIEWPORT_MARGIN;
        if (outsideViewport && "scrollIntoView" in el) {
          el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
        }
      }
      const moved =
        !lastRect ||
        Math.abs(rect.left - lastRect.left) > 0.5 ||
        Math.abs(rect.top - lastRect.top) > 0.5 ||
        Math.abs(rect.width - lastRect.width) > 0.5 ||
        Math.abs(rect.height - lastRect.height) > 0.5;
      if (moved) {
        lastRect = rect;
        setTargetRect(rect);
      }
      raf = window.requestAnimationFrame(track);
    };

    track();
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", syncViewport);
    };
  }, [visible, currentStep, steps, skipMissingStep]);

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

  if (!visible) return null;

  const step = steps[currentStep];
  if (!step) return null;
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
    transition: "left 0.35s ease, top 0.35s ease",
    animation: "tour-fade-in 0.25s ease",
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
    // Intro steps and missing targets: center the card.
    tooltipStyle.left = "50%";
    tooltipStyle.top = "50%";
    tooltipStyle.transform = "translate(-50%, -50%)";
  }

  const chatStep = !!step.target?.includes("chat-input");
  // The chat step can land on the round FloatingChat bubble or the wide
  // page composer — only draw a circle when the target itself is round.
  const roundTarget =
    chatStep && targetRect ? Math.abs(targetRect.width - targetRect.height) < 8 : false;
  // Spotlight hole = one element whose giant box-shadow dims the rest of the
  // page, so the hole and its ring always move together in one transition.
  // Without a target (intro steps) the hole collapses to a point mid-screen.
  const hole = targetRect
    ? {
        left: targetRect.left - padding,
        top: targetRect.top - padding,
        width: targetRect.width + padding * 2,
        height: targetRect.height + padding * 2,
      }
    : {
        left: viewport.width / 2,
        top: viewport.height / 2,
        width: 0,
        height: 0,
      };
  const DIM_SHADOW = "0 0 0 200vmax rgba(28,25,23,0.5)";

  return (
    <>
      <style>{`
        @keyframes tour-glow {
          0%, 100% { box-shadow: 0 0 0 6px rgba(67,107,101,0.2), 0 0 20px rgba(67,107,101,0.3), ${DIM_SHADOW}; }
          50% { box-shadow: 0 0 0 10px rgba(67,107,101,0.15), 0 0 30px rgba(67,107,101,0.4), ${DIM_SHADOW}; }
        }
        @keyframes tour-fade-in {
          from { opacity: 0; }
          to { opacity: 1; }
        }
      `}</style>
      {/* Overlay; the child spotlight's box-shadow paints the dim layer */}
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 10000,
          pointerEvents: "auto",
          overflow: "hidden",
          animation: "tour-fade-in 0.25s ease",
        }}
        onClick={next}
      >
        <div
          style={{
            position: "fixed",
            ...hole,
            borderRadius: roundTarget ? "50%" : 12,
            border: targetRect ? "2px solid rgba(67,107,101,0.6)" : "none",
            boxShadow:
              targetRect && !chatStep
                ? `0 0 0 4px rgba(67,107,101,0.15), ${DIM_SHADOW}`
                : DIM_SHADOW,
            pointerEvents: "none",
            transition: "all 0.35s ease",
            animation: chatStep && targetRect ? "tour-glow 1.5s ease-in-out infinite" : undefined,
          }}
        />
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
          {/* Progress: dots for short tours, a counter once dots get crowded */}
          {steps.length > 6 ? (
            <span
              className="mono"
              style={{ fontSize: 11, fontWeight: 600, color: "var(--text-faint)", letterSpacing: "0.02em" }}
            >
              {currentStep + 1} / {steps.length}
            </span>
          ) : (
            <div style={{ display: "flex", gap: 5 }}>
              {steps.map((_, i) => (
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
          )}

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
              {currentStep === steps.length - 1 ? t("page.team_people.done") : t("page.onboarding.next")}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
