import type { Edge, Node } from "@xyflow/react";

/** Tidying the canvas: align, distribute and auto-arrange nodes (positions only; results never change). */

type Pos = Map<string, { x: number; y: number }>;
const W = (n: Node) => n.measured?.width ?? 250;
const H = (n: Node) => n.measured?.height ?? 140;

export type AlignMode = "left" | "center" | "right" | "top" | "middle" | "bottom";

export function align(nodes: Node[], mode: AlignMode): Pos {
  const out: Pos = new Map();
  if (nodes.length < 2) return out;
  const x0 = Math.min(...nodes.map((n) => n.position.x));
  const x1 = Math.max(...nodes.map((n) => n.position.x + W(n)));
  const y0 = Math.min(...nodes.map((n) => n.position.y));
  const y1 = Math.max(...nodes.map((n) => n.position.y + H(n)));
  for (const n of nodes) {
    const { x, y } = n.position;
    out.set(n.id, {
      left: { x: x0, y },
      center: { x: (x0 + x1) / 2 - W(n) / 2, y },
      right: { x: x1 - W(n), y },
      top: { x, y: y0 },
      middle: { x, y: (y0 + y1) / 2 - H(n) / 2 },
      bottom: { x, y: y1 - H(n) },
    }[mode]);
  }
  return out;
}

/** Even gaps between nodes, in the order they already are, keeping the first and last in place. */
export function distribute(nodes: Node[], axis: "x" | "y"): Pos {
  const out: Pos = new Map();
  if (nodes.length < 3) return out;
  const size = axis === "x" ? W : H;
  const sorted = [...nodes].sort((a, b) => a.position[axis] - b.position[axis]);
  const start = sorted[0].position[axis];
  const end = sorted[sorted.length - 1].position[axis] + size(sorted[sorted.length - 1]);
  const gap = (end - start - sorted.reduce((s, n) => s + size(n), 0)) / (sorted.length - 1);
  let at = start;
  for (const n of sorted) {
    out.set(n.id, axis === "x" ? { x: at, y: n.position.y } : { x: n.position.x, y: at });
    at += size(n) + gap;
  }
  return out;
}

/** Columns that follow the data flow (each node one column right of its furthest input), rows ordered to keep
 *  wires short, starting where the nodes' top-left corner was. Like Blender's Node Arrange or Houdini's L. */
export function autoArrange(nodes: Node[], edges: Edge[], gapX = 70, gapY = 30): Pos {
  const out: Pos = new Map();
  if (!nodes.length) return out;
  const ids = new Set(nodes.map((n) => n.id));
  const inner = edges.filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target);
  const col = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  // Longest path from the sources (the graph has no cycles; cap the passes anyway).
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false;
    for (const e of inner) {
      const c = (col.get(e.source) ?? 0) + 1;
      if (c > (col.get(e.target) ?? 0)) {
        col.set(e.target, c);
        changed = true;
      }
    }
    if (!changed) break;
  }
  // Pull nodes that only feed later columns next to what they feed (e.g. a Number for a Formula far right).
  for (const n of nodes) {
    const outs = inner.filter((e) => e.source === n.id).map((e) => col.get(e.target) ?? 0);
    if (outs.length && !inner.some((e) => e.target === n.id)) col.set(n.id, Math.max(0, Math.min(...outs) - 1));
  }
  const columns = new Map<number, Node[]>();
  for (const n of nodes) columns.set(col.get(n.id)!, [...(columns.get(col.get(n.id)!) ?? []), n]);
  const x0 = Math.min(...nodes.map((n) => n.position.x));
  const y0 = Math.min(...nodes.map((n) => n.position.y));
  const rowY = new Map<string, number>();
  let x = x0;
  for (const c of [...columns.keys()].sort((a, b) => a - b)) {
    const list = columns.get(c)!;
    // Order a column by where its inputs sit (barycentre), the first column by current height.
    const key = (n: Node) => {
      const ys = inner.filter((e) => e.target === n.id && rowY.has(e.source)).map((e) => rowY.get(e.source)!);
      return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : n.position.y;
    };
    list.sort((a, b) => key(a) - key(b));
    let y = y0;
    for (const n of list) {
      out.set(n.id, { x, y });
      rowY.set(n.id, y + H(n) / 2);
      y += H(n) + gapY;
    }
    x += Math.max(...list.map(W)) + gapX;
  }
  return out;
}
