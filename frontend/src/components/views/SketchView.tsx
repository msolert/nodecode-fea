import { useLayoutEffect, useRef, useState } from "react";
import { fmt } from "../../format";
import type { Display } from "../../types";

type SketchDisplay = Extract<Display, { kind: "sketch" }>;

/** A sketch (or a part's profile) drawn to scale in the XY plane, with the x and y axes through the origin. */
export function SketchView({ display }: { display: SketchDisplay }) {
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 600, h: 360 });
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ w: Math.max(200, width), h: Math.max(160, height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const pts = display.lines.flat();
  if (!pts.length) return <div className="empty">Nothing drawn yet.</div>;
  const xs = [0, ...pts.map((p) => p[0])];
  const ys = [0, ...pts.map((p) => p[1])];
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const pad = 28;
  const k = Math.min((size.w - 2 * pad) / Math.max(x1 - x0, 1e-9), (size.h - 2 * pad) / Math.max(y1 - y0, 1e-9));
  // centre the drawing; y up
  const ox = (size.w - k * (x1 - x0)) / 2 - k * x0;
  const oy = (size.h + k * (y1 - y0)) / 2 + k * y0;
  const X = (x: number) => ox + k * x;
  const Y = (y: number) => oy - k * y;

  return (
    <div className="chart">
      <div className="chart-box" ref={box}>
        <svg width={size.w} height={size.h} role="img" aria-label={display.title}>
          <line x1={0} x2={size.w} y1={Y(0)} y2={Y(0)} stroke="var(--border)" />
          <line x1={X(0)} x2={X(0)} y1={0} y2={size.h} stroke="var(--border)" />
          <text x={size.w - 6} y={Y(0) - 4} fill="var(--muted)" fontSize={11} textAnchor="end">x</text>
          <text x={X(0) + 4} y={12} fill="var(--muted)" fontSize={11}>y</text>
          {display.lines.map((line, i) => (
            <polyline key={i} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round"
              points={line.map(([x, y]) => `${X(x)},${Y(y)}`).join(" ")} />
          ))}
          <text x={6} y={size.h - 6} fill="var(--muted)" fontSize={11}>
            x {fmt(x0)} … {fmt(x1)}, y {fmt(y0)} … {fmt(y1)}
          </text>
        </svg>
      </div>
    </div>
  );
}
