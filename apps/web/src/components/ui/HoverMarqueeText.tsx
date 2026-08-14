import { useEffect, useRef, useState, type CSSProperties } from "react";

interface HoverMarqueeTextProps {
  text: string;
  className?: string;
  style?: CSSProperties;
}

/**
 * Keeps short labels still, but reveals the end of a truncated label on hover.
 * The static copy preserves the normal ellipsis; the moving copy is visual only
 * so assistive technology reads the label once.
 */
export default function HoverMarqueeText({
  text,
  className,
  style,
}: HoverMarqueeTextProps) {
  const viewportRef = useRef<HTMLSpanElement>(null);
  const trackRef = useRef<HTMLSpanElement>(null);
  const [overflowDistance, setOverflowDistance] = useState(0);

  useEffect(() => {
    const viewport = viewportRef.current;
    const track = trackRef.current;
    if (!viewport || !track) return;

    const measure = () => {
      const nextDistance = Math.max(
        0,
        Math.ceil(track.scrollWidth - viewport.clientWidth),
      );
      setOverflowDistance((current) =>
        current === nextDistance ? current : nextDistance,
      );
    };

    measure();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }

    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    observer.observe(track);
    return () => observer.disconnect();
  }, [text]);

  const isOverflowing = overflowDistance > 0;
  const duration = Math.min(8, Math.max(1.6, overflowDistance / 28));
  const marqueeStyle = {
    "--hover-marquee-distance": `-${overflowDistance}px`,
    "--hover-marquee-duration": `${duration}s`,
  } as CSSProperties;

  return (
    <span
      ref={viewportRef}
      className={["hover-marquee-text", className].filter(Boolean).join(" ")}
      data-overflowing={isOverflowing ? "true" : "false"}
      title={isOverflowing ? text : undefined}
      style={{ ...style, ...marqueeStyle }}
    >
      <span className="hover-marquee-text__static">{text}</span>
      <span
        ref={trackRef}
        className="hover-marquee-text__track"
        aria-hidden="true"
        data-text={text}
      />
    </span>
  );
}
