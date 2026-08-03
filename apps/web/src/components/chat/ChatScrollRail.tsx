import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type RefObject,
} from "react";

export type ChatScrollRailMarkerTone =
  | "assistant"
  | "user"
  | "action"
  | "artifact"
  | "system";

export interface ChatScrollRailMarker {
  id: string;
  tone?: ChatScrollRailMarkerTone;
  title?: string;
  excerpt?: string;
  fileKind?: string;
  fileLabel?: string;
}

interface ChatScrollRailProps {
  containerRef: RefObject<HTMLElement | null>;
  markers?: ChatScrollRailMarker[];
  className?: string;
}

interface SampledRailMarker {
  marker: ChatScrollRailMarker;
  sourceIndex: number;
}

const MAX_MARKERS = 72;
const MARKER_GAP_PX = 10;
const MIN_RAIL_HEIGHT_PX = 72;
const MAX_RAIL_HEIGHT_PX = 560;
const RAIL_VERTICAL_INSET_PX = 64;
const MARKER_TARGET_TOP_OFFSET_PX = 16;

function cssAttributeEscape(value: string) {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(value);
  }
  return value.replace(/["\\]/g, "\\$&");
}

function sampleMarkers(markers: ChatScrollRailMarker[]): SampledRailMarker[] {
  if (markers.length <= MAX_MARKERS) {
    return markers.map((marker, sourceIndex) => ({ marker, sourceIndex }));
  }
  const sampled: SampledRailMarker[] = [];
  const last = markers.length - 1;
  for (let i = 0; i < MAX_MARKERS; i += 1) {
    const sourceIndex = Math.round((i / (MAX_MARKERS - 1)) * last);
    const marker = markers[sourceIndex];
    if (marker && sampled[sampled.length - 1]?.marker.id !== marker.id) {
      sampled.push({ marker, sourceIndex });
    }
  }
  const finalMarker = markers[last];
  if (finalMarker && sampled[sampled.length - 1]?.marker.id !== finalMarker.id) {
    sampled.push({ marker: finalMarker, sourceIndex: last });
  }
  return sampled;
}

function getMarkerTarget(
  container: HTMLElement,
  marker: ChatScrollRailMarker,
  sourceIndex: number,
) {
  const markerId = marker.id ? cssAttributeEscape(marker.id) : "";
  return (
    (markerId
      ? container.querySelector<HTMLElement>(
          `[data-chat-scroll-marker-id="${markerId}"]`,
        )
      : null) ||
    container.querySelector<HTMLElement>(
      `[data-chat-message-index="${sourceIndex}"]`,
    ) ||
    (marker.id ? document.getElementById(marker.id) : null)
  );
}

function targetScrollTopForMarker(
  container: HTMLElement,
  marker: ChatScrollRailMarker,
  sourceIndex: number,
) {
  const target = getMarkerTarget(container, marker, sourceIndex);
  if (!target) return null;
  const containerRect = container.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const targetTop = container.scrollTop + targetRect.top - containerRect.top;
  return Math.max(
    0,
    Math.min(
      container.scrollHeight - container.clientHeight,
      targetTop - MARKER_TARGET_TOP_OFFSET_PX,
    ),
  );
}

export default function ChatScrollRail({
  containerRef,
  markers = [],
  className = "",
}: ChatScrollRailProps) {
  const [snapshot, setSnapshot] = useState({
    scrollTop: 0,
    clientHeight: 0,
    scrollHeight: 0,
  });
  const [activeHoverIndex, setActiveHoverIndex] = useState<number | null>(null);

  const updateSnapshot = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    setSnapshot({
      scrollTop: el.scrollTop,
      clientHeight: el.clientHeight,
      scrollHeight: el.scrollHeight,
    });
  }, [containerRef]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;

    let frame = 0;
    const scheduleUpdate = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(updateSnapshot);
    };

    updateSnapshot();
    el.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate);
    const observer = new ResizeObserver(scheduleUpdate);
    observer.observe(el);

    return () => {
      window.cancelAnimationFrame(frame);
      el.removeEventListener("scroll", scheduleUpdate);
      window.removeEventListener("resize", scheduleUpdate);
      observer.disconnect();
    };
  }, [containerRef, updateSnapshot]);

  useEffect(() => {
    updateSnapshot();
  }, [markers.length, updateSnapshot]);

  const sampledMarkers = useMemo(() => sampleMarkers(markers), [markers]);
  const maxScroll = Math.max(0, snapshot.scrollHeight - snapshot.clientHeight);
  const availableRailHeight =
    snapshot.clientHeight > 0
      ? Math.max(
          MIN_RAIL_HEIGHT_PX,
          snapshot.clientHeight - RAIL_VERTICAL_INSET_PX,
        )
      : MAX_RAIL_HEIGHT_PX;
  const desiredRailHeight =
    sampledMarkers.length <= 1
      ? MIN_RAIL_HEIGHT_PX
      : Math.max(
          MIN_RAIL_HEIGHT_PX,
          (sampledMarkers.length - 1) * MARKER_GAP_PX,
        );
  const railHeight = Math.min(
    MAX_RAIL_HEIGHT_PX,
    availableRailHeight,
    desiredRailHeight,
  );
  const progress =
    maxScroll > 0
      ? Math.max(0, Math.min(1, snapshot.scrollTop / maxScroll))
      : 1;
  const viewportHeight =
    snapshot.scrollHeight > 0
      ? Math.max(3, (snapshot.clientHeight / snapshot.scrollHeight) * 100)
      : 100;
  const viewportTop = Math.max(
    0,
    Math.min(100 - viewportHeight, progress * (100 - viewportHeight)),
  );
  const isScrollable = maxScroll > 8;
  const activeIndex =
    sampledMarkers.length <= 1
      ? 0
      : Math.max(
          0,
          Math.min(
            sampledMarkers.length - 1,
            Math.round(progress * (sampledMarkers.length - 1)),
          ),
        );

  const scrollToMarker = (index: number) => {
    const el = containerRef.current;
    if (!el) return;
    const entry = sampledMarkers[index];
    if (entry) {
      const targetTop = targetScrollTopForMarker(
        el,
        entry.marker,
        entry.sourceIndex,
      );
      if (targetTop != null) {
        el.scrollTo({ top: targetTop, behavior: "smooth" });
        return;
      }
    }
    const ratio = entry
      ? entry.sourceIndex / Math.max(1, markers.length - 1)
      : sampledMarkers.length <= 1
        ? 0
        : index / (sampledMarkers.length - 1);
    el.scrollTo({
      top: ratio * Math.max(0, el.scrollHeight - el.clientHeight),
      behavior: "smooth",
    });
  };

  if (!isScrollable && sampledMarkers.length < 4) return null;

  return (
    <nav
      className={`chat-scroll-rail${className ? ` ${className}` : ""}`}
      role="navigation"
      aria-label="Conversation map"
      title="Conversation map"
      style={{ height: `${railHeight}px` }}
    >
      <span
        className="chat-scroll-rail__viewport"
        style={{ top: `${viewportTop}%`, height: `${viewportHeight}%` }}
        aria-hidden="true"
      />
      {sampledMarkers.map(({ marker }, index) => {
        const denominator = Math.max(1, sampledMarkers.length - 1);
        const top = denominator > 0 ? (index / denominator) * 100 : 0;
        const markerTitle =
          marker.title ||
          marker.fileLabel ||
          (marker.tone === "user"
            ? "You"
            : marker.tone === "action"
              ? "Action update"
              : "Manor AI");
        const markerExcerpt = marker.excerpt || marker.fileLabel || "";
        const isActive =
          activeHoverIndex === index ||
          (activeHoverIndex == null && index === activeIndex);

        return (
          <div
            key={`${marker.id}-${index}`}
            className="chat-scroll-rail__item"
            style={{ top: `${top}%` }}
            onMouseEnter={() => setActiveHoverIndex(index)}
            onMouseLeave={() =>
              setActiveHoverIndex((current) =>
                current === index ? null : current,
              )
            }
            onFocus={() => setActiveHoverIndex(index)}
            onBlur={() =>
              setActiveHoverIndex((current) =>
                current === index ? null : current,
              )
            }
          >
            <button
              type="button"
              className={`chat-scroll-rail__button${
                isActive ? " is-active" : ""
              }`}
              aria-label={`Jump to ${markerTitle}`}
              aria-current={isActive ? "location" : undefined}
              onClick={() => scrollToMarker(index)}
            >
              <span
                className={`chat-scroll-rail__marker chat-scroll-rail__marker--${
                  marker.tone || "assistant"
                }`}
                aria-hidden="true"
              />
            </button>
            <div className="embedded-chat-conversation-preview chat-scroll-rail__preview">
              <div className="embedded-chat-conversation-preview__title">
                {markerTitle}
              </div>
              {markerExcerpt ? (
                <div className="embedded-chat-conversation-preview__body">
                  {markerExcerpt}
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </nav>
  );
}
