import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { CATEGORY_COLORS, PORT_COLORS, PORT_TYPE_NAMES, nodeMatches, typesFit, type NodeDef, type PortType } from "../types";
import type { ExistingPort } from "./PortMenu";

/** Existing ports shown before the "more" row when nothing is typed. */
const NEAREST = 3;

export interface QuickAddRequest {
  x: number; // screen position of the drop
  y: number;
  fromNode: string; // "" when opened on empty canvas (double-click): any node, nothing to connect
  fromHandle: string;
  fromSide: "source" | "target"; // dragged from an output (source) or an input (target)
  type: PortType;
}

export interface QuickAddChoice {
  nodeType: string;
  port: string; // the new node's port to connect
}

/** What can take (or give) the dragged wire, as a searchable list at the mouse: matching ports of nodes already in
 *  the graph first (the nearest few, or those named like the search), then the node types that could be added. */
export function QuickAdd({ req, defs, allDefs = defs, categories, existing = [], onPick, onPickExisting, onClose }: {
  req: QuickAddRequest;
  defs: NodeDef[];
  allDefs?: NodeDef[]; // including hidden ones, so their old names still find the node that replaced them
  categories: string[];
  existing?: ExistingPort[];
  onPick: (c: QuickAddChoice) => void;
  onPickExisting?: (c: ExistingPort) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);

  const options = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list: (QuickAddChoice & { title: string; portLabel: string; category: string; exact: boolean })[] = [];
    for (const d of defs) {
      if (!req.fromNode) {
        if (!nodeMatches(allDefs, d, q)) continue;
        list.push({ nodeType: d.type, port: "", title: d.title, portLabel: "", category: d.category, exact: true });
        continue;
      }
      const ports = req.fromSide === "source" ? d.inputs : d.outputs;
      const fits = ports.filter((p) => typesFit(p.type, req.type));
      // Prefer ports of exactly this type over "anything" ports of List nodes.
      const port = fits.find((p) => p.type === req.type) ?? fits[0];
      if (!port) continue;
      if (!nodeMatches(allDefs, d, q)) continue;
      list.push({
        nodeType: d.type,
        port: port.name,
        title: d.title,
        portLabel: fits.length > 1 || ports.length > 1 ? port.label : "",
        category: d.category,
        exact: port.type === req.type,
      });
    }
    const order = (c: string) => categories.indexOf(c);
    // What you typed matching the title beats matching the description (typing "model" finds Model first).
    const rank = (t: string) => (!q ? 0 : t.toLowerCase().startsWith(q) ? 0 : t.toLowerCase().includes(q) ? 1 : 2);
    return list.sort((a, b) => rank(a.title) - rank(b.title) || Number(b.exact) - Number(a.exact) ||
      order(a.category) - order(b.category));
  }, [defs, allDefs, categories, req, query]);

  // Existing ports must not bury the new nodes: only the few nearest show, the rest behind a "more" row. With a
  // search, those are the ports whose node or port name matches; ports matching only on their value come after the
  // new nodes.
  const [showAll, setShowAll] = useState(false);
  const { first, more, rest } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const free = existing.filter((o) => !o.connected);
    const byName = (o: ExistingPort) => !q || `${o.title} ${o.port}`.toLowerCase().includes(q);
    const near = free.filter(byName).sort((a, b) => a.distance - b.distance);
    const shown = showAll || near.length <= NEAREST + 1 ? near : near.slice(0, NEAREST);
    return {
      first: shown,
      more: near.length - shown.length,
      rest: !q ? [] : free.filter((o) => !byName(o) && `${o.title} ${o.port} ${o.summary}`.toLowerCase().includes(q)),
    };
  }, [existing, query, showAll]);
  type Row = { kind: "existing"; o: ExistingPort } | { kind: "more" } | { kind: "new"; o: (typeof options)[number] };
  const rows: Row[] = [
    ...first.map((o) => ({ kind: "existing" as const, o })),
    ...(more ? [{ kind: "more" as const }] : []),
    ...options.map((o) => ({ kind: "new" as const, o })),
    ...rest.map((o) => ({ kind: "existing" as const, o })),
  ];
  const total = rows.length;
  const pickAt = (i: number) => {
    const r = rows[i];
    if (!r) return;
    if (r.kind === "more") setShowAll(true);
    else if (r.kind === "existing") onPickExisting?.(r.o);
    else onPick(r.o);
  };

  useEffect(() => {
    setActive(0);
    setShowAll(false);
  }, [query]);

  useEffect(() => {
    // Capture phase: the canvas stops mouse events for its own panning, so a normal listener never hears them.
    const away = (e: Event) => !box.current?.contains(e.target as Node) && onClose();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("pointerdown", away, true);
    window.addEventListener("wheel", away, true);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("pointerdown", away, true);
      window.removeEventListener("wheel", away, true);
      window.removeEventListener("keydown", esc);
    };
  }, [onClose]);

  // Keep the menu on screen.
  const left = Math.min(req.x, window.innerWidth - 270);
  const top = Math.min(req.y, window.innerHeight - 360);

  return (
    <div className="quick-add" ref={box} style={{ left, top }}>
      <div className="quick-head">
        {req.fromNode ? (
          <>
            <span className="help-dot" style={{ background: PORT_COLORS[req.type] }} />
            {req.fromSide === "source" ? "Connect this " : "Feed this "}
            {PORT_TYPE_NAMES[req.type].toLowerCase()} {req.fromSide === "source" ? "to…" : "from…"}
          </>
        ) : (
          "Add a node here…"
        )}
      </div>
      <input
        autoFocus
        placeholder="Search…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
          else if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, total - 1));
          else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
          else if (e.key === "Enter") pickAt(active);
        }}
      />
      <div className="quick-list">
        {rows.map((r, i) => {
          const prev = rows[i - 1];
          const head =
            r.kind === "new"
              ? prev?.kind !== "new" && i > 0 && "New node"
              : r.kind === "existing" && (!prev ? "Already in the graph" : prev.kind === "new" && "Also in the graph");
          const props = { className: i === active ? "active" : "", onMouseEnter: () => setActive(i), onClick: () => pickAt(i) };
          return (
            <Fragment key={r.kind === "existing" ? `${r.o.nodeId}.${r.o.handle}` : r.kind === "new" ? r.o.nodeType : "more"}>
              {head && <div className="quick-section">{head}</div>}
              {r.kind === "existing" ? (
                <button {...props}>
                  <span className="swatch" style={{ background: CATEGORY_COLORS[r.o.category] }} />
                  <span className="port-choice">
                    <span>
                      {r.o.title} <em>· {r.o.port}</em>
                    </span>
                    {r.o.summary && <small className="muted">{r.o.summary}</small>}
                  </span>
                </button>
              ) : r.kind === "more" ? (
                <button {...props} onMouseDown={(e) => e.preventDefault()}>
                  <em>+ {more} more in the graph…</em>
                </button>
              ) : (
                <button {...props}>
                  <span className="swatch" style={{ background: CATEGORY_COLORS[r.o.category] }} />
                  {r.o.title}
                  {r.o.portLabel && <em> → {r.o.portLabel}</em>}
                </button>
              )}
            </Fragment>
          );
        })}
        {!total && <div className="empty">No node fits</div>}
      </div>
    </div>
  );
}
