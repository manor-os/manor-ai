/**
 * OneLineText — one line of text that never wraps and never moves. Copy is
 * written to fit; if it still does not, the line ends with an ellipsis and
 * the full text shows on hover.
 */
import { useEffect, useRef, useState, type ElementType } from "react";

export default function OneLineText({ text, as: Tag = "span", className }: {
  text: string;
  as?: ElementType;
  className?: string;
}) {
  const box = useRef<HTMLElement | null>(null);
  const [cut, setCut] = useState(false);
  useEffect(() => {
    const measure = () => setCut(Boolean(box.current && box.current.scrollWidth > box.current.clientWidth));
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (observer && box.current) observer.observe(box.current);
    return () => observer?.disconnect();
  }, [text]);
  return <Tag ref={box} className={`one-line-text${className ? ` ${className}` : ""}`} title={cut ? text : undefined}>{text}</Tag>;
}
