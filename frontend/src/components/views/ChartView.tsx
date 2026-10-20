import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { downloadCsv, fmt, niceTicks } from "../../format";
import type { Display } from "../../types";
import { TableView } from "./TableView";

type ChartDisplay = Extract<Display, { kind: "chart" }>;

const COLORS = ["#4fb3e8", "#f2a541", "#e86ba0", "#5ec27a", "#9aa6ff", "#e8d44f"];
const M = { l: 70, r: 20, t: 16, b: 46 };

export function ChartView({ display }: { display: ChartDisplay }) {
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 600, h: 360 });
  const [hoverX, setHoverX] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ w: Math.max(240, width), h: Math.max(200, height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const scales = useMemo(() => {
    const xs: number[] = [];
    const ys: number[] = [];
    for (const s of display.series) {
      s.x.forEach((x, i) => {
        const y = s.y[i];
        if (x !== null && y !== null && Number.isFinite(x) && Number.isFinite(y)) {
          xs.push(x);
          ys.push(y);
        }
      });
    }
    if (!xs.length) return null;
    // Loops rather than Math.min(...arr): spreading long curves overflows the call stack.
    const lo = (a: number[]) => a.reduce((m, v) => (v < m ? v : m), Infinity);
    const hi = (a: number[]) => a.reduce((m, v) => (v > m ? v : m), -Infinity);
    const xt = niceTicks(lo(xs), hi(xs));
    const yt = niceTicks(lo(ys), hi(ys));
    const x0 = Math.min(xt[0], lo(xs)), x1 = Math.max(xt[xt.length - 1], hi(xs));
    const y0 = Math.min(yt[0], lo(ys)), y1 = Math.max(yt[yt.length - 1], hi(ys));
    return { xt, yt, x0, x1: x1 === x0 ? x0 + 1 : x1, y0, y1: y1 === y0 ? y0 + 1 : y1 };
  }, [display]);

  const W = size.w, H = size.h;
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  if (!scales) return <div className="empty">No data to plot</div>;
  const sx = (x: number) => M.l + ((x - scales.x0) / (scales.x1 - scales.x0)) * pw;
  const sy = (y: number) => M.t + ph - ((y - scales.y0) / (scales.y1 - scales.y0)) * ph;

  const paths = display.series.map((s) => {
    let d = "";
    let pen = false;
    s.x.forEach((x, i) => {
      const y = s.y[i];
      if (x === null || y === null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${sx(x).toFixed(1)},${sy(y).toFixed(1)}`;
      pen = true;
    });
    return d;
  });

  const nearest = hoverX === null ? [] : display.series.map((s) => {
    let best = -1, dist = Infinity;
    s.x.forEach((x, i) => {
      if (x === null || s.y[i] === null) return;
      const d = Math.abs(x - hoverX);
      if (d < dist) {
        dist = d;
        best = i;
      }
    });
    return best >= 0 ? { x: s.x[best] as number, y: s.y[best] as number } : null;
  });

  const exportCsv = () => {
    const header = display.series.flatMap((s) => [`${s.name} x`, `${s.name} y`]);
    const n = display.series.reduce((m, s) => Math.max(m, s.x.length), 0);
    const rows = Array.from({ length: n }, (_, i) => display.series.flatMap((s) => [s.x[i] ?? null, s.y[i] ?? null]));
    downloadCsv(`${display.title || "chart"}.csv`, header, rows);
  };

  return (
    <div className="chart">
      <div className="view-toolbar">
        <div className="legend-row">
          {display.series.map((s, i) => (
            <span key={i}>
              <i style={{ background: COLORS[i % COLORS.length] }} />
              {s.name}
            </span>
          ))}
        </div>
        {display.table && (
          <button onClick={() => setAsTable(!asTable)} title="Rows = curves, columns as a Formula indexes them">
            {asTable ? "Chart" : "Table"}
          </button>
        )}
        <button onClick={exportCsv}>CSV</button>
      </div>
      {asTable && display.table && <TableView display={display.table} />}
      <div className="chart-box" ref={box} style={asTable ? { display: "none" } : undefined}>
        <svg
          width={W}
          height={H}
          onMouseMove={(e) => {
            const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
            const px = e.clientX - r.left;
            setHoverX(px < M.l || px > M.l + pw ? null : scales.x0 + ((px - M.l) / pw) * (scales.x1 - scales.x0));
          }}
          onMouseLeave={() => setHoverX(null)}
        >
          {scales.yt.map((t) => (
            <g key={`y${t}`}>
              <line x1={M.l} x2={M.l + pw} y1={sy(t)} y2={sy(t)} className="grid" />
              <text x={M.l - 8} y={sy(t)} className="tick" textAnchor="end" dominantBaseline="middle">
                {fmt(t)}
              </text>
            </g>
          ))}
          {scales.xt.map((t) => (
            <g key={`x${t}`}>
              <line y1={M.t} y2={M.t + ph} x1={sx(t)} x2={sx(t)} className="grid" />
              <text x={sx(t)} y={M.t + ph + 16} className="tick" textAnchor="middle">
                {fmt(t)}
              </text>
            </g>
          ))}
          <rect x={M.l} y={M.t} width={pw} height={ph} className="frame" />
          <text x={M.l + pw / 2} y={H - 8} className="axis-label" textAnchor="middle">
            {display.xlabel}
          </text>
          <text
            transform={`translate(14 ${M.t + ph / 2}) rotate(-90)`}
            className="axis-label"
            textAnchor="middle"
          >
            {display.ylabel}
          </text>
          {paths.map((d, i) => (
            <path key={i} d={d} fill="none" stroke={COLORS[i % COLORS.length]} strokeWidth={2} />
          ))}
          {display.series.map((s, i) =>
            s.x.length <= 60
              ? s.x.map((x, j) =>
                  x !== null && s.y[j] !== null ? (
                    <circle key={`${i}-${j}`} cx={sx(x)} cy={sy(s.y[j] as number)} r={2.5}
                      fill={COLORS[i % COLORS.length]} />
                  ) : null,
                )
              : null,
          )}
          {hoverX !== null && (
            <g>
              <line x1={sx(hoverX)} x2={sx(hoverX)} y1={M.t} y2={M.t + ph} className="crosshair" />
              {nearest.map((p, i) =>
                p ? (
                  <g key={i}>
                    <circle cx={sx(p.x)} cy={sy(p.y)} r={5} fill="none" stroke={COLORS[i % COLORS.length]}
                      strokeWidth={2} />
                    <text x={sx(p.x) + 8} y={sy(p.y) - 8 - i * 14} className="hover-label">
                      ({fmt(p.x)}, {fmt(p.y)})
                    </text>
                  </g>
                ) : null,
              )}
            </g>
          )}
        </svg>
      </div>
    </div>
  );
}
