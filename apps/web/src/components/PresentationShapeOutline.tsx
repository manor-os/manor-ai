import { presentationPointsToCqh } from "../lib/presentationStyleInheritance";
import { presentationOutlineRectRadius, presentationStrokeDashArray } from "../lib/presentationShapeStyle";

/** Native outlines share units and dash patterns across editing and presentation. */
export default function PresentationShapeOutline({ stroke, strokeWidth = 1, strokeDash, presetGeom, w, h, borderRadius, points }: {
  stroke?: string; strokeWidth?: number; strokeDash?: string; presetGeom?: string;
  w: number; h: number; borderRadius?: number; points?: string;
}) {
  if (!stroke || strokeWidth <= 0) return null;
  const width = presentationPointsToCqh(strokeWidth);
  const inset = `calc(${width} / 2)`;
  const innerSize = `calc(100% - ${width})`;
  const common = {
    fill: "none", stroke, strokeWidth: width,
    strokeDasharray: presentationStrokeDashArray(strokeDash, strokeWidth), vectorEffect: "non-scaling-stroke",
  };
  return (
    <svg aria-hidden="true" viewBox={points ? "0 0 100 100" : undefined} preserveAspectRatio="none"
      style={{ position: "absolute", top: points ? inset : 0, left: points ? inset : 0, width: points ? innerSize : "100%", height: points ? innerSize : "100%", overflow: "visible", pointerEvents: "none", zIndex: 1 }}>
      {presetGeom === "line" ? <line {...common} x1={w === 0 ? "50%" : "0"} y1={h === 0 ? "50%" : "0"} x2={w === 0 ? "50%" : "100%"} y2={h === 0 ? "50%" : "100%"} />
        : points ? <polygon {...common} points={points} />
          : presetGeom === "ellipse" || presetGeom === "oval" ? <ellipse {...common} cx="50%" cy="50%" rx={`calc(50% - ${inset})`} ry={`calc(50% - ${inset})`} />
            : <rect {...common} x={inset} y={inset} width={innerSize} height={innerSize} rx={presentationOutlineRectRadius(presetGeom, w, h, borderRadius, inset)} />}
    </svg>
  );
}
