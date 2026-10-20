import { useEffect, useRef, useState } from "react";
import { fmt } from "../format";
import type { Display, InspectedItem, NodeDef, NodeResult } from "../types";
import { ChartView } from "./views/ChartView";
import { SketchView } from "./views/SketchView";
import { ContourView } from "./views/ContourView";
import { ModelView } from "./views/ModelView";
import { TableView } from "./views/TableView";

export function Viewer({ def, name, nodeId, result, inspect, dock, onDock, onCollapse, pinned, onPin, editor }: {
  def?: NodeDef;
  /** Shown instead of the result's preview (a typed list/matrix as an editable grid). */
  editor?: React.ReactNode;
  name?: string;
  nodeId?: string;
  /** One item of the shown node's nested result (path: one index per list level). */
  inspect?: (path: number[]) => Promise<InspectedItem>;
  pinned?: boolean;
  onPin?: () => void;
  result?: NodeResult;
  dock: "bottom" | "right";
  onDock: () => void;
  onCollapse: () => void;
}) {
  // Looking inside a list result: one index per level ([] = the whole result).
  const [path, setPath] = useState<number[]>([]);
  const [item, setItem] = useState<InspectedItem | null>(null);
  const [itemError, setItemError] = useState("");
  const inspectRef = useRef(inspect);
  inspectRef.current = inspect;
  useEffect(() => setPath([]), [nodeId]);
  useEffect(() => {
    setItemError("");
    if (!path.length || !inspectRef.current || result?.status !== "ok") return setItem(null);
    let live = true;
    inspectRef.current(path)
      .then((r) => live && setItem(r))
      .catch((e: Error) => live && (setItem(null), setItemError(e.message)));
    return () => {
      live = false;
    };
  }, [path, result]);

  const actions = (
    <span className="viewer-actions">
      {onPin && (
        <button className={`icon-btn ${pinned ? "on" : ""}`} onClick={onPin}
          title={pinned ? "Unpin: show the node you click" : "Pin: keep showing this node while you click and edit others (it updates live)"}>
          📌
        </button>
      )}
      <button className="icon-btn" onClick={onDock} title={dock === "right" ? "Dock at the bottom" : "Dock on the right"}>
        {dock === "right" ? "⬓" : "◨"}
      </button>
      <button className="icon-btn" onClick={onCollapse} title="Hide the data panel (Ctrl+J)">
        ✕
      </button>
    </span>
  );
  if (!def) {
    return (
      <div className="viewer">
        <div className="viewer-head">
          <span className="muted">Data</span>
          {actions}
        </div>
        <div className="viewer-body empty-body">
          <p className="muted">Select a node to see its data.</p>
        </div>
      </div>
    );
  }
  const d = result?.display;
  const shape = result?.status === "ok" ? result.shape : undefined;
  // Pickers for lists of lists, and for lists the panel doesn't already show item by item (curves, sets, fields…).
  const openable = !!shape && !!inspect && (shape.length > 1 || (d?.kind !== "table" && d?.kind !== "sketch"));
  const sizes = [shape?.[0] ?? 0, ...(item?.levels.slice(1) ?? [])];
  const pickers = openable ? sizes.slice(0, Math.min(sizes.length, path.length + 1)) : [];
  const shownDisplay = path.length ? item?.display : d;
  let body: React.ReactNode;
  if (editor && !path.length) {
    body = (
      <>
        {editor}
        {result && result.status !== "ok" && <div className={`empty msg-${result.status}`}>{result.message}</div>}
      </>
    );
  } else if (!result) {
    body = <div className="empty">Running…</div>;
  } else if (result.status !== "ok") {
    body = <div className={`empty msg-${result.status}`}>{result.message}</div>;
  } else if (path.length && itemError) {
    body = <div className="empty msg-error">{itemError}</div>;
  } else if (path.length && !item) {
    body = <div className="empty">Opening…</div>;
  } else if (!shownDisplay) {
    body = <div className="empty">{path.length ? item?.summary || "No preview for this item." : "This node has no preview."}</div>;
  } else {
    body = <DisplayBody d={shownDisplay} />;
  }
  const levelBar = pickers.length > 0 && (
    <div className="level-bar" title="Look inside the result: pick an item at each level (a row of a grid, one curve…)">
      {pickers.map((n, k) => (
        <label key={k}>
          {k === 0 ? "Item" : "›"}
          <select
            value={k < path.length ? path[k] : -1}
            onChange={(e) => {
              const i = Number(e.target.value);
              setPath(i < 0 ? path.slice(0, k) : [...path.slice(0, k), i]);
            }}
          >
            <option value={-1}>All {n}</option>
            {Array.from({ length: Math.min(n, 2000) }, (_, i) => (
              <option key={i} value={i}>
                #{i}
              </option>
            ))}
          </select>
        </label>
      ))}
      {path.length > 0 && item?.summary && <span className="muted">{item.summary}</span>}
    </div>
  );
  return (
    <div className="viewer">
      <div className="viewer-head">
        <span className="viewer-title">
          <b>{name || def.title}</b>
          {name && <span className="muted"> ({def.title})</span>}
          {d && "title" in d && d.title && d.kind !== "info" && <span className="muted"> · {d.title}</span>}
        </span>
        {actions}
      </div>
      {levelBar}
      <div className="viewer-body">
        {result?.warnings?.length ? (
          <ul className="viewer-warnings">
            {result.warnings.map((w, i) => (
              <li key={i}>⚠ {w}</li>
            ))}
          </ul>
        ) : null}
        {body}
      </div>
    </div>
  );
}

export function DisplayBody({ d }: { d: Display }) {
  if (d.kind === "value") {
    return (
      <div className="value-view">
        <div className="value-label">{d.label}</div>
        <div className="value-number">
          {fmt(d.value, d.digits)}
          {d.unit && <span className="value-unit"> {d.unit}</span>}
        </div>
        {d.where && <div className="muted">at {d.where}</div>}
      </div>
    );
  }
  if (d.kind === "text") {
    return (
      <div className="value-view">
        <div className="value-label">{d.label}</div>
        <div
          className={`value-number ${d.text === "Yes" ? "yes" : d.text === "No" ? "no" : ""} ${d.text.length > 14 ? "long-text" : ""}`}
        >
          {d.text}
        </div>
      </div>
    );
  }
  if (d.kind === "info") {
    return (
      <dl className="info-view">
        <dt>Source</dt>
        <dd>{d.title}</dd>
        {d.items.map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <dt>{k}</dt>
            <dd>{v || "—"}</dd>
          </div>
        ))}
      </dl>
    );
  }
  if (d.kind === "log") return <LogView display={d} />;
  if (d.kind === "table") return <TableView display={d} />;
  if (d.kind === "chart") return <ChartView display={d} />;
  if (d.kind === "sketch") return <SketchView display={d} />;
  if (d.kind === "model3d") return <ModelView display={d} />;
  return <ContourView display={d} />;
}

/** A file a job writes as it runs (.sta, .msg, .dat, .log): it stays scrolled to the end while it grows, unless
 * the reader scrolled up; errors and warnings stand out. */
function LogView({ display }: { display: Extract<Display, { kind: "log" }> }) {
  const ref = useRef<HTMLPreElement>(null);
  const atEnd = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (el && atEnd.current) el.scrollTop = el.scrollHeight;
  }, [display.text]);
  const lines = display.text.split("\n");
  return (
    <div className="log-view">
      <pre
        ref={ref}
        title={display.path}
        onScroll={(e) => {
          const el = e.currentTarget;
          atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
      >
        {lines.map((line, i) => (
          <div
            key={i}
            className={/\*\*\*ERROR|Abaqus Error|exited with error/i.test(line) ? "log-error" : /\*\*\*WARNING/i.test(line) ? "log-warning" : undefined}
          >
            {line || " "}
          </div>
        ))}
      </pre>
    </div>
  );
}
