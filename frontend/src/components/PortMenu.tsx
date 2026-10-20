import type { Edge, Node } from "@xyflow/react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  CATEGORY_COLORS,
  PORT_COLORS,
  PORT_TYPE_NAMES,
  findPort,
  nodeTitle,
  typesFit,
  type NodeDef,
  type NodeResult,
  type OpNodeData,
  type PortType,
} from "../types";

export interface PortMenuRequest {
  x: number;
  y: number;
  nodeId: string;
  handle: string;
  side: "in" | "out"; // the clicked port is an input (incl. settings) or an output
  type: PortType;
}

export interface PortChoice {
  nodeId: string;
  handle: string;
  connected: boolean;
}

/** Nodes reachable from `start` following wires forwards (or backwards). */
function reachable(start: string, edges: Edge[], forward: boolean): Set<string> {
  const seen = new Set<string>([start]);
  const queue = [start];
  while (queue.length) {
    const cur = queue.pop()!;
    for (const e of edges) {
      const [from, to] = forward ? [e.source, e.target] : [e.target, e.source];
      if (from === cur && !seen.has(to)) {
        seen.add(to);
        queue.push(to);
      }
    }
  }
  return seen;
}

export type ExistingPort = PortChoice & {
  title: string;
  port: string;
  summary: string;
  category: string;
  exact: boolean;
  distance: number; // on the canvas, from the node the menu was opened on (nearby ports are the likely ones)
};

/** The ports already in the graph that fit `req`'s port (outputs for an input, inputs and settings for an output). */
export function existingPorts(
  req: { nodeId: string; handle: string; side: "in" | "out"; type: PortType },
  nodes: Node<OpNodeData>[],
  edges: Edge[],
  defs: Record<string, NodeDef>,
  results: Record<string, NodeResult>,
  query = "",
): ExistingPort[] {
  // Connecting must not create a loop: an input can't take from nodes downstream of it, and vice versa.
  const blocked = reachable(req.nodeId, edges, req.side === "in");
  const q = query.trim().toLowerCase();
  const origin = nodes.find((n) => n.id === req.nodeId)?.position;
  const list: ExistingPort[] = [];
  for (const n of nodes) {
    if (blocked.has(n.id)) continue;
    const def = defs[n.data.type];
    if (!def) continue;
    const handles =
      req.side === "in"
        ? def.outputs.map((p) => p.name)
        : [...def.inputs.map((p) => p.name), ...def.params.filter((p) => p.port).map((p) => `param:${p.name}`)];
    for (const h of handles) {
      const p = findPort(def, h, req.side === "in" ? "out" : "in");
      if (!p || !typesFit(p.type, req.type)) continue;
      const summary = req.side === "in" ? results[n.id]?.summary ?? "" : "";
      const title = nodeTitle(def, n.data);
      const label = `${title} ${def.title} ${p.label} ${summary}`.toLowerCase();
      if (q && !label.includes(q)) continue;
      const connected = edges.some((e) =>
        req.side === "in"
          ? e.target === req.nodeId && e.targetHandle === req.handle && e.source === n.id && e.sourceHandle === h
          : e.source === req.nodeId && e.sourceHandle === req.handle && e.target === n.id && e.targetHandle === h,
      );
      list.push({ nodeId: n.id, handle: h, connected, title, port: p.label, summary, category: def.category,
                  exact: p.type === req.type,
                  distance: origin ? Math.hypot(n.position.x - origin.x, n.position.y - origin.y) : 0 });
    }
  }
  // Connected first, then ports of exactly this type before "anything" ports (List nodes, Formula).
  return list.sort((a, b) => Number(b.connected) - Number(a.connected) || Number(b.exact) - Number(a.exact) ||
    a.title.localeCompare(b.title));
}

/** Click on a port: the matching ports that already exist in the graph, to connect in one click. */
export function PortMenu({ req, nodes, edges, defs, results, onPick, onNewNode, onClose }: {
  req: PortMenuRequest;
  nodes: Node<OpNodeData>[];
  edges: Edge[];
  defs: Record<string, NodeDef>;
  results: Record<string, NodeResult>;
  onPick: (c: PortChoice) => void;
  onNewNode: () => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const options = useMemo(() => existingPorts(req, nodes, edges, defs, results, query), [req, nodes, edges, defs, results, query]);

  useEffect(() => {
    const away = (e: Event) => !box.current?.contains(e.target as globalThis.Node) && onClose();
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

  const left = Math.min(req.x + 8, window.innerWidth - 310);
  const top = Math.min(req.y + 8, window.innerHeight - 380);
  return (
    <div className="quick-add port-menu" ref={box} style={{ left, top, width: 300 }}>
      <div className="quick-head">
        <span className="help-dot" style={{ background: PORT_COLORS[req.type] }} />
        {req.side === "in" ? "Take this " : "Send this "}
        {PORT_TYPE_NAMES[req.type].toLowerCase()} {req.side === "in" ? "from…" : "to…"}
      </div>
      <input autoFocus placeholder="Search…" value={query} onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && options[0] && onPick(options[0])} />
      <div className="quick-list">
        {options.map((o) => (
          <button key={`${o.nodeId}.${o.handle}`} onClick={() => onPick(o)}
            title={o.connected ? "Connected: click to disconnect" : "Click to connect"}>
            <span className="swatch" style={{ background: CATEGORY_COLORS[o.category] }} />
            <span className="port-choice">
              <span>
                {o.connected && "✓ "}
                {o.title} <em>· {o.port}</em>
              </span>
              {o.summary && <small className="muted">{o.summary}</small>}
            </span>
          </button>
        ))}
        {!options.length && <div className="empty">Nothing in the graph fits here yet.</div>}
      </div>
      <button className="link-btn new-node" onClick={onNewNode}>
        ＋ New node…
      </button>
    </div>
  );
}
